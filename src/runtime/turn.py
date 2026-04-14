from __future__ import annotations

import contextlib
import os
import signal
import time
from typing import Any, Dict, Mapping, Tuple

from session.meta import load_meta, save_meta, session_lock
from session.ops import touch_session_event
from text_utils import shorten
from tmux.client import process_is_alive


CLAUDE_PROMPT_ACK_POLL_INTERVAL = 0.1
CLAUDE_PROMPT_ACK_TIMEOUT = 15.0


def _turn_matches(turn: Mapping[str, Any], *, turn_id: str = "", prompt: str = "") -> bool:
    return (turn_id and str(turn.get("turn_id") or "") == str(turn_id)) or (prompt and str(turn.get("prompt") or "") == str(prompt))


def _current_turn_entry(meta: Mapping[str, Any], turn_id: str = "", *, prompt: str = "", allow_fallback: bool = True) -> Tuple[str, Dict[str, Any]]:
    pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
    last_completed_turn = meta.get("last_completed_turn") if isinstance(meta.get("last_completed_turn"), dict) else None
    if turn_id or prompt:
        if pending_turn and _turn_matches(pending_turn, turn_id=turn_id, prompt=prompt):
            return "pending_turn", dict(pending_turn)
        if last_completed_turn and _turn_matches(last_completed_turn, turn_id=turn_id, prompt=prompt):
            return "last_completed_turn", dict(last_completed_turn)
        if not allow_fallback:
            return "", {}
    if pending_turn:
        return "pending_turn", dict(pending_turn)
    if last_completed_turn:
        return "last_completed_turn", dict(last_completed_turn)
    return "", {}


def initialize_session_startup(session: str, *, state: str = "launching", started_at: float | None = None) -> Dict[str, Any]:
    timestamp = started_at if started_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = {"state": state, "started_at": timestamp, "ready_at": 0.0, "ready_source": "", "blocked_at": 0.0, "blocked_reason": "", "blocked_event": "", "updated_at": timestamp}
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:{state}")
    return dict(startup)


def mark_session_startup_ready(session: str, *, source: str) -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        startup.update({"state": "ready", "ready_at": float(startup.get("ready_at") or timestamp), "ready_source": str(source or "").strip(), "blocked_at": 0.0, "blocked_reason": "", "blocked_event": "", "updated_at": timestamp})
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:ready:{source}")
    return dict(startup)


def mark_session_startup_timeout(session: str, *, reason: str = "") -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        startup.update({"state": "timeout", "blocked_reason": str(reason or startup.get("blocked_reason") or "").strip(), "updated_at": timestamp})
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source="startup:timeout")
    return dict(startup)


def mark_session_startup_blocked(session: str, *, reason: str, event_name: str) -> Tuple[Dict[str, Any], bool]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = dict(meta.get("startup") or {})
        state = str(startup.get("state") or "").strip().lower()
        if state != "launching":
            return startup, False
        blocked_reason = str(reason or "").strip()
        blocked_event = str(event_name or "").strip()
        changed = str(startup.get("blocked_reason") or "") != blocked_reason or str(startup.get("blocked_event") or "") != blocked_event or state != "blocked"
        startup.update({"state": "blocked", "blocked_at": timestamp, "blocked_reason": blocked_reason, "blocked_event": blocked_event, "updated_at": timestamp})
        if not startup.get("started_at"):
            startup["started_at"] = timestamp
        meta["startup"] = startup
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:blocked:{blocked_event or 'unknown'}")
    return dict(startup), changed


def mark_pending_turn_prompt_accepted(session: str, *, source: str = "user-prompt-submit") -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn:
            return {}
        prompt_ack = dict(pending_turn.get("prompt_ack") or {})
        prompt_ack.update({"state": "accepted", "accepted_at": timestamp, "source": str(source or "").strip()})
        pending_turn["prompt_ack"] = prompt_ack
        meta["pending_turn"] = pending_turn
        save_meta(session, meta)
    touch_session_event(session, source=f"prompt-ack:{source}")
    return dict(prompt_ack)


def wait_for_prompt_ack(session: str, *, turn_id: str, prompt: str, timeout: float = CLAUDE_PROMPT_ACK_TIMEOUT) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        meta = load_meta(session)
        _turn_key, turn = _current_turn_entry(meta, turn_id=turn_id, prompt=prompt, allow_fallback=False)
        prompt_ack = turn.get("prompt_ack") if isinstance(turn.get("prompt_ack"), dict) else {}
        if str(prompt_ack.get("state") or "").strip().lower() == "accepted":
            return dict(prompt_ack)
        time.sleep(CLAUDE_PROMPT_ACK_POLL_INTERVAL)
    raise RuntimeError(f"Timed out waiting for Claude to accept the prompt in {session}; the prompt may have been submitted before the TUI was ready")


def claim_turn_notification(session: str, event: str, *, turn_id: str = "", prompt: str = "", source: str = "", status: str = "", summary: str = "", notification_key: str = "") -> bool:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return True
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(turn_id or prompt)
        turn_key, turn = _current_turn_entry(meta, turn_id=turn_id, prompt=prompt, allow_fallback=not strict_match)
        if not turn_key or not turn:
            return True
        notifications = turn.get("notifications") if isinstance(turn.get("notifications"), dict) else {}
        if normalized_event in notifications:
            return False
        notifications[normalized_event] = {"at": time.time(), "source": str(source or "").strip(), "status": str(status or "").strip(), "summary": shorten(summary, 400)}
        turn["notifications"] = notifications
        meta[turn_key] = turn
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-claim:{normalized_event}")
    return True


def release_turn_notification(session: str, event: str, *, turn_id: str = "", prompt: str = "", notification_key: str = "") -> None:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(turn_id or prompt)
        turn_key, turn = _current_turn_entry(meta, turn_id=turn_id, prompt=prompt, allow_fallback=not strict_match)
        if not turn_key or not turn:
            return
        notifications = turn.get("notifications")
        if not isinstance(notifications, dict) or normalized_event not in notifications:
            return
        notifications.pop(normalized_event, None)
        turn["notifications"] = notifications
        meta[turn_key] = turn
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-release:{normalized_event}")


def update_watchdog_metadata(session: str, *, turn_id: str, values: Mapping[str, Any]) -> Dict[str, Any]:
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn or str(pending_turn.get("turn_id") or "") != turn_id:
            return {}
        watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
        watchdog.update(values)
        pending_turn["watchdog"] = watchdog
        meta["pending_turn"] = pending_turn
        save_meta(session, meta)
    touch_session_event(session, source="watchdog")
    return dict(watchdog)


def complete_pending_turn(session: str, *, summary: str = "", turn_id: str = "", prompt: str = "", completed_at: float | None = None) -> Dict[str, Any]:
    finished_at = completed_at if completed_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        pending_turn = meta.get("pending_turn") if isinstance(meta.get("pending_turn"), dict) else None
        if not pending_turn:
            return {}
        pending_turn_id = str(pending_turn.get("turn_id") or "")
        pending_prompt = str(pending_turn.get("prompt") or "")
        if turn_id and pending_turn_id and pending_turn_id != str(turn_id) and (not prompt or pending_prompt != str(prompt)):
            return {}
        watchdog = pending_turn.get("watchdog") if isinstance(pending_turn.get("watchdog"), dict) else {}
        pid = int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
        completed = dict(pending_turn)
        if summary:
            completed["summary"] = summary
        completed["completed_at"] = finished_at
        meta["last_completed_turn"] = completed
        meta.pop("pending_turn", None)
        save_meta(session, meta)
    touch_session_event(session, source="turn-complete")
    if pid > 0 and process_is_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    return completed
