from __future__ import annotations

import contextlib
import os
import signal
import time
from collections.abc import Mapping
from typing import Any, Dict, Tuple

from session.ops import touch_session_event
from session.session_state import (
    initialize_startup_state,
    mark_startup_blocked_if_launching,
    mark_startup_ready,
    mark_startup_timeout,
)
from session.store import load_meta, save_meta, session_lock
from session.turn_state import (
    claim_turn_notification_in_meta,
    complete_pending_turn_state,
    release_turn_notification_in_meta,
    update_pending_turn_watchdog,
)
from session.turn_state import (
    mark_pending_turn_prompt_accepted as mark_pending_turn_prompt_accepted_in_meta,
)
from session.types import TurnRecord, WatchdogState, as_prompt_ack_state, as_turn_record
from tmux.client import process_is_alive

CLAUDE_PROMPT_ACK_POLL_INTERVAL = 0.1
CLAUDE_PROMPT_ACK_TIMEOUT = 15.0


def _turn_matches(
    turn: Mapping[str, Any], *, turn_id: str = "", prompt: str = ""
) -> bool:
    return bool(
        (turn_id and str(turn.get("turn_id") or "") == str(turn_id))
        or (prompt and str(turn.get("prompt") or "") == str(prompt))
    )


def _current_turn_entry(
    meta: Mapping[str, object],
    turn_id: str = "",
    *,
    prompt: str = "",
    allow_fallback: bool = True,
) -> Tuple[str, TurnRecord]:
    pending_turn = as_turn_record(meta.get("pending_turn"))
    last_completed_turn = as_turn_record(meta.get("last_completed_turn"))
    if turn_id or prompt:
        if pending_turn and _turn_matches(pending_turn, turn_id=turn_id, prompt=prompt):
            return "pending_turn", as_turn_record(pending_turn)
        if last_completed_turn and _turn_matches(
            last_completed_turn, turn_id=turn_id, prompt=prompt
        ):
            return "last_completed_turn", as_turn_record(last_completed_turn)
        if not allow_fallback:
            return "", {}
    if pending_turn:
        return "pending_turn", as_turn_record(pending_turn)
    if last_completed_turn:
        return "last_completed_turn", as_turn_record(last_completed_turn)
    return "", {}


def initialize_session_startup(
    session: str, *, state: str = "launching", started_at: float | None = None
) -> Dict[str, Any]:
    timestamp = started_at if started_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = initialize_startup_state(meta, state=state, timestamp=timestamp)
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:{state}")
    return dict(startup)


def mark_session_startup_ready(session: str, *, source: str) -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = mark_startup_ready(meta, source=source, timestamp=timestamp)
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:ready:{source}")
    return dict(startup)


def mark_session_startup_timeout(session: str, *, reason: str = "") -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        startup = mark_startup_timeout(meta, reason=reason, timestamp=timestamp)
        save_meta(session, meta)
    touch_session_event(session, source="startup:timeout")
    return dict(startup)


def mark_session_startup_blocked(
    session: str, *, reason: str, event_name: str
) -> Tuple[Dict[str, Any], bool]:
    timestamp = time.time()
    blocked_event = str(event_name or "").strip()
    with session_lock(session):
        meta = load_meta(session)
        startup, changed = mark_startup_blocked_if_launching(
            meta,
            reason=reason,
            event_name=blocked_event,
            timestamp=timestamp,
        )
        save_meta(session, meta)
    touch_session_event(session, source=f"startup:blocked:{blocked_event or 'unknown'}")
    return dict(startup), changed


def mark_pending_turn_prompt_accepted(
    session: str, *, source: str = "user-prompt-submit"
) -> Dict[str, Any]:
    timestamp = time.time()
    with session_lock(session):
        meta = load_meta(session)
        prompt_ack = mark_pending_turn_prompt_accepted_in_meta(
            meta, source=source, timestamp=timestamp
        )
        if not prompt_ack:
            return {}
        save_meta(session, meta)
    touch_session_event(session, source=f"prompt-ack:{source}")
    return dict(prompt_ack)


def wait_for_prompt_ack(
    session: str,
    *,
    turn_id: str,
    prompt: str,
    timeout: float = CLAUDE_PROMPT_ACK_TIMEOUT,
) -> Dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() <= deadline:
        meta = load_meta(session)
        _turn_key, turn = _current_turn_entry(
            meta, turn_id=turn_id, prompt=prompt, allow_fallback=False
        )
        prompt_ack = as_prompt_ack_state(turn.get("prompt_ack"))
        if str(prompt_ack.get("state") or "").strip().lower() == "accepted":
            return dict(prompt_ack)
        time.sleep(CLAUDE_PROMPT_ACK_POLL_INTERVAL)
    raise RuntimeError(
        f"Timed out waiting for Claude to accept the prompt in {session}; the prompt may have been submitted before the TUI was ready"
    )


def claim_turn_notification(
    session: str,
    event: str,
    *,
    turn_id: str = "",
    prompt: str = "",
    source: str = "",
    status: str = "",
    summary: str = "",
    notification_key: str = "",
) -> bool:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return True
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(
            turn_id or prompt
        )
        turn_key, turn = _current_turn_entry(
            meta, turn_id=turn_id, prompt=prompt, allow_fallback=not strict_match
        )
        if not turn_key or not turn:
            return True
        claimed = claim_turn_notification_in_meta(
            meta,
            turn_key=turn_key,
            turn=turn,
            notification_key=normalized_event,
            timestamp=time.time(),
            source=source,
            status=status,
            summary=summary,
        )
        if not claimed:
            return False
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-claim:{normalized_event}")
    return True


def release_turn_notification(
    session: str,
    event: str,
    *,
    turn_id: str = "",
    prompt: str = "",
    notification_key: str = "",
) -> None:
    normalized_event = str(notification_key or event or "").strip().lower()
    if not session or not normalized_event:
        return
    with session_lock(session):
        meta = load_meta(session)
        strict_match = str(event or "").strip().lower() == "completed" and bool(
            turn_id or prompt
        )
        turn_key, turn = _current_turn_entry(
            meta, turn_id=turn_id, prompt=prompt, allow_fallback=not strict_match
        )
        if not turn_key or not turn:
            return
        released = release_turn_notification_in_meta(
            meta,
            turn_key=turn_key,
            turn=turn,
            notification_key=normalized_event,
        )
        if not released:
            return
        save_meta(session, meta)
    touch_session_event(session, source=f"notify-release:{normalized_event}")


def update_watchdog_metadata(
    session: str, *, turn_id: str, values: Mapping[str, Any]
) -> WatchdogState:
    with session_lock(session):
        meta = load_meta(session)
        watchdog = update_pending_turn_watchdog(meta, turn_id=turn_id, values=values)
        if not watchdog:
            return {}
        save_meta(session, meta)
    touch_session_event(session, source="watchdog")
    return watchdog


def complete_pending_turn(
    session: str,
    *,
    summary: str = "",
    turn_id: str = "",
    prompt: str = "",
    completed_at: float | None = None,
) -> Dict[str, Any]:
    finished_at = completed_at if completed_at is not None else time.time()
    with session_lock(session):
        meta = load_meta(session)
        completed, pid = complete_pending_turn_state(
            meta,
            summary=summary,
            turn_id=turn_id,
            prompt=prompt,
            completed_at=finished_at,
        )
        if not completed:
            return {}
        save_meta(session, meta)
    touch_session_event(session, source="turn-complete")
    if pid > 0 and process_is_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    return dict(completed)
