from __future__ import annotations

import pytest

from session.session_state import (
    apply_agent_launch_state,
    apply_runtime_state,
    apply_session_open_state,
    apply_session_pane_state,
    clear_legacy_notify_state,
    initialize_startup_state,
    mark_startup_blocked_if_launching,
    mark_startup_ready,
    mark_startup_timeout,
    set_notify_binding,
    touch_session_meta,
)
from session.turn_state import (
    begin_pending_turn,
    claim_turn_notification_in_meta,
    complete_pending_turn_state,
    mark_pending_turn_prompt_accepted,
    release_turn_notification_in_meta,
    update_pending_turn_watchdog,
)

pytestmark = pytest.mark.unit


def test_apply_session_open_state_sets_core_session_fields():
    meta: dict[str, object] = {}

    result = apply_session_open_state(
        meta,
        session="demo",
        cwd="/repo",
        agent="codex",
        pane_id="%1",
        tmux_mode="inline-pane",
        host_pane_id="%9",
        tmux_host_session="dev",
        parent_session="reviewer",
        expires_after_seconds=1800,
        backend="tmux",
        timestamp=123.0,
    )

    assert result is meta
    assert meta == {
        "backend": "tmux",
        "session": "demo",
        "cwd": "/repo",
        "agent": "codex",
        "pane_id": "%1",
        "tmux_mode": "inline-pane",
        "host_pane_id": "%9",
        "tmux_host_session": "dev",
        "last_seen_at": 123.0,
        "parent_session": "reviewer",
        "last_event_at": 123.0,
        "last_event_source": "open",
        "expires_after_seconds": 1800,
    }


def test_notify_mutations_replace_binding_and_clear_legacy_fields():
    meta = {
        "discord_channel_id": "123",
        "discord_session": "legacy",
        "notify_routes": {"tmux-bridge": {"target_session": "old"}},
        "notify_binding": {"provider": "discord", "target": "old"},
    }

    clear_legacy_notify_state(meta)
    set_notify_binding(meta, {"provider": "tmux-bridge", "target": "pane:%9"})

    assert meta == {"notify_binding": {"provider": "tmux-bridge", "target": "pane:%9"}}


def test_begin_pending_turn_creates_consistent_turn_payload():
    meta: dict[str, object] = {}

    pending_turn = begin_pending_turn(
        meta,
        prompt="hello",
        pane_id="%1",
        before_capture="before",
        submitted_at=321.0,
    )

    assert meta["pending_turn"] is pending_turn
    assert pending_turn["prompt"] == "hello"
    assert pending_turn["pane_id"] == "%1"
    assert pending_turn["before_capture"] == "before"
    assert pending_turn["submitted_at"] == 321.0
    assert pending_turn["notifications"] == {}
    assert pending_turn["prompt_ack"] == {
        "state": "pending",
        "accepted_at": 0.0,
        "source": "",
    }
    assert pending_turn["watchdog"] == {
        "state": "queued",
        "started_at": 0.0,
        "last_progress_at": 321.0,
        "last_sample_at": 0.0,
        "idle_samples": 0,
        "stop_requested": False,
    }
    assert isinstance(pending_turn["turn_id"], str)
    assert len(str(pending_turn["turn_id"])) == 12


def test_apply_session_pane_state_tracks_inline_slot_and_agent_launch():
    meta: dict[str, object] = {}

    apply_session_pane_state(
        meta,
        session="demo",
        cwd="/repo",
        agent="claude",
        tmux_session="orche-demo",
        pane_id="%3",
        window_id="@1",
        window_name="demo",
        tmux_mode="inline-pane",
        host_pane_id="%1",
        tmux_host_session="workspace",
        last_seen_at=10.0,
        backend="tmux",
        inline_slot=2,
    )
    apply_agent_launch_state(
        meta,
        session="demo",
        cwd="/repo",
        pane_id="%3",
        agent_started_at=11.0,
        last_seen_at=12.0,
        backend="tmux",
    )
    apply_session_pane_state(
        meta,
        session="demo",
        cwd="/repo",
        agent="claude",
        tmux_session="orche-demo",
        pane_id="%3",
        window_id="@1",
        window_name="demo",
        tmux_mode="dedicated-session",
        host_pane_id="",
        tmux_host_session="orche-demo",
        last_seen_at=13.0,
        backend="tmux",
        inline_slot=None,
    )

    assert meta["pane_id"] == "%3"
    assert meta["agent_started_at"] == 11.0
    assert meta["last_seen_at"] == 13.0
    assert "inline_slot" not in meta


