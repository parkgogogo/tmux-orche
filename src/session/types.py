from __future__ import annotations

from typing import TypedDict, cast


class NotifyBinding(TypedDict, total=False):
    provider: str
    target: str
    session: str


class StartupState(TypedDict, total=False):
    state: str
    started_at: float
    ready_at: float
    ready_source: str
    blocked_at: float
    blocked_reason: str
    blocked_event: str
    updated_at: float


class PromptAckState(TypedDict, total=False):
    state: str
    accepted_at: float
    source: str


class NotificationRecord(TypedDict, total=False):
    at: float
    source: str
    status: str
    summary: str


NotificationMap = dict[str, NotificationRecord]


class WatchdogState(TypedDict, total=False):
    state: str
    started_at: float
    last_progress_at: float
    last_sample_at: float
    idle_samples: int
    stop_requested: bool
    pid: int
    last_signature: str
    last_cursor_x: str
    last_cursor_y: str
    last_event: str
    last_event_at: float
    pending_event: str
    pending_event_at: float
    pending_event_summary: str
    notification_sent_at: float


class TurnRecord(TypedDict, total=False):
    turn_id: str
    prompt: str
    before_capture: str
    submitted_at: float
    pane_id: str
    notifications: NotificationMap
    prompt_ack: PromptAckState
    watchdog: WatchdogState
    summary: str
    completed_at: float


class SessionMeta(TypedDict, total=False):
    backend: str
    session: str
    cwd: str
    agent: str
    pane_id: str
    tmux_mode: str
    host_pane_id: str
    tmux_host_session: str
    parent_session: str
    last_seen_at: float
    last_event_at: float
    last_event_source: str
    expires_after_seconds: int
    tmux_session: str
    window_id: str
    window_name: str
    inline_slot: int
    runtime_home: str
    runtime_home_managed: bool
    runtime_label: str
    codex_home: str
    codex_home_managed: bool
    agent_started_at: float
    notify_binding: NotifyBinding
    startup: StartupState
    pending_turn: TurnRecord
    last_completed_turn: TurnRecord


def as_session_meta(value: object) -> SessionMeta:
    return cast(SessionMeta, dict(value) if isinstance(value, dict) else {})


def as_startup_state(value: object) -> StartupState:
    return cast(StartupState, dict(value) if isinstance(value, dict) else {})


def as_turn_record(value: object) -> TurnRecord:
    return cast(TurnRecord, dict(value) if isinstance(value, dict) else {})


def as_prompt_ack_state(value: object) -> PromptAckState:
    return cast(PromptAckState, dict(value) if isinstance(value, dict) else {})


def as_watchdog_state(value: object) -> WatchdogState:
    return cast(WatchdogState, dict(value) if isinstance(value, dict) else {})
