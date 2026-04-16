from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set, Tuple

from agents import AgentRuntime
from runtime.agent import (
    BACKEND,
    _managed_startup_reuse_wait_policy,
    append_action_history,
    apply_runtime_to_meta,
    ensure_agent_running,
    ensure_pane,
    get_agent,
    is_agent_running,
    prepare_managed_runtime,
    runtime_home_from_meta,
    runtime_home_managed_from_meta,
    runtime_label_from_meta,
    wait_for_agent_ready,
    wait_for_managed_startup_ready,
)
from runtime.turn import (
    CLAUDE_PROMPT_ACK_TIMEOUT,
    complete_pending_turn,
    initialize_session_startup,
    wait_for_prompt_ack,
)
from runtime.watchdog import (
    start_session_watchdog,
    stop_session_watchdog,
)
from session import (
    _read_notify_binding,
    apply_session_open_state,
    begin_pending_turn,
    build_notify_binding,
    clear_legacy_notify_state,
    load_meta,
    managed_session_ttl_seconds,
    remove_meta,
    save_meta,
    session_children,
    session_last_event_at,
    session_metadata_is_live,
    session_parent,
    set_notify_binding,
    touch_session_event,
    update_runtime_config,
)
from session.types import SessionMeta, as_startup_state
from text_utils import (
    _is_prompt_fragment,
    extract_summary_candidate,
    turn_delta,
)
from tmux import (
    DEFAULT_CAPTURE_LINES,
    _tmux_has_session,
    ensure_tmux_session,
    get_pane_info,
    list_tmux_session_clients,
    pane_exists,
    read_pane,
    tmux,
)
from tmux.bridge import (
    bridge_keys,
    bridge_resolve,
    bridge_type,
)


class OrcheError(RuntimeError):
    pass


class AgentStartupBlockedError(OrcheError):
    pass


def _notify_discord_channel_id(notify_binding: Dict[str, str]) -> str:
    if notify_binding.get("provider") == "discord":
        return str(notify_binding.get("target") or "").strip()
    return ""


def _resolve_runtime(
    *,
    plugin,
    session: str,
    cwd: Path,
    existing_meta: SessionMeta,
    discord_channel_id: str,
) -> Tuple[AgentRuntime, str, bool]:
    existing_runtime_home = runtime_home_from_meta(existing_meta)
    if existing_runtime_home:
        runtime_home_managed = runtime_home_managed_from_meta(existing_meta)
        return (
            AgentRuntime(
                home=existing_runtime_home,
                managed=runtime_home_managed,
                label=runtime_label_from_meta(existing_meta, plugin),
            ),
            existing_runtime_home,
            runtime_home_managed,
        )
    runtime = prepare_managed_runtime(
        plugin, session, cwd=cwd, discord_channel_id=discord_channel_id
    )
    return runtime, runtime.home, True


def _parent_session_from_notify(
    *, tmux_mode: str, notify_binding: Dict[str, str]
) -> str:
    target = str(notify_binding.get("target") or "").strip()
    if (
        tmux_mode == "inline-pane"
        and notify_binding.get("provider") == "tmux-bridge"
        and target
        and not target.startswith("pane:")
    ):
        return target
    return ""


def _load_session_context(
    session: str,
) -> Tuple[Path, str, SessionMeta, Dict[str, str]]:
    existing_meta = load_meta(session)
    if not existing_meta:
        raise OrcheError(f"Unknown session: {session}")
    raw_cwd = str(existing_meta.get("cwd") or "").strip()
    agent = str(existing_meta.get("agent") or "").strip()
    if not raw_cwd or not agent:
        raise OrcheError(
            f"Session {session} is missing cwd/agent context; open it first"
        )
    notify_binding = _read_notify_binding(existing_meta)
    return Path(raw_cwd).resolve(), agent, existing_meta, notify_binding