def test_touch_session_and_runtime_state_update_meta_fields():
    meta: dict[str, object] = {}

    touch_session_meta(
        meta,
        timestamp=20.0,
        source="watchdog",
        expires_after_seconds=1800,
    )
    apply_runtime_state(
        meta,
        agent="codex",
        runtime_home="~/runtime",
        runtime_home_managed=True,
        runtime_label="Codex home",
    )

    assert meta["last_event_at"] == 20.0
    assert meta["last_event_source"] == "watchdog"
    assert meta["expires_after_seconds"] == 1800
    assert meta["runtime_home"]
    assert meta["runtime_home_managed"] is True
    assert meta["codex_home"] == meta["runtime_home"]
    assert meta["codex_home_managed"] is True


def test_startup_state_helpers_cover_launch_ready_timeout_and_blocked():
    meta: dict[str, object] = {}

    startup = initialize_startup_state(meta, state="launching", timestamp=1.0)
    ready = mark_startup_ready(meta, source="hook", timestamp=2.0)
    timeout = mark_startup_timeout(meta, reason="slow boot", timestamp=3.0)
    blocked, changed = mark_startup_blocked_if_launching(
        meta,
        reason="waiting for login",
        event_name="startup-blocked",
        timestamp=4.0,
    )

    assert startup["state"] == "launching"
    assert ready["state"] == "ready"
    assert ready["ready_source"] == "hook"
    assert timeout["state"] == "timeout"
    assert timeout["blocked_reason"] == "slow boot"
    assert changed is False
    assert blocked["state"] == "timeout"


def test_turn_meta_helpers_mutate_prompt_ack_notifications_watchdog_and_completion():
    meta: dict[str, object] = {}

    pending_turn = begin_pending_turn(
        meta,
        prompt="hello",
        pane_id="%1",
        before_capture="before",
        submitted_at=5.0,
    )
    prompt_ack = mark_pending_turn_prompt_accepted(
        meta,
        source="submit",
        timestamp=6.0,
    )
    claimed = claim_turn_notification_in_meta(
        meta,
        turn_key="pending_turn",
        turn=meta["pending_turn"],
        notification_key="completed",
        timestamp=7.0,
        source="watchdog",
        status="success",
        summary="done",
    )
    duplicate = claim_turn_notification_in_meta(
        meta,
        turn_key="pending_turn",
        turn=meta["pending_turn"],
        notification_key="completed",
        timestamp=8.0,
    )
    watchdog = update_pending_turn_watchdog(
        meta,
        turn_id=str(pending_turn["turn_id"]),
        values={"pid": 4321, "state": "running"},
    )
    released = release_turn_notification_in_meta(
        meta,
        turn_key="pending_turn",
        turn=meta["pending_turn"],
        notification_key="completed",
    )
    completed, pid = complete_pending_turn_state(
        meta,
        summary="final",
        turn_id=str(pending_turn["turn_id"]),
        completed_at=9.0,
    )

    assert prompt_ack["state"] == "accepted"
    assert prompt_ack["accepted_at"] == 6.0
    assert claimed is True
    assert duplicate is False
    assert watchdog["pid"] == 4321
    assert watchdog["state"] == "running"
    assert released is True
    assert completed["summary"] == "final"
    assert completed["completed_at"] == 9.0
    assert pid == 4321
    assert meta["last_completed_turn"]["turn_id"] == pending_turn["turn_id"]
    assert "pending_turn" not in meta
