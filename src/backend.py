from __future__ import annotations

import contextlib
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Set, Tuple

import agents.claude as claude_agent_module
import agents.codex as codex_agent_module
from agents import AgentRuntime
from agents.claude import ClaudeAgent
from agents.codex import CodexAgent, SOURCE_CONFIG_BACKUP_SUFFIX
from agents.common import ensure_orche_shim
from runtime.agent import (
    BACKEND,
    CLAUDE_STARTUP_GRACE_SECONDS,
    DEFAULT_CLAUDE_COMMAND,
    DEFAULT_CLAUDE_SOURCE_CONFIG_PATH,
    DEFAULT_CLAUDE_SOURCE_HOME,
    DEFAULT_CODEX_HOME_ROOT,
    DEFAULT_CODEX_SOURCE_HOME,
    LAUNCH_ERROR_PREFIX,
    STARTUP_TIMEOUT,
    _managed_startup_reuse_wait_policy,
    append_action_history,
    apply_runtime_to_meta,
    build_native_agent_launch_command,
    ensure_agent_running,
    ensure_managed_claude_home,
    ensure_managed_codex_home,
    ensure_native_agent_running,
    ensure_pane,
    extract_launch_error,
    get_agent,
    is_agent_running,
    native_cli_args_from_meta,
    prepare_managed_runtime,
    runtime_home_from_meta,
    runtime_home_managed_from_meta,
    runtime_label_from_meta,
    session_launch_mode,
    supported_agent_names,
    deliver_notify_to_session,
    wait_for_agent_process_start,
    wait_for_agent_ready,
    wait_for_managed_startup_ready,
)
from runtime.turn import (
    CLAUDE_PROMPT_ACK_TIMEOUT,
    claim_turn_notification,
    complete_pending_turn,
    initialize_session_startup,
    mark_pending_turn_prompt_accepted,
    mark_session_startup_blocked,
    mark_session_startup_ready,
    mark_session_startup_timeout,
    release_turn_notification,
    update_watchdog_metadata,
    wait_for_prompt_ack,
)
from runtime.watchdog import (
    WATCHDOG_NEEDS_INPUT_AFTER,
    WATCHDOG_NOTIFY_BUFFER,
    WATCHDOG_POLL_INTERVAL,
    WATCHDOG_REMINDER_AFTER,
    WATCHDOG_STALLED_AFTER,
    _latest_notification_at,
    _orche_bootstrap_command,
    _watchdog_event_status,
    _watchdog_pending_event_ready,
    _watchdog_reminder_summary,
    _watchdog_summary_for_event,
    emit_internal_notify,
    run_session_watchdog,
    start_session_watchdog,
    stop_session_watchdog,
)
from session import (
    DEFAULT_MANAGED_SESSION_TTL_SECONDS,
    DEFAULT_MAX_INLINE_SESSIONS,
    _iter_meta_payloads,
    _read_notify_binding,
    build_notify_binding,
    config_key_field,
    default_config_value,
    default_session_name,
    derive_discord_session,
    get_config_value,
    list_config_values,
    load_config,
    load_history_entries,
    load_meta,
    load_raw_config,
    log_event,
    log_exception,
    managed_session_last_event_at,
    managed_session_ttl_seconds,
    max_inline_sessions,
    meta_path,
    remove_meta,
    repo_name,
    reset_config_value,
    save_config,
    save_meta,
    session_children,
    session_exists,
    session_lock,
    session_metadata_is_live,
    session_parent,
    set_config_value,
    touch_session_event,
    update_runtime_config,
    validate_discord_channel_id,
    validate_notify_provider,
    attach_session,
    expire_managed_sessions,
    list_sessions,
    sample_pane_state,
    sample_watchdog_state,
    observable_progress_detected,
    recent_capture_excerpt,
)
from text_utils import _is_prompt_fragment, compact_text, extract_summary_candidate, turn_delta
from tmux.bridge import bridge_keys, bridge_name_pane, bridge_read, bridge_resolve, bridge_type
from tmux import (
    DEFAULT_CAPTURE_LINES,
    LEGACY_TMUX_SESSION,
    TMUX_PANE_OUTPUT_SEPARATOR,
    TMUX_SESSION,
    _known_tmux_sessions,
    _tmux_has_session,
    _tmux_join_fields,
    _tmux_split_fields,
    _tmux_value_for_pane,
    _tmux_window_index_in_use,
    ensure_tmux_session,
    find_window,
    get_pane_info,
    list_panes,
    list_tmux_session_clients,
    list_tmux_sessions,
    list_windows,
    next_window_index,
    pane_cursor_state,
    pane_exists,
    process_cpu_percent,
    process_descendants,
    process_is_alive,
    read_pane,
    require_tmux,
    run,
    tmux,
)


