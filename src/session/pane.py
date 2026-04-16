from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict

from agents import AgentPlugin
from text_utils import compact_text
from tmux.bridge import bridge_resolve
from tmux.client import process_cpu_percent, process_descendants
from tmux.query import (
    DEFAULT_CAPTURE_LINES,
    get_pane_info,
    pane_cursor_state,
    read_pane,
)

from .store import load_meta

WATCHDOG_ACTIVE_CPU_THRESHOLD = 5.0
WATCHDOG_CAPTURE_LINES = DEFAULT_CAPTURE_LINES
NOTIFY_TAIL_LINES = 20


def _normalize_watchdog_tail(capture: str) -> str:
    return compact_text("\n".join(line.rstrip() for line in capture.splitlines()[-12:]))


def _pane_signature(
    *,
    tail: str,
    cursor_x: str,
    cursor_y: str,
    pane_in_mode: str,
    pane_current_command: str,
) -> str:
    return "|".join([tail, cursor_x, cursor_y, pane_in_mode, pane_current_command])


def _plugin_agent_running(plugin: AgentPlugin, pane_id: str) -> bool:
    info = get_pane_info(pane_id)
    if info is None or str(info.get("pane_dead") or "") == "1":
        return False
    command = str(info.get("pane_current_command") or "").lower()
    try:
        pane_pid = int(info.get("pane_pid") or "0")
    except ValueError:
        return False
    return plugin.matches_process(command, process_descendants(pane_pid))


def sample_pane_state(
    plugin: AgentPlugin,
    pane_id: str,
    *,
    capture_lines: int = WATCHDOG_CAPTURE_LINES,
    agent_running_fn=None,
) -> Dict[str, Any]:
    resolved_pane_id = str(pane_id or "").strip()
    info = get_pane_info(resolved_pane_id) if resolved_pane_id else None
    capture = read_pane(resolved_pane_id, capture_lines) if resolved_pane_id else ""
    cursor = pane_cursor_state(resolved_pane_id) if resolved_pane_id else {}
    tail = _normalize_watchdog_tail(capture)
    return {
        "pane_id": resolved_pane_id,
        "capture": capture,
        "capture_bytes": len(capture.encode("utf-8")),
        "tail": tail,
        "signature": _pane_signature(
            tail=tail,
            cursor_x=str(cursor.get("cursor_x") or ""),
            cursor_y=str(cursor.get("cursor_y") or ""),
            pane_in_mode=str(cursor.get("pane_in_mode") or ""),
            pane_current_command=str((info or {}).get("pane_current_command") or ""),
        ),
        "cursor_x": str(cursor.get("cursor_x") or ""),
        "cursor_y": str(cursor.get("cursor_y") or ""),
        "pane_in_mode": str(cursor.get("pane_in_mode") or ""),
        "pane_dead": str(
            cursor.get("pane_dead") or (info or {}).get("pane_dead") or ""
        ),
        "pane_current_command": str((info or {}).get("pane_current_command") or ""),
        "cpu_percent": process_cpu_percent((info or {}).get("pane_pid", "")),
        "agent_running": bool(
            resolved_pane_id
            and (
                agent_running_fn(plugin, resolved_pane_id)
                if agent_running_fn is not None
                else _plugin_agent_running(plugin, resolved_pane_id)
            )
        ),
    }


def sample_watchdog_state(
    session: str,
    *,
    pane_id: str = "",
    bridge_resolve_fn=None,
    get_agent_fn,
    agent_running_fn=None,
    load_meta_fn=None,
) -> Dict[str, Any]:
    meta_loader = load_meta_fn or load_meta
    resolve_bridge = bridge_resolve_fn or bridge_resolve
    resolve_agent = get_agent_fn
    meta: Dict[str, Any] = dict(meta_loader(session))
    raw_pending_turn = meta.get("pending_turn")
    pending_turn: Dict[str, Any] = (
        dict(raw_pending_turn) if isinstance(raw_pending_turn, dict) else {}
    )
    fallback_pane_id = str(
        pending_turn.get("pane_id") or meta.get("pane_id") or ""
    ).strip()
    resolved_pane_id = str(
        pane_id
        or resolve_bridge(session, fallback_pane_id=fallback_pane_id)
        or fallback_pane_id
    ).strip()
    plugin = resolve_agent(str(meta.get("agent") or "codex"))
    return sample_pane_state(
        plugin,
        resolved_pane_id,
        capture_lines=WATCHDOG_CAPTURE_LINES,
        agent_running_fn=agent_running_fn,
    )


def observable_progress_detected(
    previous_signature: str, previous_cursor: tuple[str, str], sample: Mapping[str, Any]
) -> bool:
    current_cursor = (
        str(sample.get("cursor_x") or ""),
        str(sample.get("cursor_y") or ""),
    )
    return (
        not previous_signature
        or previous_signature != str(sample.get("signature") or "")
        or previous_cursor != current_cursor
        or float(sample.get("cpu_percent") or 0.0) >= WATCHDOG_ACTIVE_CPU_THRESHOLD
    )


def recent_capture_excerpt(
    capture: str, *, lines: int = NOTIFY_TAIL_LINES, max_chars: int = 1200
) -> str:
    excerpt = "\n".join(capture.splitlines()[-max(lines, 1) :]).strip()
    if len(excerpt) <= max_chars:
        return excerpt
    trimmed = excerpt[-max_chars:].lstrip()
    return f"...\n{trimmed}" if trimmed else ""
