from __future__ import annotations

import pytest

from runtime import turn as turn_module
from session.store import load_meta, save_meta

pytestmark = pytest.mark.unit


def test_current_turn_entry_respects_matching_and_fallback():
    meta = {
        "pending_turn": {"turn_id": "pending-1", "prompt": "new"},
        "last_completed_turn": {"turn_id": "done-1", "prompt": "old"},
    }

    turn_key, turn = turn_module._current_turn_entry(meta, turn_id="pending-1")
    missing_key, missing_turn = turn_module._current_turn_entry(
        meta,
        turn_id="unknown",
        allow_fallback=False,
    )

    assert turn_key == "pending_turn"
    assert turn["turn_id"] == "pending-1"
    assert missing_key == ""
    assert missing_turn == {}


def test_mark_session_startup_blocked_only_transitions_from_launching(
    xdg_runtime, monkeypatch
):
    save_meta("demo-session", {"session": "demo-session"})
    monkeypatch.setattr(turn_module.time, "time", lambda: 10.0)
    turn_module.initialize_session_startup(
        "demo-session", state="launching", started_at=1.0
    )

    startup, changed = turn_module.mark_session_startup_blocked(
        "demo-session",
        reason="waiting for login",
        event_name="startup-blocked",
    )
    repeated, repeated_changed = turn_module.mark_session_startup_blocked(
        "demo-session",
        reason="different reason",
        event_name="startup-blocked",
    )

    assert changed is True
    assert startup["state"] == "blocked"
    assert startup["blocked_reason"] == "waiting for login"
    assert repeated_changed is False
    assert repeated["blocked_reason"] == "waiting for login"


def test_claim_and_release_turn_notification_deduplicate_by_key(xdg_runtime):
    save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "pending_turn": {"turn_id": "turn-1", "notifications": {}},
        },
    )

    assert turn_module.claim_turn_notification(
        "demo-session",
        "reminder",
        turn_id="turn-1",
        notification_key="reminder:bucket-1",
    )
    assert not turn_module.claim_turn_notification(
        "demo-session",
        "reminder",
        turn_id="turn-1",
        notification_key="reminder:bucket-1",
    )

    turn_module.release_turn_notification(
        "demo-session",
        "reminder",
        turn_id="turn-1",
        notification_key="reminder:bucket-1",
    )

    assert turn_module.claim_turn_notification(
        "demo-session",
        "reminder",
        turn_id="turn-1",
        notification_key="reminder:bucket-1",
    )


def test_claim_turn_notification_does_not_match_unrelated_completed_turn(xdg_runtime):
    save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "last_completed_turn": {
                "turn_id": "turn-1",
                "prompt": "old prompt",
                "notifications": {"completed": {"source": "hook"}},
            },
        },
    )

    claimed = turn_module.claim_turn_notification(
        "demo-session",
        "completed",
        turn_id="turn-2",
        prompt="new prompt",
    )

    assert claimed is True
    assert load_meta("demo-session")["last_completed_turn"]["turn_id"] == "turn-1"


def test_complete_pending_turn_promotes_turn_and_stops_watchdog(
    xdg_runtime, monkeypatch
):
    save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "ship it",
                "watchdog": {"pid": 4321},
            },
        },
    )
    killed = []
    monkeypatch.setattr(turn_module.time, "time", lambda: 42.0)
    monkeypatch.setattr(turn_module, "process_is_alive", lambda pid: pid == 4321)
    monkeypatch.setattr(
        turn_module.os, "kill", lambda pid, sig: killed.append((pid, sig))
    )

    completed = turn_module.complete_pending_turn(
        "demo-session",
        summary="done",
        turn_id="turn-1",
    )
    meta = load_meta("demo-session")

    assert completed["summary"] == "done"
    assert "pending_turn" not in meta
    assert meta["last_completed_turn"]["completed_at"] == 42.0
    assert killed and killed[0][0] == 4321