class OrcheError(RuntimeError):
    pass


class AgentStartupBlockedError(OrcheError):
    pass


def ensure_native_session(session: str, cwd: Path, agent: str, *, cli_args: Sequence[str] = ()) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(f"Session {session} is already bound to cwd={existing_cwd}. Use the same --cwd or close the session and create a new one.")
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(f"Session {session} is already bound to agent={existing_agent}. Close the session and create a new one for a different agent.")
    if existing_meta and session_launch_mode(existing_meta) != "native":
        raise OrcheError(f"Session {session} is already managed by orche open. Use orche open without raw agent args for managed sessions, or close the session and recreate it.")
    provided_cli_args = [str(value) for value in cli_args]
    existing_cli_args = native_cli_args_from_meta(existing_meta)
    if existing_meta and provided_cli_args and provided_cli_args != existing_cli_args:
        raise OrcheError(f"Session {session} is already bound to native args={existing_cli_args!r}. Use the same shortcut args or close the session and create a new one.")
    resolved_cli_args = existing_cli_args or provided_cli_args
    pane_id = ensure_pane(session, cwd, agent)
    pane_id = ensure_native_agent_running(plugin, session, cwd, pane_id, cli_args=resolved_cli_args)
    meta = load_meta(session)
    meta.update({"backend": BACKEND, "session": session, "cwd": str(cwd), "agent": agent, "pane_id": pane_id, "launch_mode": "native", "native_cli_args": list(resolved_cli_args), "last_seen_at": time.time(), "runtime_home": "", "runtime_home_managed": False, "runtime_label": "", "codex_home": "", "codex_home_managed": False, "parent_session": "", "last_event_at": 0.0, "last_event_source": "", "expires_after_seconds": 0})
    for key in ("discord_channel_id", "discord_session", "notify_routes", "notify_binding"):
        meta.pop(key, None)
    save_meta(session, meta)
    update_runtime_config(session=session, cwd=cwd, agent=agent, pane_id=pane_id, tmux_session=str(meta.get("tmux_session") or ""), runtime_home="", runtime_home_managed=False, runtime_label="")
    return pane_id


