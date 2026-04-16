from __future__ import annotations
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents import AgentPlugin, AgentRuntime, supported_agents
from agents.base import AgentConfig
from agents.claude import (
    DEFAULT_CLAUDE_COMMAND,
    DEFAULT_CLAUDE_SOURCE_CONFIG_PATH,
    DEFAULT_CLAUDE_SOURCE_HOME,
    ClaudeAgent,
)
from agents.codex import DEFAULT_CODEX_SOURCE_HOME, CodexAgent
from agents.common import (
    DEFAULT_RUNTIME_HOME_ROOT,
    ensure_orche_shim,
    normalize_runtime_home,
    remove_runtime_home,
)
from runtime.turn import mark_session_startup_ready, mark_session_startup_timeout
from session.config import (
    load_config,
    max_inline_sessions,
)
from session.meta import (
    _iter_meta_payloads,
    append_history_entry,
    inline_host_lock,
    load_meta,
    save_meta,
    session_lock,
    target_session_io_lock,
)
from session.ops import (
    _current_tmux_value,
    session_last_event_at,
    session_metadata_is_live,
    tmux_session_name,
    touch_session_event,
)
from session.pane import observable_progress_detected, sample_pane_state
from text_utils import window_name
from tmux.bridge import bridge_keys, bridge_name_pane, bridge_resolve, bridge_type
from tmux.client import process_descendants, tmux
from tmux.query import (
    DEFAULT_CAPTURE_LINES,
    _tmux_has_session,
    _tmux_join_fields,
    _tmux_split_fields,
    _tmux_window_index_in_use,
    get_pane_info,
    list_panes,
    next_window_index,
    pane_exists,
    read_pane,
)

BACKEND = "tmux"
DEFAULT_MAX_INLINE_SESSIONS = 4
STARTUP_TIMEOUT = 90.0
CLAUDE_STARTUP_GRACE_SECONDS = 2.0
LAUNCH_ERROR_PREFIX = "orche launch error:"
DEFAULT_CODEX_HOME_ROOT = DEFAULT_RUNTIME_HOME_ROOT


class AgentStartupBlockedError(RuntimeError):
    pass


def get_agent(name: str) -> AgentPlugin:
    key = str(name or "").strip().lower()
    config = load_config()
    claude_home_path = str(config.get("claude_home_path") or "").strip()
    claude_config_path = str(config.get("claude_config_path") or "").strip()
    runtime_home_root = str(config.get("runtime_home_root") or "").strip()
    source_home = str(config.get("source_home") or "").strip()
    plugin_config: AgentConfig = {
        "claude_command": str(config.get("claude_command") or "").strip()
        or DEFAULT_CLAUDE_COMMAND,
        "claude_home_path": Path(claude_home_path).expanduser()
        if claude_home_path
        else DEFAULT_CLAUDE_SOURCE_HOME,
        "claude_config_path": Path(claude_config_path).expanduser()
        if claude_config_path
        else DEFAULT_CLAUDE_SOURCE_CONFIG_PATH,
        "runtime_home_root": Path(runtime_home_root).expanduser()
        if runtime_home_root
        else DEFAULT_CODEX_HOME_ROOT,
        "source_home": Path(source_home).expanduser()
        if source_home
        else DEFAULT_CODEX_SOURCE_HOME,
    }
    if key == "codex":
        return CodexAgent(config=plugin_config)
    if key == "claude":
        return ClaudeAgent(config=plugin_config)
    supported = ", ".join(supported_agents())
    raise ValueError(f"Unsupported agent: {name}. Supported agents: {supported}")


def supported_agent_names() -> Tuple[str, ...]:
    return supported_agents()


def prepare_managed_runtime(
    plugin: AgentPlugin, session: str, *, cwd: Path, discord_channel_id: Optional[str]
) -> AgentRuntime:
    return plugin.ensure_managed_runtime(
        session, cwd=cwd, discord_channel_id=discord_channel_id
    )


