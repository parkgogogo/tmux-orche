from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from typing import Any, Callable, Dict

from agents.common import orche_bootstrap_command
from session.config import _read_notify_binding
from session.ops import touch_session_event
from session.pane import (
    observable_progress_detected,
    recent_capture_excerpt,
    sample_watchdog_state,
)
from session.store import load_meta, log_event
from session.types import SessionMeta, as_turn_record, as_watchdog_state
from text_utils import extract_summary_candidate, shorten, turn_delta
from tmux.client import process_is_alive

from .turn import (
    _current_turn_entry,
    release_turn_notification,
    update_watchdog_metadata,
)

WATCHDOG_POLL_INTERVAL = 3.0
WATCHDOG_STALLED_AFTER = 45.0
WATCHDOG_NEEDS_INPUT_AFTER = 120.0
WATCHDOG_REMINDER_AFTER = 600.0
WATCHDOG_NOTIFY_BUFFER = 10.0


def _orche_bootstrap_command() -> list[str]:
    return orche_bootstrap_command()


def emit_internal_notify(
    session: str,
    *,
    event: str,
    summary: str,
    status: str,
    turn_id: str = "",
    cwd: str = "",
    source: str = "",
    notification_key: str = "",
    tail_text: str = "",
) -> bool:
    notify_provider = _read_notify_binding(load_meta(session)).get("provider", "")
    normalized_tail_text = (
        recent_capture_excerpt(tail_text)
        if notify_provider == "discord"
        else tail_text.strip()
    )
    payload: Dict[str, Any] = {
        "event": event,
        "summary": summary,
        "session": session,
        "cwd": cwd,
        "turn_id": turn_id,
        "source": source,
        "metadata": {
            "turn_id": turn_id,
            "source": source,
            "notification_key": notification_key,
        },
    }
    if normalized_tail_text:
        payload["metadata"]["tail_text"] = normalized_tail_text
        payload["metadata"]["tail_lines"] = max(
            1, len(normalized_tail_text.splitlines())
        )
    result = subprocess.run(
        _orche_bootstrap_command()
        + ["notify-internal", "--session", session, "--status", status],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=False,
        start_new_session=True,
    )
    if result.returncode != 0:
        log_event(
            "watchdog.notify.failed",
            session=session,
            notify_event=event,
            status=status,
            detail=shorten((result.stderr or result.stdout or "").strip(), 400),
        )
        return False
    touch_session_event(session, source=f"notify:{event}")
    return True


def _watchdog_summary_for_event(
    event: str, *, pending_turn: Mapping[str, Any], capture: str
) -> str:
    before_capture = str(pending_turn.get("before_capture") or "")
    delta = turn_delta(before_capture, capture) if capture else ""
    prompt = str(pending_turn.get("prompt") or "")
    candidate = extract_summary_candidate(delta, prompt=prompt)
    if candidate:
        return candidate
    if event == "failed":
        return "Agent process exited before completion notify was delivered"
    if event == "needs-input":
        return "Agent has been idle for an extended period and likely needs input"
    return "Agent output has stalled without observable progress"


def _watchdog_event_status(event: str) -> str:
    if event == "failed":
        return "failure"
    if event in {"stalled", "needs-input", "startup-blocked"}:
        return event
    return "success"


def _latest_notification_at(pending_turn: Mapping[str, Any]) -> float:
    notifications = pending_turn.get("notifications")
    if not isinstance(notifications, dict):
        return 0.0
    latest = 0.0
    for payload in notifications.values():
        if not isinstance(payload, Mapping):
            continue
        try:
            latest = max(latest, float(payload.get("at") or 0.0))
        except (TypeError, ValueError):
            continue
    return latest


def _watchdog_time_value(*values: Any, default: float) -> float:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _watchdog_reminder_summary(session: str, state: str) -> str:
    normalized = str(state or "").strip().lower() or "stalled"
    if normalized == "needs-input":
        situation = "The agent is likely waiting for terminal input, approval, or other user intervention."
    else:
        situation = (
            "The agent session has shown no visible progress for an extended period."
        )
    return f"Session {session} is still in {normalized} state and has gone 10 minutes without a successful notify. {situation} To reconnect with it, run `orche status {session}` and `orche read {session} --lines 120`."