def create_session(
    session: str,
    cwd: Path,
    agent: str,
    *,
    notify_to: Optional[str] = None,
    notify_target: Optional[str] = None,
) -> str:
    if load_meta(session):
        raise OrcheError(
            f"Session {session} already exists. Use 'orche attach {session}' or choose a different --name."
        )
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    notify_binding = (
        build_notify_binding(notify_to, notify_target)
        if notify_to and notify_target
        else {}
    )
    runtime, resolved_runtime_home, runtime_home_managed = _resolve_runtime(
        plugin=plugin,
        session=session,
        cwd=cwd,
        existing_meta={},
        discord_channel_id=_notify_discord_channel_id(notify_binding),
    )
    return _start_or_restore_session(
        session=session,
        cwd=cwd,
        agent=agent,
        plugin=plugin,
        existing_meta={},
        notify_binding=notify_binding,
        runtime=runtime,
        resolved_runtime_home=resolved_runtime_home,
        runtime_home_managed=runtime_home_managed,
    )


def ensure_session(session: str) -> str:
    cwd, agent, existing_meta, notify_binding = _load_session_context(session)
    plugin = get_agent(agent)
    runtime, resolved_runtime_home, runtime_home_managed = _resolve_runtime(
        plugin=plugin,
        session=session,
        cwd=cwd,
        existing_meta=existing_meta,
        discord_channel_id=_notify_discord_channel_id(notify_binding),
    )
    return _start_or_restore_session(
        session=session,
        cwd=cwd,
        agent=agent,
        plugin=plugin,
        existing_meta=existing_meta,
        notify_binding=notify_binding,
        runtime=runtime,
        resolved_runtime_home=resolved_runtime_home,
        runtime_home_managed=runtime_home_managed,
    )


def _start_or_restore_session(
    *,
    session: str,
    cwd: Path,
    agent: str,
    plugin,
    existing_meta: SessionMeta,
    notify_binding: Dict[str, str],
    runtime: AgentRuntime,
    resolved_runtime_home: str,
    runtime_home_managed: bool,
) -> str:
    resolved_discord_channel_id = _notify_discord_channel_id(notify_binding)
    tmux_mode = str(existing_meta.get("tmux_mode") or "").strip() or "dedicated-session"
    host_pane_id = str(existing_meta.get("host_pane_id") or "").strip()
    tmux_host_session = str(existing_meta.get("tmux_host_session") or "").strip()
    parent_session = _parent_session_from_notify(
        tmux_mode=tmux_mode,
        notify_binding=notify_binding,
    )
    pane_id = ensure_pane(
        session,
        cwd,
        agent,
        tmux_mode=tmux_mode,
        host_pane_id=host_pane_id,
        tmux_host_session=tmux_host_session,
    )
    meta = load_meta(session)
    apply_session_open_state(
        meta,
        session=session,
        cwd=str(cwd),
        agent=agent,
        pane_id=pane_id,
        tmux_mode=tmux_mode,
        host_pane_id=host_pane_id,
        tmux_host_session=tmux_host_session,
        parent_session=parent_session,
        expires_after_seconds=managed_session_ttl_seconds(),
        backend=BACKEND,
        timestamp=time.time(),
    )
    apply_runtime_to_meta(meta, agent=agent, runtime=runtime)
    clear_legacy_notify_state(meta)
    set_notify_binding(meta, notify_binding)
    save_meta(session, meta)
    wait_for_startup = False
    if plugin.name in {"claude", "codex"} and runtime.managed:
        session_meta = load_meta(session)
        raw_startup = session_meta.get("startup")
        startup = as_startup_state(raw_startup)
        if is_agent_running(plugin, pane_id):
            wait_for_startup = _managed_startup_reuse_wait_policy(
                session, plugin, pane_id, startup
            )
        else:
            initialize_session_startup(session)
            wait_for_startup = True
    pane_id = ensure_agent_running(
        plugin,
        session,
        cwd,
        pane_id,
        runtime=runtime,
        discord_channel_id=resolved_discord_channel_id,
    )
    touch_session_event(session, source="open")
    if wait_for_startup:
        wait_for_managed_startup_ready(session, plugin, pane_id, cwd)
    elif plugin.name == "claude":
        wait_for_agent_ready(plugin, pane_id, cwd)
    update_runtime_config(
        session=session,
        cwd=cwd,
        agent=agent,
        pane_id=pane_id,
        tmux_session=str(load_meta(session).get("tmux_session") or ""),
        runtime_home=resolved_runtime_home,
        runtime_home_managed=runtime_home_managed,
        runtime_label=runtime.label,
    )
    return pane_id


