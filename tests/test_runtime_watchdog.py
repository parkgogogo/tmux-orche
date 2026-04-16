from __future__ import annotations

import pytest

from runtime import watchdog as watchdog_module

pytestmark = pytest.mark.unit


def test_watchdog_pending_event_ready_requires_buffer_then_clears_state():
    ready, pending = watchdog_module._watchdog_pending_event_ready(
        {},
        event="needs-input",
        summary="stuck",
        now=100.0,
        notify_buffer=10.0,
    )
    buffered, still_pending = watchdog_module._watchdog_pending_event_ready(
        pending,
        event="needs-input",
        summary="stuck",
        now=105.0,
        notify_buffer=10.0,
    )
    drained, cleared = watchdog_module._watchdog_pending_event_ready(
        pending,
        event="needs-input",
        summary="stuck",
        now=111.0,
        notify_buffer=10.0,
    )

    assert ready is False
    assert pending["pending_event"] == "needs-input"
    assert buffered is False
    assert still_pending == {}
    assert drained is True
    assert cleared["pending_event"] == ""


def test_watchdog_summary_for_event_uses_event_specific_fallbacks(monkeypatch):
    monkeypatch.setattr(
        watchdog_module, "extract_summary_candidate", lambda delta, prompt="": ""
    )

    failed = watchdog_module._watchdog_summary_for_event(
        "failed",
        pending_turn={"before_capture": "", "prompt": "ship it"},
        capture="",
    )
    needs_input = watchdog_module._watchdog_summary_for_event(
        "needs-input",
        pending_turn={"before_capture": "", "prompt": "ship it"},
        capture="",
    )

    assert failed == "Agent process exited before completion notify was delivered"
    assert (
        needs_input
        == "Agent has been idle for an extended period and likely needs input"
    )


def test_watchdog_event_status_maps_warning_events():
    assert watchdog_module._watchdog_event_status("failed") == "failure"
    assert watchdog_module._watchdog_event_status("stalled") == "stalled"
    assert watchdog_module._watchdog_event_status("completed") == "success"


def test_watchdog_reminder_summary_includes_recovery_commands():
    summary = watchdog_module._watchdog_reminder_summary("worker-1", "needs-input")

    assert "worker-1" in summary
    assert "orche status worker-1" in summary
    assert "orche read worker-1 --lines 120" in summary
