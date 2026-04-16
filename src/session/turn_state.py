from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from text_utils import shorten

from .types import (
    NotificationMap,
    NotificationRecord,
    PromptAckState,
    SessionMeta,
    TurnRecord,
    WatchdogState,
    as_prompt_ack_state,
    as_turn_record,
    as_watchdog_state,
)


def begin_pending_turn(
    meta: SessionMeta,
    *,
    prompt: str,
    pane_id: str,
    before_capture: str,
    submitted_at: float,
) -> TurnRecord:
    pending_turn: TurnRecord = {
        "turn_id": uuid_hex(12),
        "prompt": prompt,
        "before_capture": before_capture,
        "submitted_at": submitted_at,
        "pane_id": pane_id,
        "notifications": {},
        "prompt_ack": {"state": "pending", "accepted_at": 0.0, "source": ""},
        "watchdog": {
            "state": "queued",
            "started_at": 0.0,
            "last_progress_at": submitted_at,
            "last_sample_at": 0.0,
            "idle_samples": 0,
            "stop_requested": False,
        },
    }
    meta["pending_turn"] = pending_turn
    return pending_turn


def mark_pending_turn_prompt_accepted(
    meta: SessionMeta, *, source: str, timestamp: float
) -> PromptAckState:
    pending_turn = as_turn_record(meta.get("pending_turn"))
    if not pending_turn:
        return {}
    prompt_ack = as_prompt_ack_state(pending_turn.get("prompt_ack"))
    prompt_ack.update(
        {
            "state": "accepted",
            "accepted_at": timestamp,
            "source": str(source or "").strip(),
        }
    )
    updated_turn = cast(TurnRecord, dict(pending_turn))
    updated_turn["prompt_ack"] = prompt_ack
    meta["pending_turn"] = updated_turn
    return prompt_ack


def claim_turn_notification_in_meta(
    meta: SessionMeta,
    *,
    turn_key: str,
    turn: Mapping[str, object],
    notification_key: str,
    timestamp: float,
    source: str = "",
    status: str = "",
    summary: str = "",
) -> bool:
    raw_notifications = turn.get("notifications")
    notifications: NotificationMap = cast(
        NotificationMap,
        dict(raw_notifications) if isinstance(raw_notifications, dict) else {},
    )
    if notification_key in notifications:
        return False
    notifications[notification_key] = NotificationRecord(
        at=timestamp,
        source=str(source or "").strip(),
        status=str(status or "").strip(),
        summary=shorten(summary, 400),
    )
    updated_turn = cast(TurnRecord, dict(turn))
    updated_turn["notifications"] = notifications
    meta[turn_key] = updated_turn
    return True


def release_turn_notification_in_meta(
    meta: SessionMeta,
    *,
    turn_key: str,
    turn: Mapping[str, object],
    notification_key: str,
) -> bool:
    notifications = turn.get("notifications")
    if not isinstance(notifications, dict) or notification_key not in notifications:
        return False
    updated_notifications = dict(notifications)
    updated_notifications.pop(notification_key, None)
    updated_turn = cast(TurnRecord, dict(turn))
    updated_turn["notifications"] = updated_notifications
    meta[turn_key] = updated_turn
    return True


def update_pending_turn_watchdog(
    meta: SessionMeta, *, turn_id: str = "", values: Mapping[str, object]
) -> WatchdogState:
    pending_turn = as_turn_record(meta.get("pending_turn"))
    if not pending_turn:
        return {}
    if turn_id and str(pending_turn.get("turn_id") or "") != turn_id:
        return {}
    watchdog = as_watchdog_state(pending_turn.get("watchdog"))
    updated_watchdog = cast(WatchdogState, dict(watchdog))
    cast(dict[str, object], updated_watchdog).update(values)
    updated_turn = cast(TurnRecord, dict(pending_turn))
    updated_turn["watchdog"] = updated_watchdog
    meta["pending_turn"] = updated_turn
    return updated_watchdog


def complete_pending_turn_state(
    meta: SessionMeta,
    *,
    summary: str = "",
    turn_id: str = "",
    prompt: str = "",
    completed_at: float,
) -> tuple[TurnRecord, int]:
    pending_turn = as_turn_record(meta.get("pending_turn"))
    if not pending_turn:
        return {}, 0
    pending_turn_id = str(pending_turn.get("turn_id") or "")
    pending_prompt = str(pending_turn.get("prompt") or "")
    if (
        turn_id
        and pending_turn_id
        and pending_turn_id != str(turn_id)
        and (not prompt or pending_prompt != str(prompt))
    ):
        return {}, 0
    watchdog = as_watchdog_state(pending_turn.get("watchdog"))
    pid = (
        int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    )
    completed = cast(TurnRecord, dict(pending_turn))
    if summary:
        completed["summary"] = summary
    completed["completed_at"] = completed_at
    meta["last_completed_turn"] = completed
    meta.pop("pending_turn", None)
    return completed, pid


def uuid_hex(length: int) -> str:
    import uuid

    return uuid.uuid4().hex[:length]