def _pane_bridge_adapter(pane_id: str):
    resolved_pane_id = str(pane_id or "").strip()
    if not resolved_pane_id:
        raise OrcheError("pane_id is required")

    class _Bridge:
        def type(self, text: str) -> None:
            if not text:
                return
            buffer_name = f"orche-{uuid.uuid4().hex}"
            try:
                tmux(
                    "load-buffer",
                    "-b",
                    buffer_name,
                    "-",
                    check=True,
                    capture=True,
                    input_text=text,
                )
                tmux(
                    "paste-buffer",
                    "-t",
                    resolved_pane_id,
                    "-b",
                    buffer_name,
                    check=True,
                    capture=True,
                )
            finally:
                tmux("delete-buffer", "-b", buffer_name, check=False, capture=True)

        def keys(self, keys: Sequence[str]) -> None:
            values = list(keys)
            if values:
                tmux(
                    "send-keys",
                    "-t",
                    resolved_pane_id,
                    *values,
                    check=True,
                    capture=True,
                )

    return _Bridge()


def send_prompt_to_pane(
    session: str,
    cwd: Path,
    agent: str,
    prompt: str,
    *,
    pane_id: str,
) -> str:
    plugin = get_agent(agent)
    resolved_pane_id = str(pane_id or "").strip()
    if not resolved_pane_id:
        raise OrcheError("pane_id is required")
    meta = load_meta(session)
    wait_for_ack = plugin.name == "claude" and runtime_home_managed_from_meta(meta)
    submitted_at = time.time()
    pending_turn = begin_pending_turn(
        meta,
        prompt=prompt,
        pane_id=resolved_pane_id,
        before_capture=read_pane(resolved_pane_id, DEFAULT_CAPTURE_LINES),
        submitted_at=submitted_at,
    )
    save_meta(session, meta)
    touch_session_event(session, source="prompt-submit")
    plugin.submit_prompt(session, prompt, bridge=_pane_bridge_adapter(resolved_pane_id))
    with contextlib.suppress(Exception):
        start_session_watchdog(session, turn_id=str(pending_turn.get("turn_id") or ""))
    append_action_history(
        session, cwd, agent, "prompt", prompt=prompt, pane_id=resolved_pane_id
    )
    if wait_for_ack:
        wait_for_prompt_ack(
            session,
            turn_id=str(pending_turn.get("turn_id") or ""),
            prompt=prompt,
            timeout=CLAUDE_PROMPT_ACK_TIMEOUT,
        )
    return resolved_pane_id


def send_prompt_to_session(
    session: str,
    cwd: Path,
    agent: str,
    prompt: str,
) -> str:
    return send_prompt_to_pane(
        session,
        cwd,
        agent,
        prompt,
        pane_id=ensure_session(session),
    )


def _completion_summary_from_capture(
    plugin, *, capture: str, before_capture: str, prompt: str
) -> str:
    delta = turn_delta(before_capture, capture) if capture else ""
    for candidate in (delta, capture):
        if not candidate:
            continue
        summary = plugin.extract_completion_summary(candidate, prompt)
        if summary:
            return summary
        if plugin.capture_has_completion_surface(candidate, prompt):
            fallback = extract_summary_candidate(candidate, prompt=prompt)
            if fallback and not _is_prompt_fragment(fallback, prompt):
                return fallback
    return ""