def _watchdog_pending_event_ready(
    watchdog: Mapping[str, Any],
    *,
    event: str,
    summary: str,
    now: float,
    notify_buffer: float,
) -> tuple[bool, dict[str, Any]]:
    if notify_buffer <= 0:
        return True, {
            "pending_event": "",
            "pending_event_at": 0.0,
            "pending_event_summary": "",
        }
    pending_event = str(watchdog.get("pending_event") or "")
    pending_summary = str(watchdog.get("pending_event_summary") or "")
    pending_at = float(watchdog.get("pending_event_at") or 0.0)
    if pending_event != event or pending_summary != summary or pending_at <= 0.0:
        return False, {
            "pending_event": event,
            "pending_event_at": now,
            "pending_event_summary": summary,
        }
    if now - pending_at < notify_buffer:
        return False, {}
    return True, {
        "pending_event": "",
        "pending_event_at": 0.0,
        "pending_event_summary": "",
    }


def start_session_watchdog(session: str, *, turn_id: str = "") -> int:
    meta = load_meta(session)
    turn_key, turn = _current_turn_entry(meta, turn_id=turn_id)
    if turn_key != "pending_turn" or not turn:
        raise RuntimeError(f"Session {session} has no pending turn to watch")
    resolved_turn_id = str(turn.get("turn_id") or "").strip()
    raw_watchdog = turn.get("watchdog")
    watchdog: Dict[str, Any] = (
        dict(raw_watchdog) if isinstance(raw_watchdog, dict) else {}
    )
    existing_pid = (
        int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    )
    if existing_pid and process_is_alive(existing_pid):
        return existing_pid
    proc = subprocess.Popen(
        _orche_bootstrap_command()
        + [
            "watchdog-loop-internal",
            "--session",
            session,
            "--turn-id",
            resolved_turn_id,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    update_watchdog_metadata(
        session,
        turn_id=resolved_turn_id,
        values={
            "pid": proc.pid,
            "state": "starting",
            "started_at": time.time(),
            "last_progress_at": _watchdog_time_value(
                turn.get("submitted_at"), default=time.time()
            ),
            "last_sample_at": 0.0,
            "idle_samples": 0,
            "stop_requested": False,
        },
    )
    return proc.pid


def stop_session_watchdog(session: str) -> int:
    meta = load_meta(session)
    turn_key, turn = _current_turn_entry(meta)
    if turn_key != "pending_turn" or not turn:
        return 0
    raw_watchdog = turn.get("watchdog")
    watchdog: Dict[str, Any] = (
        dict(raw_watchdog) if isinstance(raw_watchdog, dict) else {}
    )
    pid = (
        int(watchdog.get("pid") or 0) if str(watchdog.get("pid") or "").isdigit() else 0
    )
    update_watchdog_metadata(
        session,
        turn_id=str(turn.get("turn_id") or ""),
        values={"stop_requested": True, "stopped_at": time.time(), "state": "stopping"},
    )
    if pid > 0 and process_is_alive(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
    return pid


def run_session_watchdog(
    session: str,
    *,
    turn_id: str,
    poll_interval: float = WATCHDOG_POLL_INTERVAL,
    stalled_after: float = WATCHDOG_STALLED_AFTER,
    needs_input_after: float = WATCHDOG_NEEDS_INPUT_AFTER,
    reminder_after: float = WATCHDOG_REMINDER_AFTER,
    notify_buffer: float = WATCHDOG_NOTIFY_BUFFER,
    sample_watchdog_state_fn: Callable[..., Dict[str, Any]] = sample_watchdog_state,
    observable_progress_detected_fn: Callable[
        [str, tuple[str, str], Mapping[str, Any]], bool
    ] = observable_progress_detected,
    update_watchdog_metadata_fn: Callable[
        ..., Mapping[str, object]
    ] = update_watchdog_metadata,
    release_turn_notification_fn: Callable[..., None] = release_turn_notification,
    emit_internal_notify_fn: Callable[..., bool] = emit_internal_notify,
    load_meta_fn: Callable[[str], SessionMeta] = load_meta,
) -> str:
    while True:
        meta = load_meta_fn(session)
        pending_turn = as_turn_record(meta.get("pending_turn"))
        if not pending_turn or str(pending_turn.get("turn_id") or "") != turn_id:
            return "completed"
        watchdog = as_watchdog_state(pending_turn.get("watchdog"))
        if bool(watchdog.get("stop_requested")):
            update_watchdog_metadata_fn(
                session,
                turn_id=turn_id,
                values={"state": "stopped", "stopped_at": time.time()},
            )
            return "stopped"
        sample = sample_watchdog_state_fn(
            session,
            pane_id=str(pending_turn.get("pane_id") or meta.get("pane_id") or ""),
        )
        now = time.time()
        previous_signature = str(watchdog.get("last_signature") or "")
        previous_cursor = (
            str(watchdog.get("last_cursor_x") or ""),
            str(watchdog.get("last_cursor_y") or ""),
        )
        current_cursor = (
            str(sample.get("cursor_x") or ""),
            str(sample.get("cursor_y") or ""),
        )
        progress_detected = observable_progress_detected_fn(
            previous_signature, previous_cursor, sample
        )
        last_progress_at = _watchdog_time_value(
            watchdog.get("last_progress_at"),
            pending_turn.get("submitted_at"),
            default=now,
        )
        idle_samples = int(watchdog.get("idle_samples") or 0)
        state = "running"
        if progress_detected:
            last_progress_at = now
            idle_samples = 0
        else:
            idle_samples += 1
            idle_seconds = max(0.0, now - last_progress_at)
            if not bool(sample.get("agent_running")):
                state = "failed"
            elif idle_samples >= 2 and idle_seconds >= needs_input_after:
                state = "needs-input"
            elif idle_samples >= 2 and idle_seconds >= stalled_after:
                state = "stalled"
        reset_values: Dict[str, Any] = {}
        if state == "running":
            if (
                watchdog.get("pending_event")
                or watchdog.get("pending_event_at")
                or watchdog.get("pending_event_summary")
            ):
                reset_values.update(
                    {
                        "pending_event": "",
                        "pending_event_at": 0.0,
                        "pending_event_summary": "",
                    }
                )
            previous_event = str(watchdog.get("last_event") or "")
            if previous_event in {"stalled", "needs-input", "failed"}:
                release_turn_notification_fn(session, previous_event, turn_id=turn_id)
                reset_values.update({"last_event": "", "last_event_at": 0.0})
        update_watchdog_metadata_fn(
            session,
            turn_id=turn_id,
            values={
                "pid": os.getpid(),
                "state": state,
                "last_signature": str(sample.get("signature") or ""),
                "last_cursor_x": current_cursor[0],
                "last_cursor_y": current_cursor[1],
                "last_cpu_percent": float(sample.get("cpu_percent") or 0.0),
                "last_sample_at": now,
                "last_progress_at": last_progress_at,
                "idle_samples": idle_samples,
                **reset_values,
            },
        )
        if state in {"failed", "stalled", "needs-input"}:
            emitted = False
            last_event = str(watchdog.get("last_event") or "")
            summary = _watchdog_summary_for_event(
                state,
                pending_turn=pending_turn,
                capture=str(sample.get("capture") or ""),
            )
            if last_event == state:
                if (
                    watchdog.get("pending_event")
                    or watchdog.get("pending_event_at")
                    or watchdog.get("pending_event_summary")
                ):
                    update_watchdog_metadata_fn(
                        session,
                        turn_id=turn_id,
                        values={
                            "pending_event": "",
                            "pending_event_at": 0.0,
                            "pending_event_summary": "",
                        },
                    )
            else:
                ready, pending_values = _watchdog_pending_event_ready(
                    watchdog,
                    event=state,
                    summary=summary,
                    now=now,
                    notify_buffer=notify_buffer,
                )
                if not ready:
                    if pending_values:
                        update_watchdog_metadata_fn(
                            session, turn_id=turn_id, values=pending_values
                        )
                elif (
                    str(
                        load_meta_fn(session)
                        .get("pending_turn", {})
                        .get("watchdog", {})
                        .get("last_event")
                        or ""
                    )
                    != state
                ):
                    if pending_values:
                        update_watchdog_metadata_fn(
                            session, turn_id=turn_id, values=pending_values
                        )
                    emitted = emit_internal_notify_fn(
                        session,
                        event=state,
                        summary=summary,
                        status=_watchdog_event_status(state),
                        turn_id=turn_id,
                        cwd=str(meta.get("cwd") or ""),
                        source="watchdog",
                        tail_text=str(sample.get("capture") or ""),
                    )
            update_watchdog_metadata_fn(
                session,
                turn_id=turn_id,
                values={
                    "last_event": state if emitted else last_event,
                    "last_event_at": now
                    if emitted
                    else float(watchdog.get("last_event_at") or 0.0),
                },
            )
            if state == "failed":
                return state
        latest_notify_at = _latest_notification_at(pending_turn or {})
        if (
            state in {"stalled", "needs-input"}
            and latest_notify_at > 0.0
            and now - latest_notify_at >= reminder_after
        ):
            reminder_bucket = int(now // max(reminder_after, 1.0))
            reminder_key = f"reminder:{state}:{int(latest_notify_at)}:{reminder_bucket}"
            emit_internal_notify_fn(
                session,
                event="reminder",
                summary=_watchdog_reminder_summary(session, state),
                status=_watchdog_event_status(state),
                turn_id=turn_id,
                cwd=str(meta.get("cwd") or ""),
                source="watchdog",
                notification_key=reminder_key,
            )
        time.sleep(max(0.5, poll_interval))