def deliver_notify_to_session(session: str, prompt: str) -> str:
    with target_session_io_lock(session.strip()):
        target_meta = load_meta(session)
        fallback_pane_id = str(target_meta.get("pane_id") or "").strip()
        pane_id = bridge_resolve(session, fallback_pane_id=fallback_pane_id)
        if not pane_id:
            raise RuntimeError(f"notify target session not found: {session}")
        resolved_pane_id = str(pane_id)

        class _FallbackBridge:
            def type(self, session: str, text: str) -> None:
                bridge_type(session, text, fallback_pane_id=resolved_pane_id)

            def keys(self, session: str, keys: Sequence[str]) -> None:
                bridge_keys(session, list(keys), fallback_pane_id=resolved_pane_id)

        target_agent = str(target_meta.get("agent") or "").strip().lower() or "codex"
        get_agent(target_agent).submit_prompt(session, prompt, bridge=_FallbackBridge())
        return pane_id


def deliver_notify_to_pane(pane_id: str, prompt: str) -> str:
    resolved_pane_id = str(pane_id or "").strip()
    if not resolved_pane_id or not pane_exists(resolved_pane_id):
        raise RuntimeError(f"notify target pane not found: {resolved_pane_id or '-'}")
    buffer_name = f"orche-notify-{time.time_ns()}"
    try:
        tmux(
            "load-buffer",
            "-b",
            buffer_name,
            "-",
            check=True,
            capture=True,
            input_text=prompt,
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
    tmux("send-keys", "-t", resolved_pane_id, "Enter", check=True, capture=True)
    return resolved_pane_id


def runtime_home_from_meta(meta: Dict[str, Any]) -> str:
    return normalize_runtime_home(
        meta.get("runtime_home") or meta.get("codex_home") or ""
    )


def runtime_home_managed_from_meta(meta: Dict[str, Any]) -> bool:
    if "runtime_home_managed" in meta:
        return bool(meta.get("runtime_home_managed"))
    return bool(meta.get("codex_home_managed"))


def runtime_label_from_meta(meta: Dict[str, Any], plugin: AgentPlugin) -> str:
    return str(meta.get("runtime_label") or plugin.runtime_label)


def apply_runtime_to_meta(
    meta: Dict[str, Any], *, agent: str, runtime: AgentRuntime
) -> None:
    meta["runtime_home"] = normalize_runtime_home(runtime.home)
    meta["runtime_home_managed"] = bool(runtime.managed)
    meta["runtime_label"] = runtime.label
    if agent == "codex":
        meta["codex_home"] = meta["runtime_home"]
        meta["codex_home_managed"] = meta["runtime_home_managed"]
    else:
        meta["codex_home"] = ""
        meta["codex_home_managed"] = False


def _pane_record_from_tmux_output(output: str) -> Dict[str, str]:
    parts = _tmux_split_fields(output, expected=4)
    if len(parts) != 4:
        raise RuntimeError("Failed to parse tmux pane output")
    return {
        "session_name": parts[0],
        "pane_id": parts[1],
        "window_id": parts[2],
        "window_name": parts[3],
        "pane_dead": "0",
        "pane_pid": "",
        "pane_current_command": "",
        "pane_current_path": "",
        "pane_title": "",
    }


def create_dedicated_pane(session: str, cwd: Path) -> Dict[str, str]:
    tmux_name = tmux_session_name(session)
    if _tmux_has_session(tmux_name):
        panes = list_panes(tmux_name)
        if panes:
            return panes[0]
        raise RuntimeError(f"Failed to create tmux pane for {session}")
    result = tmux(
        "new-session",
        "-d",
        "-s",
        tmux_name,
        "-n",
        window_name(session),
        "-c",
        str(cwd),
        "-P",
        "-F",
        _tmux_join_fields(
            "#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}"
        ),
        check=True,
        capture=True,
    )
    return _pane_record_from_tmux_output(result.stdout)


def _preferred_host_pane(
    *, tmux_session: str, host_pane_id: str = "", exclude_pane_id: str = ""
) -> str:
    if host_pane_id and pane_exists(host_pane_id):
        return host_pane_id
    for pane in list_panes(tmux_session):
        pane_id = str(pane.get("pane_id") or "").strip()
        if (
            pane_id
            and pane_id != exclude_pane_id
            and str(pane.get("pane_dead") or "") != "1"
        ):
            return pane_id
    raise RuntimeError(
        f"Unable to find a live host pane in tmux session: {tmux_session}"
    )


def _inline_slot_value(value: Any) -> Optional[int]:
    try:
        slot = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return slot if 0 <= slot < DEFAULT_MAX_INLINE_SESSIONS else None


def _create_temp_inline_pane(*, tmux_session: str, cwd: Path) -> Dict[str, str]:
    last_error: Optional[subprocess.CalledProcessError] = None
    for _ in range(3):
        target = f"{tmux_session}:{next_window_index(tmux_session)}"
        try:
            result = tmux(
                "new-window",
                "-d",
                "-t",
                target,
                "-c",
                str(cwd),
                "-P",
                "-F",
                _tmux_join_fields(
                    "#{session_name}", "#{pane_id}", "#{window_id}", "#{window_name}"
                ),
                check=True,
                capture=True,
            )
            return _pane_record_from_tmux_output(result.stdout)
        except subprocess.CalledProcessError as exc:
            last_error = exc
            if not _tmux_window_index_in_use(exc):
                raise
    raise last_error or RuntimeError("Failed to create inline pane")


def _inline_group_sessions(
    *, tmux_session: str, host_pane_id: str, exclude_session: str = ""
) -> List[Dict[str, Any]]:
    members: List[Dict[str, Any]] = []
    for payload in _iter_meta_payloads():
        child_session = str(payload.get("session") or "").strip()
        if not child_session or child_session == exclude_session:
            continue
        if str(payload.get("tmux_mode") or "").strip() != "inline-pane":
            continue
        if str(payload.get("host_pane_id") or "").strip() != host_pane_id:
            continue
        host_session = str(
            payload.get("tmux_host_session") or payload.get("tmux_session") or ""
        ).strip()
        if host_session != tmux_session or not session_metadata_is_live(
            child_session, payload
        ):
            continue
        pane_id = str(payload.get("pane_id") or "").strip()
        if not pane_id or pane_id == host_pane_id or not pane_exists(pane_id):
            continue
        info = get_pane_info(pane_id)
        if (
            info is not None
            and str(info.get("session_name") or "").strip() == tmux_session
        ):
            member = dict(payload)
            member["pane_id"] = pane_id
            members.append(member)
    return members


def _normalize_inline_group_slots(
    group: Sequence[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    def sort_key(payload: Mapping[str, Any]) -> Tuple[int, float, str]:
        slot = _inline_slot_value(payload.get("inline_slot"))
        return (
            DEFAULT_MAX_INLINE_SESSIONS if slot is None else slot,
            session_last_event_at(payload, default=float("inf")),
            str(payload.get("session") or ""),
        )

    normalized: List[Dict[str, Any]] = []
    for index, payload in enumerate(sorted(group, key=sort_key)):
        member = dict(payload)
        member["inline_slot"] = index
        normalized.append(member)
        session_name = str(payload.get("session") or "").strip()
        if session_name and _inline_slot_value(payload.get("inline_slot")) != index:
            meta = load_meta(session_name)
            if meta:
                meta["inline_slot"] = index
                save_meta(session_name, meta)
    return normalized


def _reflow_inline_panes(
    *, host_pane_id: str, pane_ids_by_slot: Mapping[int, str]
) -> None:
    host_info = get_pane_info(host_pane_id)
    if host_info is None:
        raise RuntimeError(
            f"Unable to find host pane for inline layout: {host_pane_id}"
        )
    host_window_id = str(host_info.get("window_id") or "").strip()
    for pane_id in pane_ids_by_slot.values():
        if pane_id and pane_id != host_pane_id and pane_exists(pane_id):
            info = get_pane_info(pane_id)
            if (
                info is not None
                and str(info.get("window_id") or "").strip() == host_window_id
            ):
                tmux("break-pane", "-d", "-s", pane_id, check=True, capture=True)
    slot_0, slot_1, slot_2, slot_3 = (
        pane_ids_by_slot.get(index, "") for index in range(4)
    )
    pane_count = len({slot: pane for slot, pane in pane_ids_by_slot.items() if pane})
    if pane_count == 1 and slot_0:
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "25%",
            "-s",
            slot_0,
            "-t",
            host_pane_id,
            check=True,
            capture=True,
        )
        return
    if pane_count == 2 and slot_0 and slot_1:
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "25%",
            "-s",
            slot_0,
            "-t",
            host_pane_id,
            check=True,
            capture=True,
        )
        tmux(
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            slot_1,
            "-t",
            slot_0,
            check=True,
            capture=True,
        )
        return
    if pane_count == 3 and slot_0 and slot_1 and slot_2:
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            slot_2,
            "-t",
            host_pane_id,
            check=True,
            capture=True,
        )
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            slot_0,
            "-t",
            slot_2,
            check=True,
            capture=True,
        )
        tmux(
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            slot_1,
            "-t",
            slot_0,
            check=True,
            capture=True,
        )
        return
    if slot_0:
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            slot_0,
            "-t",
            host_pane_id,
            check=True,
            capture=True,
        )
    if slot_1:
        tmux(
            "join-pane",
            "-d",
            "-h",
            "-l",
            "50%",
            "-s",
            slot_1,
            "-t",
            slot_0,
            check=True,
            capture=True,
        )
    if slot_2 and slot_0:
        tmux(
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            slot_2,
            "-t",
            slot_0,
            check=True,
            capture=True,
        )
    if slot_3 and slot_1:
        tmux(
            "join-pane",
            "-d",
            "-v",
            "-l",
            "50%",
            "-s",
            slot_3,
            "-t",
            slot_1,
            check=True,
            capture=True,
        )