def ensure_session(session: str, cwd: Path, agent: str, *, approve_all: bool = False, runtime_home: Optional[str] = None, codex_home: Optional[str] = None, notify_to: Optional[str] = None, notify_target: Optional[str] = None) -> str:
    cwd = cwd.resolve()
    plugin = get_agent(agent)
    existing_meta = load_meta(session)
    if existing_meta and session_launch_mode(existing_meta) != "managed":
        raise OrcheError(f"Session {session} is already bound to native open mode. Reuse it through orche open with the same raw agent args, or close it and recreate it.")
    existing_cwd = Path(str(existing_meta.get("cwd") or "")).resolve() if existing_meta.get("cwd") else None
    if existing_cwd is not None and existing_cwd != cwd:
        raise OrcheError(f"Session {session} is already bound to cwd={existing_cwd}. Use the same --cwd or close the session and create a new one.")
    existing_agent = str(existing_meta.get("agent") or "").strip()
    if existing_agent and existing_agent != plugin.name:
        raise OrcheError(f"Session {session} is already bound to agent={existing_agent}. Close the session and create a new one for a different agent.")
    existing_notify_binding = _read_notify_binding(existing_meta)
    if (not notify_to or not notify_target) and not existing_notify_binding:
        raise OrcheError("managed sessions require both notify_to and notify_target")
    provided_notify_binding = build_notify_binding(notify_to, notify_target) if notify_to and notify_target else existing_notify_binding
    if existing_meta and provided_notify_binding != existing_notify_binding and existing_notify_binding:
        raise OrcheError(f"Session {session} is already bound to notify_to={existing_notify_binding['provider']} notify_target={existing_notify_binding['target']}. Use the same notify binding or close the session and create a new one.")
    resolved_notify_binding = existing_notify_binding or provided_notify_binding
    resolved_discord_channel_id = resolved_notify_binding.get("target") if resolved_notify_binding.get("provider") == "discord" else ""
    requested_runtime_home = runtime_home or codex_home
    managed_runtime_home = False
    if requested_runtime_home:
        runtime = AgentRuntime(home=str(requested_runtime_home), managed=False, label=plugin.runtime_label)
        resolved_runtime_home = str(requested_runtime_home)
    elif runtime_home_from_meta(existing_meta):
        resolved_runtime_home = runtime_home_from_meta(existing_meta)
        managed_runtime_home = runtime_home_managed_from_meta(existing_meta)
        runtime = AgentRuntime(home=resolved_runtime_home, managed=managed_runtime_home, label=runtime_label_from_meta(existing_meta, plugin))
    else:
        runtime = prepare_managed_runtime(plugin, session, cwd=cwd, discord_channel_id=resolved_discord_channel_id)
        resolved_runtime_home = runtime.home
        managed_runtime_home = True
    tmux_mode = str(existing_meta.get("tmux_mode") or "").strip() or "dedicated-session"
    host_pane_id = str(existing_meta.get("host_pane_id") or "").strip()
    tmux_host_session = str(existing_meta.get("tmux_host_session") or "").strip()
    parent_session = str(resolved_notify_binding.get("target") or "").strip() if tmux_mode == "inline-pane" and str(resolved_notify_binding.get("provider") or "").strip() == "tmux-bridge" else ""
    pane_id = ensure_pane(session, cwd, agent, tmux_mode=tmux_mode, host_pane_id=host_pane_id, tmux_host_session=tmux_host_session)
    meta = load_meta(session)
    meta.update({"backend": BACKEND, "session": session, "cwd": str(cwd), "agent": agent, "pane_id": pane_id, "launch_mode": "managed", "tmux_mode": tmux_mode, "host_pane_id": host_pane_id, "tmux_host_session": tmux_host_session, "last_seen_at": time.time(), "parent_session": parent_session, "last_event_at": time.time(), "last_event_source": "open", "expires_after_seconds": managed_session_ttl_seconds()})
    apply_runtime_to_meta(meta, agent=agent, runtime=runtime)
    for key in ("native_cli_args", "discord_channel_id", "discord_session", "notify_routes"):
        meta.pop(key, None)
    if resolved_notify_binding:
        meta["notify_binding"] = resolved_notify_binding
    save_meta(session, meta)
    wait_for_startup = False
    if plugin.name in {"claude", "codex"} and runtime.managed:
        startup = load_meta(session).get("startup") if isinstance(load_meta(session).get("startup"), dict) else {}
        if is_agent_running(plugin, pane_id):
            wait_for_startup = _managed_startup_reuse_wait_policy(session, plugin, pane_id, startup)
        else:
            initialize_session_startup(session)
            wait_for_startup = True
    pane_id = ensure_agent_running(plugin, session, cwd, pane_id, approve_all=approve_all, runtime=runtime, discord_channel_id=resolved_discord_channel_id)
    touch_session_event(session, source="open")
    if wait_for_startup:
        wait_for_managed_startup_ready(session, plugin, pane_id, cwd)
    elif plugin.name == "claude":
        wait_for_agent_ready(plugin, pane_id, cwd)
    update_runtime_config(session=session, cwd=cwd, agent=agent, pane_id=pane_id, tmux_session=str(load_meta(session).get("tmux_session") or ""), runtime_home=resolved_runtime_home, runtime_home_managed=managed_runtime_home, runtime_label=runtime.label)
    return pane_id