def latest_turn_summary(session: str) -> str:
    meta = load_meta(session)
    pending_turn = (
        meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    )
    if pending_turn:
        plugin = get_agent(str(meta.get("agent") or "codex"))
        prompt = str(pending_turn.get("prompt") or "")
        fallback_pane_id = str(
            pending_turn.get("pane_id") or meta.get("pane_id") or ""
        ).strip()
        pane_id = str(
            bridge_resolve(session, fallback_pane_id=fallback_pane_id)
            or fallback_pane_id
        ).strip()
        summary = _completion_summary_from_capture(
            plugin,
            capture=read_pane(pane_id, DEFAULT_CAPTURE_LINES) if pane_id else "",
            before_capture=str(pending_turn.get("before_capture") or ""),
            prompt=prompt,
        )
        if summary:
            complete_pending_turn(session, summary=summary)
            return summary
        return ""
    last_completed = (
        meta.get("last_completed_turn")
        if isinstance(meta.get("last_completed_turn"), dict)
        else None
    )
    return str(last_completed.get("summary") or "") if last_completed else ""


def build_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    if not meta:
        raise OrcheError(f"Unknown session: {session}")
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    pane_id = (
        bridge_resolve(session, fallback_pane_id=fallback_pane_id) or fallback_pane_id
    )
    info = get_pane_info(pane_id) if pane_id else None
    plugin = get_agent(str(meta.get("agent") or "codex"))
    raw_pending_turn = meta.get("pending_turn")
    pending_turn: Dict[str, Any] = (
        dict(raw_pending_turn) if isinstance(raw_pending_turn, dict) else {}
    )
    return {
        "backend": BACKEND,
        "session": session,
        "cwd": str(meta.get("cwd") or (info or {}).get("pane_current_path") or "-"),
        "agent": str(meta.get("agent") or "codex"),
        "runtime_home": runtime_home_from_meta(meta),
        "runtime_home_managed": runtime_home_managed_from_meta(meta),
        "runtime_label": runtime_label_from_meta(meta, plugin),
        "codex_home": str(meta.get("codex_home") or runtime_home_from_meta(meta)),
        "codex_home_managed": bool(
            meta.get("codex_home_managed") or runtime_home_managed_from_meta(meta)
        ),
        "tmux_session": str(
            (info or {}).get("session_name") or meta.get("tmux_session") or ""
        ).strip()
        or "-",
        "pane_id": pane_id or "-",
        "window_name": (info or {}).get("window_name", meta.get("window_name", "-")),
        "agent_running": bool(pane_id and is_agent_running(plugin, pane_id)),
        "codex_running": bool(pane_id and is_agent_running(plugin, pane_id)),
        "pane_exists": bool(pane_id and pane_exists(pane_id)),
        "discord_session": (
            _read_notify_binding(meta).get("session", "")
            if _read_notify_binding(meta).get("provider") == "discord"
            else ""
        ),
        "notify_binding": _read_notify_binding(meta),
        "parent_session": session_parent(meta),
        "child_count": len(session_children(session, live_only=True)),
        "last_event_at": session_last_event_at(meta),
        "ttl_seconds": int(
            meta.get("expires_after_seconds") or managed_session_ttl_seconds()
        ),
        "ttl_exempt_because_parent_alive": bool(
            session_parent(meta)
            and session_metadata_is_live(
                session_parent(meta), load_meta(session_parent(meta))
            )
        ),
        "pending_turn_id": str(pending_turn.get("turn_id") or ""),
        "pending_turn_submitted_at": float(pending_turn.get("submitted_at") or 0.0),
        "startup": dict(meta.get("startup") or {}),
        "prompt_ack": dict(pending_turn.get("prompt_ack") or {}),
        "watchdog": dict(pending_turn.get("watchdog") or {}),
    }