def create_inline_pane(
    session: str, cwd: Path, *, tmux_session: str, host_pane_id: str = ""
) -> Tuple[Dict[str, str], str]:
    resolved_host_pane = _preferred_host_pane(
        tmux_session=tmux_session, host_pane_id=host_pane_id
    )
    existing_group = _normalize_inline_group_slots(
        _inline_group_sessions(
            tmux_session=tmux_session,
            host_pane_id=resolved_host_pane,
            exclude_session=session,
        )
    )
    inline_limit = max_inline_sessions()
    if len(existing_group) >= inline_limit:
        raise RuntimeError(
            f"Inline pane limit reached for host pane {resolved_host_pane}: {inline_limit} session(s) max. Adjust inline.max-sessions (1-4) or close an existing inline session."
        )
    new_slot = len(existing_group)
    pane = _create_temp_inline_pane(tmux_session=tmux_session, cwd=cwd)
    try:
        pane_ids_by_slot = {
            int(member["inline_slot"]): str(member.get("pane_id") or "").strip()
            for member in existing_group
            if str(member.get("pane_id") or "").strip()
        }
        pane_ids_by_slot[new_slot] = pane["pane_id"]
        _reflow_inline_panes(
            host_pane_id=resolved_host_pane, pane_ids_by_slot=pane_ids_by_slot
        )
        info = get_pane_info(pane["pane_id"])
        if info is None:
            raise RuntimeError(f"Failed to create inline tmux pane for {session}")
        info["inline_slot"] = str(new_slot)
        return info, resolved_host_pane
    except Exception:
        if pane_exists(pane["pane_id"]):
            tmux("kill-pane", "-t", pane["pane_id"], check=False, capture=True)
        raise