def send_prompt(session: str, cwd: Path, agent: str, prompt: str, *, approve_all: bool = False, pane_id: str = "") -> str:
    plugin = get_agent(agent)
    resolved_pane_id = str(pane_id or "").strip()
    meta = load_meta(session)
    if not resolved_pane_id:
        resolved_pane_id = ensure_native_session(session, cwd, agent, cli_args=native_cli_args_from_meta(meta)) if session_launch_mode(meta) == "native" else ensure_session(session, cwd, agent, approve_all=approve_all)
    meta = load_meta(session)
    wait_for_ack = plugin.name == "claude" and runtime_home_managed_from_meta(meta) and session_launch_mode(meta) != "native"
    meta["pending_turn"] = {"turn_id": uuid.uuid4().hex[:12], "prompt": prompt, "before_capture": read_pane(resolved_pane_id, DEFAULT_CAPTURE_LINES), "submitted_at": time.time(), "pane_id": resolved_pane_id, "notifications": {}, "prompt_ack": {"state": "pending", "accepted_at": 0.0, "source": ""}, "watchdog": {"state": "queued", "started_at": 0.0, "last_progress_at": time.time(), "last_sample_at": 0.0, "idle_samples": 0, "stop_requested": False}}
    save_meta(session, meta)
    touch_session_event(session, source="prompt-submit")
    plugin.submit_prompt(session, prompt, bridge=bridge_adapter())
    with contextlib.suppress(Exception):
        start_session_watchdog(session, turn_id=str(meta["pending_turn"]["turn_id"]))
    append_action_history(session, cwd, agent, "prompt", prompt=prompt, pane_id=resolved_pane_id)
    if wait_for_ack:
        wait_for_prompt_ack(session, turn_id=str(meta["pending_turn"]["turn_id"]), prompt=prompt, timeout=CLAUDE_PROMPT_ACK_TIMEOUT)
    return resolved_pane_id


def _completion_summary_from_capture(plugin, *, capture: str, before_capture: str, prompt: str) -> str:
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
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    if pending_turn:
        plugin = get_agent(str(meta.get("agent") or "codex"))
        prompt = str(pending_turn.get("prompt") or "")
        fallback_pane_id = str(pending_turn.get("pane_id") or meta.get("pane_id") or "").strip()
        pane_id = str(bridge_resolve(session, fallback_pane_id=fallback_pane_id) or fallback_pane_id).strip()
        summary = _completion_summary_from_capture(plugin, capture=read_pane(pane_id, DEFAULT_CAPTURE_LINES) if pane_id else "", before_capture=str(pending_turn.get("before_capture") or ""), prompt=prompt)
        if summary:
            complete_pending_turn(session, summary=summary)
            return summary
        return ""
    last_completed = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    return str(last_completed.get("summary") or "") if last_completed else ""


def build_status(session: str) -> Dict[str, Any]:
    meta = load_meta(session)
    if not meta:
        raise OrcheError(f"Unknown session: {session}")
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    pane_id = bridge_resolve(session, fallback_pane_id=fallback_pane_id) or fallback_pane_id
    info = get_pane_info(pane_id) if pane_id else None
    plugin = get_agent(str(meta.get("agent") or "codex"))
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else {}
    return {"backend": BACKEND, "session": session, "cwd": str(meta.get("cwd") or (info or {}).get("pane_current_path") or "-"), "agent": str(meta.get("agent") or "codex"), "runtime_home": runtime_home_from_meta(meta), "runtime_home_managed": runtime_home_managed_from_meta(meta), "runtime_label": runtime_label_from_meta(meta, plugin), "codex_home": str(meta.get("codex_home") or runtime_home_from_meta(meta)), "codex_home_managed": bool(meta.get("codex_home_managed") or runtime_home_managed_from_meta(meta)), "tmux_session": str((info or {}).get("session_name") or meta.get("tmux_session") or "").strip() or "-", "pane_id": pane_id or "-", "window_name": (info or {}).get("window_name", meta.get("window_name", "-")), "agent_running": bool(pane_id and is_agent_running(plugin, pane_id)), "codex_running": bool(pane_id and is_agent_running(plugin, pane_id)), "pane_exists": bool(pane_id and pane_exists(pane_id)), "discord_session": (_read_notify_binding(meta).get("session", "") if _read_notify_binding(meta).get("provider") == "discord" else ""), "notify_binding": _read_notify_binding(meta), "parent_session": session_parent(meta), "child_count": len(session_children(session, live_only=True)), "last_event_at": managed_session_last_event_at(meta), "ttl_seconds": int(meta.get("expires_after_seconds") or managed_session_ttl_seconds()), "ttl_exempt_because_parent_alive": bool(session_parent(meta) and session_metadata_is_live(session_parent(meta), load_meta(session_parent(meta)))), "pending_turn_id": str(pending_turn.get("turn_id") or ""), "pending_turn_submitted_at": float(pending_turn.get("submitted_at") or 0.0), "startup": dict(meta.get("startup") or {}), "prompt_ack": dict(pending_turn.get("prompt_ack") or {}), "watchdog": dict(pending_turn.get("watchdog") or {})}