def resolve_session_context(
    *, session: str, require_existing: bool = False, require_cwd_agent: bool = False
) -> Tuple[Optional[Path], Optional[str], SessionMeta]:
    meta = load_meta(session)
    cwd = Path(str(meta.get("cwd") or "")).resolve() if meta.get("cwd") else None
    agent = str(meta.get("agent")) if meta.get("agent") else None
    if require_existing and not meta:
        raise OrcheError(f"Unknown session: {session}")
    if require_cwd_agent and (cwd is None or agent is None):
        raise OrcheError(
            f"Session {session} is missing cwd/agent context; open it first"
        )
    return cwd, agent, meta


def current_session_id() -> str:
    env_session = str(os.environ.get("ORCHE_SESSION") or "").strip()
    if env_session:
        return env_session
    raise OrcheError(
        "Unable to resolve current orche session id. Set ORCHE_SESSION or run inside an orche tmux pane."
    )


def cancel_session(session: str) -> str:
    _cwd, agent, meta = resolve_session_context(session=session)
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    get_agent(agent or "codex").interrupt(
        session, bridge=bridge_adapter(session, fallback_pane_id=fallback_pane_id)
    )
    return bridge_resolve(session, fallback_pane_id=fallback_pane_id) or "-"


def _close_session_single(session: str) -> str:
    meta = load_meta(session)
    if not meta:
        return "-"
    plugin = get_agent(str(meta.get("agent") or "codex"))
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    pane_id = (
        bridge_resolve(session, fallback_pane_id=fallback_pane_id) or fallback_pane_id
    )
    info = get_pane_info(pane_id) if pane_id and pane_exists(pane_id) else None
    target_tmux_session = str(
        (info or {}).get("session_name") or meta.get("tmux_session") or ""
    ).strip() or ensure_tmux_session(session, Path(str(meta.get("cwd") or ".")))
    with contextlib.suppress(Exception):
        stop_session_watchdog(session)
    if str(meta.get("tmux_mode") or "").strip() == "inline-pane":
        if pane_id and pane_exists(pane_id):
            tmux("kill-pane", "-t", pane_id, check=False, capture=True)
    else:
        for client_tty in list_tmux_session_clients(target_tmux_session):
            tmux("detach-client", "-t", client_tty, check=False, capture=True)
        if _tmux_has_session(target_tmux_session):
            tmux("kill-session", "-t", target_tmux_session, check=False, capture=True)
    runtime_home = runtime_home_from_meta(meta)
    if runtime_home and runtime_home_managed_from_meta(meta):
        plugin.cleanup_runtime(
            AgentRuntime(
                home=runtime_home,
                managed=True,
                label=runtime_label_from_meta(meta, plugin),
            )
        )
    remove_meta(session)
    return pane_id or "-"


def close_session_tree(
    session: str, *, reason: str = "", _visited: Optional[Set[str]] = None
) -> str:
    session_name = str(session or "").strip()
    if not session_name:
        return "-"
    visited = _visited if _visited is not None else set()
    if session_name in visited:
        return "-"
    visited.add(session_name)
    fallback_pane_id = str(load_meta(session_name).get("pane_id") or "").strip()
    root_pane = (
        bridge_resolve(session_name, fallback_pane_id=fallback_pane_id)
        or fallback_pane_id
        or "-"
    )
    for child in session_children(session_name):
        close_session_tree(child, reason=reason, _visited=visited)
    _close_session_single(session_name)
    return root_pane


def close_session(session: str) -> str:
    return close_session_tree(session)


def bridge_adapter(session: str = "", *, fallback_pane_id: str = ""):
    class _Bridge:
        def type(self, text: str) -> None:
            bridge_type(session_name, text, fallback_pane_id=fallback_pane_id)

        def keys(self, keys: Sequence[str]) -> None:
            bridge_keys(session_name, keys, fallback_pane_id=fallback_pane_id)

    session_name = session
    return _Bridge()