def normalize_pane(session: str, cwd: Path, pane: Dict[str, str]) -> str:
    pane_id = pane["pane_id"]
    if pane.get("pane_dead") == "1":
        tmux(
            "respawn-pane",
            "-k",
            "-t",
            pane_id,
            "-c",
            str(cwd),
            check=True,
            capture=True,
        )
    bridge_name_pane(pane_id, session)
    return pane_id


def ensure_pane(
    session: str,
    cwd: Path,
    agent: str,
    *,
    tmux_mode: str = "dedicated-session",
    host_pane_id: str = "",
    tmux_host_session: str = "",
) -> str:
    cwd = cwd.resolve()
    with session_lock(session):
        meta = load_meta(session)
        resolved_tmux_mode = (
            str(meta.get("tmux_mode") or tmux_mode or "dedicated-session").strip()
            or "dedicated-session"
        )
        resolved_host_pane_id = str(
            meta.get("host_pane_id") or host_pane_id or ""
        ).strip()
        resolved_tmux_host_session = str(
            meta.get("tmux_host_session") or tmux_host_session or ""
        ).strip()
        pane_id = str(meta.get("pane_id") or "")
        if pane_id and pane_exists(pane_id):
            info = get_pane_info(pane_id)
            if info is not None:
                pane_id = normalize_pane(session, cwd, info)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "tmux_session": info["session_name"],
                        "pane_id": pane_id,
                        "window_id": info["window_id"],
                        "window_name": info["window_name"],
                        "tmux_mode": resolved_tmux_mode,
                        "host_pane_id": resolved_host_pane_id,
                        "tmux_host_session": resolved_tmux_host_session,
                        "last_seen_at": time.time(),
                    }
                )
                if resolved_tmux_mode != "inline-pane":
                    meta.pop("inline_slot", None)
                save_meta(session, meta)
                return pane_id
        if resolved_tmux_mode == "inline-pane":
            inline_tmux_session = (
                resolved_tmux_host_session
                or str(meta.get("tmux_session") or "").strip()
                or _current_tmux_value("#{session_name}")
            )
            if not inline_tmux_session:
                raise RuntimeError("Inline pane mode requires a live tmux session")
            inline_host_pane_id = resolved_host_pane_id or _current_tmux_value(
                "#{pane_id}"
            )
            with inline_host_lock(inline_tmux_session, inline_host_pane_id):
                pane, resolved_host_pane_id = create_inline_pane(
                    session,
                    cwd,
                    tmux_session=inline_tmux_session,
                    host_pane_id=inline_host_pane_id,
                )
                pane_id = normalize_pane(session, cwd, pane)
                meta.update(
                    {
                        "backend": BACKEND,
                        "session": session,
                        "cwd": str(cwd),
                        "agent": agent,
                        "tmux_session": pane["session_name"],
                        "pane_id": pane_id,
                        "window_id": pane["window_id"],
                        "window_name": pane["window_name"],
                        "tmux_mode": resolved_tmux_mode,
                        "host_pane_id": resolved_host_pane_id,
                        "tmux_host_session": resolved_tmux_host_session
                        or pane["session_name"],
                        "last_seen_at": time.time(),
                    }
                )
                inline_slot = str(pane.get("inline_slot") or "").strip()
                if inline_slot:
                    meta["inline_slot"] = int(inline_slot)
                save_meta(session, meta)
                return pane_id
        pane = create_dedicated_pane(session, cwd)
        pane_id = normalize_pane(session, cwd, pane)
        meta.update(
            {
                "backend": BACKEND,
                "session": session,
                "cwd": str(cwd),
                "agent": agent,
                "tmux_session": pane["session_name"],
                "pane_id": pane_id,
                "window_id": pane["window_id"],
                "window_name": pane["window_name"],
                "tmux_mode": resolved_tmux_mode,
                "host_pane_id": resolved_host_pane_id,
                "tmux_host_session": resolved_tmux_host_session or pane["session_name"],
                "last_seen_at": time.time(),
            }
        )
        if resolved_tmux_mode != "inline-pane":
            meta.pop("inline_slot", None)
        save_meta(session, meta)
        return pane_id