def resolve_session_context(*, session: str, require_existing: bool = False, require_cwd_agent: bool = False) -> Tuple[Optional[Path], Optional[str], Dict[str, Any]]:
    meta = load_meta(session)
    cwd = Path(meta["cwd"]).resolve() if meta.get("cwd") else None
    agent = str(meta.get("agent")) if meta.get("agent") else None
    if require_existing and not meta:
        raise OrcheError(f"Unknown session: {session}")
    if require_cwd_agent and (cwd is None or agent is None):
        raise OrcheError(f"Session {session} is missing cwd/agent context; open it first")
    return cwd, agent, meta


def current_session_id() -> str:
    env_session = str(os.environ.get("ORCHE_SESSION") or "").strip()
    if env_session:
        return env_session
    raise OrcheError("Unable to resolve current orche session id. Set ORCHE_SESSION or run inside an orche tmux pane.")


def cancel_session(session: str) -> str:
    _cwd, agent, meta = resolve_session_context(session=session)
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    get_agent(agent or "codex").interrupt(session, bridge=bridge_adapter(session, fallback_pane_id=fallback_pane_id))
    return bridge_resolve(session, fallback_pane_id=fallback_pane_id) or "-"


def _close_session_single(session: str) -> str:
    meta = load_meta(session)
    if not meta:
        return "-"
    plugin = get_agent(str(meta.get("agent") or "codex"))
    fallback_pane_id = str(meta.get("pane_id") or "").strip()
    pane_id = bridge_resolve(session, fallback_pane_id=fallback_pane_id) or fallback_pane_id
    info = get_pane_info(pane_id) if pane_id and pane_exists(pane_id) else None
    target_tmux_session = str((info or {}).get("session_name") or meta.get("tmux_session") or "").strip() or ensure_tmux_session(session, Path(str(meta.get("cwd") or ".")))
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
        plugin.cleanup_runtime(AgentRuntime(home=runtime_home, managed=True, label=runtime_label_from_meta(meta, plugin)))
    remove_meta(session)
    return pane_id or "-"


def close_session_tree(session: str, *, reason: str = "", _visited: Optional[Set[str]] = None) -> str:
    session_name = str(session or "").strip()
    if not session_name:
        return "-"
    visited = _visited if _visited is not None else set()
    if session_name in visited:
        return "-"
    visited.add(session_name)
    fallback_pane_id = str(load_meta(session_name).get("pane_id") or "").strip()
    root_pane = bridge_resolve(session_name, fallback_pane_id=fallback_pane_id) or fallback_pane_id or "-"
    for child in session_children(session_name):
        close_session_tree(child, reason=reason, _visited=visited)
    _close_session_single(session_name)
    return root_pane


def close_session(session: str) -> str:
    return close_session_tree(session)


def bridge_adapter(session: str = "", *, fallback_pane_id: str = ""):
    class _Bridge:
        def type(self, target_session: str, text: str) -> None:
            resolved_session = target_session or session
            bridge_type(resolved_session, text, fallback_pane_id=fallback_pane_id)

        def keys(self, target_session: str, keys) -> None:
            resolved_session = target_session or session
            bridge_keys(resolved_session, keys, fallback_pane_id=fallback_pane_id)

    return _Bridge()
