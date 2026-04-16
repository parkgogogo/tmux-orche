from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from agents.common import normalize_runtime_home

from .types import NotifyBinding, SessionMeta, StartupState, as_startup_state


def apply_session_open_state(
    meta: SessionMeta,
    *,
    session: str,
    cwd: str,
    agent: str,
    pane_id: str,
    tmux_mode: str,
    host_pane_id: str,
    tmux_host_session: str,
    parent_session: str,
    expires_after_seconds: int,
    backend: str,
    timestamp: float,
) -> SessionMeta:
    meta.update(
        {
            "backend": backend,
            "session": session,
            "cwd": cwd,
            "agent": agent,
            "pane_id": pane_id,
            "tmux_mode": tmux_mode,
            "host_pane_id": host_pane_id,
            "tmux_host_session": tmux_host_session,
            "last_seen_at": timestamp,
            "parent_session": parent_session,
            "last_event_at": timestamp,
            "last_event_source": "open",
            "expires_after_seconds": expires_after_seconds,
        }
    )
    return meta


def apply_session_pane_state(
    meta: SessionMeta,
    *,
    session: str,
    cwd: str,
    agent: str,
    tmux_session: str,
    pane_id: str,
    window_id: str,
    window_name: str,
    tmux_mode: str,
    host_pane_id: str,
    tmux_host_session: str,
    last_seen_at: float,
    backend: str,
    inline_slot: int | None = None,
) -> SessionMeta:
    meta.update(
        {
            "backend": backend,
            "session": session,
            "cwd": cwd,
            "agent": agent,
            "tmux_session": tmux_session,
            "pane_id": pane_id,
            "window_id": window_id,
            "window_name": window_name,
            "tmux_mode": tmux_mode,
            "host_pane_id": host_pane_id,
            "tmux_host_session": tmux_host_session,
            "last_seen_at": last_seen_at,
        }
    )
    set_inline_slot(meta, inline_slot)
    return meta


def apply_agent_launch_state(
    meta: SessionMeta,
    *,
    session: str,
    cwd: str,
    pane_id: str,
    agent_started_at: float,
    last_seen_at: float,
    backend: str,
) -> SessionMeta:
    meta.update(
        {
            "backend": backend,
            "session": session,
            "cwd": cwd,
            "pane_id": pane_id,
            "agent_started_at": agent_started_at,
            "last_seen_at": last_seen_at,
        }
    )
    return meta


def apply_runtime_state(
    meta: SessionMeta,
    *,
    agent: str,
    runtime_home: str,
    runtime_home_managed: bool,
    runtime_label: str,
) -> SessionMeta:
    normalized_runtime_home = normalize_runtime_home(runtime_home)
    meta["runtime_home"] = normalized_runtime_home
    meta["runtime_home_managed"] = bool(runtime_home_managed)
    meta["runtime_label"] = runtime_label
    if agent == "codex":
        meta["codex_home"] = normalized_runtime_home
        meta["codex_home_managed"] = bool(runtime_home_managed)
    else:
        meta["codex_home"] = ""
        meta["codex_home_managed"] = False
    return meta


def clear_legacy_notify_state(meta: SessionMeta) -> SessionMeta:
    for key in ("discord_channel_id", "discord_session", "notify_routes"):
        meta.pop(key, None)
    return meta


def set_notify_binding(
    meta: SessionMeta, notify_binding: Mapping[str, object] | None
) -> SessionMeta:
    if notify_binding:
        meta["notify_binding"] = cast(NotifyBinding, dict(notify_binding))
    else:
        meta.pop("notify_binding", None)
    return meta


def touch_session_meta(
    meta: SessionMeta,
    *,
    timestamp: float,
    source: str,
    expires_after_seconds: int,
) -> SessionMeta:
    meta["last_event_at"] = timestamp
    meta["last_event_source"] = str(source or "").strip()
    meta["expires_after_seconds"] = expires_after_seconds
    return meta


def initialize_startup_state(
    meta: SessionMeta, *, state: str = "launching", timestamp: float
) -> StartupState:
    startup: StartupState = {
        "state": state,
        "started_at": timestamp,
        "ready_at": 0.0,
        "ready_source": "",
        "blocked_at": 0.0,
        "blocked_reason": "",
        "blocked_event": "",
        "updated_at": timestamp,
    }
    meta["startup"] = startup
    return startup


def mark_startup_ready(
    meta: SessionMeta, *, source: str, timestamp: float
) -> StartupState:
    startup = as_startup_state(meta.get("startup"))
    startup.update(
        {
            "state": "ready",
            "ready_at": float(startup.get("ready_at") or timestamp),
            "ready_source": str(source or "").strip(),
            "blocked_at": 0.0,
            "blocked_reason": "",
            "blocked_event": "",
            "updated_at": timestamp,
        }
    )
    if not startup.get("started_at"):
        startup["started_at"] = timestamp
    meta["startup"] = startup
    return startup


def mark_startup_timeout(
    meta: SessionMeta, *, reason: str = "", timestamp: float
) -> StartupState:
    startup = as_startup_state(meta.get("startup"))
    startup.update(
        {
            "state": "timeout",
            "blocked_reason": str(
                reason or startup.get("blocked_reason") or ""
            ).strip(),
            "updated_at": timestamp,
        }
    )
    if not startup.get("started_at"):
        startup["started_at"] = timestamp
    meta["startup"] = startup
    return startup


def mark_startup_blocked_if_launching(
    meta: SessionMeta, *, reason: str, event_name: str, timestamp: float
) -> tuple[StartupState, bool]:
    startup = as_startup_state(meta.get("startup"))
    state = str(startup.get("state") or "").strip().lower()
    if state != "launching":
        return startup, False
    blocked_reason = str(reason or "").strip()
    blocked_event = str(event_name or "").strip()
    changed = (
        str(startup.get("blocked_reason") or "") != blocked_reason
        or str(startup.get("blocked_event") or "") != blocked_event
        or state != "blocked"
    )
    startup.update(
        {
            "state": "blocked",
            "blocked_at": timestamp,
            "blocked_reason": blocked_reason,
            "blocked_event": blocked_event,
            "updated_at": timestamp,
        }
    )
    if not startup.get("started_at"):
        startup["started_at"] = timestamp
    meta["startup"] = startup
    return startup, changed


def set_inline_slot(meta: SessionMeta, inline_slot: int | None) -> SessionMeta:
    if inline_slot is None:
        meta.pop("inline_slot", None)
    else:
        meta["inline_slot"] = int(inline_slot)
    return meta