def is_agent_running(plugin: AgentPlugin, pane_id: str) -> bool:
    info = get_pane_info(pane_id)
    if info is None or info.get("pane_dead") == "1":
        return False
    command = str(info.get("pane_current_command") or "").lower()
    try:
        pane_pid = int(info.get("pane_pid") or "0")
    except ValueError:
        return False
    return plugin.matches_process(command, process_descendants(pane_pid))


def wait_for_agent_ready(
    plugin: AgentPlugin, pane_id: str, cwd: Path, *, timeout: float = STARTUP_TIMEOUT
) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    last_signature = ""
    last_cursor = ("", "")
    last_sample: Dict[str, Any] = {}
    while time.time() <= deadline:
        sample = sample_pane_state(plugin, pane_id, capture_lines=DEFAULT_CAPTURE_LINES)
        capture = str(sample.get("capture") or "")
        if any(prompt in capture for prompt in plugin.login_prompts):
            raise RuntimeError(
                f"{plugin.display_name} is not logged in inside the tmux pane"
            )
        if str(sample.get("pane_dead") or "") == "1":
            raise RuntimeError(
                f"{plugin.display_name} pane exited before becoming ready: {pane_id}"
            )
        ready_candidate = bool(
            sample.get("agent_running")
        ) and plugin.capture_has_ready_surface(capture, cwd)
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= plugin.ready_streak_required:
            return pane_id
        observable_progress_detected(last_signature, last_cursor, sample)
        last_signature = str(sample.get("signature") or "")
        last_cursor = (
            str(sample.get("cursor_x") or ""),
            str(sample.get("cursor_y") or ""),
        )
        last_sample = sample
        time.sleep(1.0)
    if bool(last_sample.get("agent_running")):
        raise AgentStartupBlockedError(
            f"{plugin.display_name} startup blocked before reaching ready state in {pane_id}"
        )
    raise RuntimeError(
        f"Timed out waiting for {plugin.display_name} to become ready in {pane_id}"
    )


def wait_for_managed_startup_ready(
    session: str,
    plugin: AgentPlugin,
    pane_id: str,
    cwd: Path,
    *,
    timeout: float = STARTUP_TIMEOUT,
) -> str:
    deadline = time.time() + timeout
    ready_streak = 0
    while time.time() <= deadline:
        meta = load_meta(session)
        raw_startup = meta.get("startup")
        startup: Dict[str, Any] = (
            dict(raw_startup) if isinstance(raw_startup, dict) else {}
        )
        startup_state = str(startup.get("state") or "").strip().lower()
        if startup_state == "ready":
            if plugin.name == "claude":
                ready_at = float(
                    startup.get("ready_at")
                    or startup.get("updated_at")
                    or startup.get("started_at")
                    or 0.0
                )
                if (
                    ready_at > 0
                    and (time.time() - ready_at) < CLAUDE_STARTUP_GRACE_SECONDS
                ):
                    time.sleep(0.1)
                    continue
            return pane_id
        if startup_state == "blocked":
            raise AgentStartupBlockedError(
                str(startup.get("blocked_reason") or "").strip()
                or f"{plugin.display_name} startup blocked before reaching ready state in {pane_id}"
            )
        if startup_state == "timeout":
            raise AgentStartupBlockedError(
                str(startup.get("blocked_reason") or "").strip()
                or f"{plugin.display_name} startup timed out before reaching ready state in {pane_id}"
            )
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        if any(prompt in capture for prompt in plugin.login_prompts):
            raise RuntimeError(
                f"{plugin.display_name} is not logged in inside the tmux pane"
            )
        ready_candidate = plugin.name == "codex" and plugin.capture_has_ready_surface(
            capture, cwd
        )
        ready_streak = ready_streak + 1 if ready_candidate else 0
        if ready_streak >= plugin.ready_streak_required:
            mark_session_startup_ready(session, source="ready-surface-fallback")
            return pane_id
        info = get_pane_info(pane_id)
        if info is None or info.get("pane_dead") == "1":
            raise RuntimeError(
                f"{plugin.display_name} pane exited before startup completed: {pane_id}"
            )
        if not is_agent_running(plugin, pane_id):
            raise RuntimeError(
                f"{plugin.display_name} process exited before startup completed: {pane_id}"
            )
        time.sleep(0.5)
    reason = f"Timed out waiting for {plugin.display_name} SessionStart(startup) hook in {pane_id}"
    mark_session_startup_timeout(session, reason=reason)
    raise RuntimeError(reason)


def _managed_startup_reuse_wait_policy(
    session: str, plugin: AgentPlugin, pane_id: str, startup: Mapping[str, Any]
) -> bool:
    startup_state = str(startup.get("state") or "").strip().lower()
    if not startup_state:
        mark_session_startup_ready(session, source="existing-running-process")
        return False
    if startup_state == "ready":
        return False
    if startup_state == "launching":
        return True
    if startup_state in {"blocked", "timeout"}:
        detail = str(startup.get("blocked_reason") or "").strip()
        if not detail:
            detail = f"{plugin.display_name} startup {'timed out' if startup_state == 'timeout' else 'blocked'} before reaching ready state in {pane_id}"
        raise RuntimeError(
            f"Session {session} is not ready because {detail}. Reopen or restart the session before sending prompts."
        )
    return False


def wait_for_agent_process_start(
    plugin: AgentPlugin, pane_id: str, *, timeout: float = STARTUP_TIMEOUT
) -> str:
    deadline = time.time() + timeout
    last_capture = ""
    while time.time() <= deadline:
        capture = read_pane(pane_id, DEFAULT_CAPTURE_LINES)
        last_capture = capture
        launch_error = extract_launch_error(capture)
        if launch_error:
            raise RuntimeError(launch_error)
        if any(prompt in capture for prompt in plugin.login_prompts):
            return pane_id
        info = get_pane_info(pane_id)
        if info is None or info.get("pane_dead") == "1":
            raise RuntimeError(
                extract_launch_error(last_capture)
                or f"{plugin.display_name} pane exited before launch completed: {pane_id}"
            )
        if is_agent_running(plugin, pane_id):
            return pane_id
        time.sleep(0.5)
    raise RuntimeError(
        extract_launch_error(last_capture)
        or f"Timed out waiting for {plugin.display_name} process to start in {pane_id}"
    )


def ensure_agent_running(
    plugin: AgentPlugin,
    session: str,
    cwd: Path,
    pane_id: str,
    *,
    runtime: Optional[AgentRuntime] = None,
    discord_channel_id: Optional[str] = None,
) -> str:
    if is_agent_running(plugin, pane_id):
        return pane_id
    if get_pane_info(pane_id) is None:
        raise RuntimeError(
            f"Pane disappeared before {plugin.display_name} launch: {pane_id}"
        )
    launch_command = plugin.build_launch_command(
        cwd=cwd,
        runtime=runtime or AgentRuntime(label=plugin.runtime_label),
        session=session,
        discord_channel_id=discord_channel_id,
    )
    tmux(
        "respawn-pane",
        "-k",
        "-t",
        pane_id,
        "-c",
        str(cwd),
        launch_command,
        check=True,
        capture=True,
    )
    pane_id = wait_for_agent_process_start(plugin, pane_id)
    bridge_name_pane(pane_id, session)
    meta = load_meta(session)
    meta.update(
        {
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "pane_id": pane_id,
            "agent_started_at": time.time(),
            "last_seen_at": time.time(),
        }
    )
    apply_runtime_to_meta(
        meta,
        agent=plugin.name,
        runtime=runtime or AgentRuntime(label=plugin.runtime_label),
    )
    save_meta(session, meta)
    return pane_id


def append_action_history(
    session: str, cwd: Path, agent: str, action: str, **fields: Any
) -> None:
    append_history_entry(
        session,
        {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "backend": BACKEND,
            "session": session,
            "cwd": str(cwd),
            "agent": agent,
            "action": action,
            **fields,
        },
    )
    if action != "close":
        touch_session_event(session, source=f"action:{action}")


def ensure_managed_codex_home(
    session: str, *, cwd: Path, discord_channel_id: Optional[str]
) -> Path:
    return Path(
        prepare_managed_runtime(
            get_agent("codex"), session, cwd=cwd, discord_channel_id=discord_channel_id
        ).home
    )


def ensure_managed_claude_home(
    session: str, *, cwd: Path, discord_channel_id: Optional[str]
) -> Path:
    return Path(
        prepare_managed_runtime(
            get_agent("claude"), session, cwd=cwd, discord_channel_id=discord_channel_id
        ).home
    )


def remove_managed_codex_home(codex_home: str) -> None:
    if codex_home:
        remove_runtime_home(codex_home)


def extract_launch_error(capture: str) -> str:
    for line in capture.splitlines():
        text = str(line or "").strip()
        if text.startswith(LAUNCH_ERROR_PREFIX):
            return text[len(LAUNCH_ERROR_PREFIX) :].strip()
    return ""
