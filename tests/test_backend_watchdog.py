from __future__ import annotations

import backend


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.1)


def test_claim_turn_notification_deduplicates_same_event(xdg_runtime):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "pending_turn": {
                "turn_id": "turn-1",
                "notifications": {},
            },
        },
    )

    assert backend.claim_turn_notification("demo-session", "completed", turn_id="turn-1", source="hook") is True
    assert backend.claim_turn_notification("demo-session", "completed", turn_id="turn-1", source="hook") is False


def test_claim_turn_notification_deduplicates_same_notification_key(xdg_runtime):
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "pending_turn": {
                "turn_id": "turn-1",
                "notifications": {},
            },
        },
    )

    assert (
        backend.claim_turn_notification(
            "demo-session",
            "reminder",
            turn_id="turn-1",
            source="watchdog",
            notification_key="reminder:needs-input:bucket-1",
        )
        is True
    )
    assert (
        backend.claim_turn_notification(
            "demo-session",
            "reminder",
            turn_id="turn-1",
            source="watchdog",
            notification_key="reminder:needs-input:bucket-1",
        )
        is False
    )


def test_run_session_watchdog_emits_stalled_event(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "codex",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "do the work",
                "before_capture": "",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "state": "queued",
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "idle_samples": 0,
                    "stop_requested": False,
                },
            },
        },
    )

    samples = [
        {
            "signature": "sig-1",
            "cursor_x": "1",
            "cursor_y": "1",
            "cpu_percent": 0.0,
            "agent_running": True,
            "capture": "working",
        },
        {
            "signature": "sig-1",
            "cursor_x": "1",
            "cursor_y": "1",
            "cpu_percent": 0.0,
            "agent_running": True,
            "capture": "working",
        },
        {
            "signature": "sig-1",
            "cursor_x": "1",
            "cursor_y": "1",
            "cpu_percent": 0.0,
            "agent_running": True,
            "capture": "working",
        },
    ]
    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        index = min(len(emitted) + int(clock.now), len(samples) - 1)
        payload = dict(samples[index])
        payload.setdefault("pane_id", pane_id or "%1")
        payload.setdefault("pane_in_mode", "0")
        payload.setdefault("pane_dead", "0")
        payload.setdefault("pane_current_command", "codex")
        payload.setdefault("capture_bytes", len(str(payload["capture"]).encode("utf-8")))
        payload.setdefault("tail", str(payload["capture"]))
        return payload

    def fake_emit(
        session: str,
        *,
        event: str,
        summary: str,
        status: str,
        turn_id: str = "",
        cwd: str = "",
        source: str = "",
        tail_text: str = "",
    ):
        emitted.append(
            {
                "session": session,
                "event": event,
                "summary": summary,
                "status": status,
                "turn_id": turn_id,
                "cwd": cwd,
                "source": source,
                "tail_text": tail_text,
            }
        )
        meta = backend.load_meta(session)
        meta.pop("pending_turn", None)
        backend.save_meta(session, meta)
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-1",
        poll_interval=1.0,
        stalled_after=2.0,
        needs_input_after=10.0,
    )

    assert result == "completed"
    assert emitted == [
        {
            "session": "demo-session",
            "event": "stalled",
            "summary": "working",
            "status": "warning",
            "turn_id": "turn-1",
            "cwd": "/repo",
            "source": "watchdog",
            "tail_text": "working",
        }
    ]


def test_run_session_watchdog_emits_completed_when_codex_returns_to_input_surface(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "codex",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-complete-1",
                "prompt": "Reply with exactly DEBUG_TOKEN_456 and nothing else.",
                "before_capture": "before prompt\n",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "state": "queued",
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "idle_samples": 0,
                    "stop_requested": False,
                },
            },
        },
    )

    samples = iter(
        [
            {
                "signature": "sig-1",
                "cursor_x": "1",
                "cursor_y": "1",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": "before prompt\n",
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": len("before prompt\n".encode("utf-8")),
                "tail": "before prompt",
            },
            {
                "signature": "sig-2",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_456 and nothing else.\n"
                    "\n"
                    "• DEBUG_TOKEN_456\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_TOKEN_456",
            },
            {
                "signature": "sig-2",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_456 and nothing else.\n"
                    "\n"
                    "• DEBUG_TOKEN_456\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_TOKEN_456",
            },
        ]
    )
    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        try:
            return dict(next(samples))
        except StopIteration:
            return {
                "signature": "sig-2",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_456 and nothing else.\n"
                    "\n"
                    "• DEBUG_TOKEN_456\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": pane_id or "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_TOKEN_456",
            }

    def fake_emit(
        session: str,
        *,
        event: str,
        summary: str,
        status: str,
        turn_id: str = "",
        cwd: str = "",
        source: str = "",
        tail_text: str = "",
    ):
        emitted.append(
            {
                "session": session,
                "event": event,
                "summary": summary,
                "status": status,
                "turn_id": turn_id,
                "cwd": cwd,
                "source": source,
                "tail_text": tail_text,
            }
        )
        meta = backend.load_meta(session)
        meta.pop("pending_turn", None)
        backend.save_meta(session, meta)
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-complete-1",
        poll_interval=1.0,
        stalled_after=45.0,
        needs_input_after=120.0,
    )

    assert result == "completed"
    assert emitted == [
        {
            "session": "demo-session",
            "event": "completed",
            "summary": "DEBUG_TOKEN_456",
            "status": "success",
            "turn_id": "turn-complete-1",
            "cwd": "/repo",
            "source": "watchdog",
            "tail_text": "",
        }
    ]


def test_run_session_watchdog_ignores_transient_codex_progress_before_real_output(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "codex",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-complete-2",
                "prompt": "Reply with exactly DEBUG_TOKEN_789 and nothing else.",
                "before_capture": "before prompt\n",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "state": "queued",
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "idle_samples": 0,
                    "stop_requested": False,
                },
            },
        },
    )

    samples = iter(
        [
            {
                "signature": "sig-1",
                "cursor_x": "1",
                "cursor_y": "1",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": "before prompt\n",
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": len("before prompt\n".encode("utf-8")),
                "tail": "before prompt",
            },
            {
                "signature": "sig-2",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_789 and nothing else.\n"
                    "\n"
                    "• Working (1s • esc to interrupt)\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "Working (1s • esc to interrupt)",
            },
            {
                "signature": "sig-3",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_789 and nothing else.\n"
                    "\n"
                    "• Working (1s • esc to interrupt)\n"
                    "\n"
                    "• DEBUG_TOKEN_789\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_TOKEN_789",
            },
        ]
    )
    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        try:
            return dict(next(samples))
        except StopIteration:
            return {
                "signature": "sig-3",
                "cursor_x": "2",
                "cursor_y": "18",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "› Reply with exactly DEBUG_TOKEN_789 and nothing else.\n"
                    "\n"
                    "• Working (1s • esc to interrupt)\n"
                    "\n"
                    "• DEBUG_TOKEN_789\n"
                    "\n"
                    "› Implement {feature}\n"
                    "\n"
                    "  gpt-5.4 high · 98% left · /repo\n"
                ),
                "pane_id": pane_id or "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_TOKEN_789",
            }

    def fake_emit(
        session: str,
        *,
        event: str,
        summary: str,
        status: str,
        turn_id: str = "",
        cwd: str = "",
        source: str = "",
        tail_text: str = "",
    ):
        emitted.append(
            {
                "session": session,
                "event": event,
                "summary": summary,
                "status": status,
                "turn_id": turn_id,
                "cwd": cwd,
                "source": source,
                "tail_text": tail_text,
            }
        )
        meta = backend.load_meta(session)
        meta.pop("pending_turn", None)
        backend.save_meta(session, meta)
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-complete-2",
        poll_interval=1.0,
        stalled_after=45.0,
        needs_input_after=120.0,
    )

    assert result == "completed"
    assert emitted == [
        {
            "session": "demo-session",
            "event": "completed",
            "summary": "DEBUG_TOKEN_789",
            "status": "success",
            "turn_id": "turn-complete-2",
            "cwd": "/repo",
            "source": "watchdog",
            "tail_text": "",
        }
    ]


def test_watchdog_summary_ignores_wrapped_prompt_fragments():
    pending_turn = {
        "prompt": "Reply with exactly DEBUG_TOKEN and nothing else. Do not add punctuation, quotes, bullets, or explanations.",
        "before_capture": "before prompt\n",
    }
    capture = (
        "before prompt\n"
        "› Reply with exactly DEBUG_TOKEN and nothing else.\n"
        "  Do not add punctuation, quotes, bullets, or\n"
        "  explanations.\n"
        "\n"
        "  gpt-5.4 high · 100% left · /repo\n"
    )

    stalled = backend._watchdog_summary_for_event("stalled", pending_turn=pending_turn, capture=capture)
    needs_input = backend._watchdog_summary_for_event("needs-input", pending_turn=pending_turn, capture=capture)

    assert stalled == "Agent output has stalled without observable progress"
    assert needs_input == "Agent has been idle for an extended period and likely needs input"


def test_run_session_watchdog_emits_completed_when_claude_returns_to_input_surface(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "claude",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-claude-complete-1",
                "prompt": "Reply with exactly DEBUG_CLAUDE_TOKEN and nothing else.",
                "before_capture": "before prompt\n",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "state": "queued",
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "idle_samples": 0,
                    "stop_requested": False,
                },
            },
        },
    )

    samples = iter(
        [
            {
                "signature": "sig-1",
                "cursor_x": "1",
                "cursor_y": "1",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": "before prompt\n",
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": len("before prompt\n".encode("utf-8")),
                "tail": "before prompt",
            },
            {
                "signature": "sig-2",
                "cursor_x": "1",
                "cursor_y": "16",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "❯ Reply with exactly DEBUG_CLAUDE_TOKEN and nothing else.\n"
                    "\n"
                    "⏺ DEBUG_CLAUDE_TOKEN\n"
                    "\n"
                    "────────────────────────────────────────────────────────────────────────────────\n"
                    "❯ \n"
                ),
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_CLAUDE_TOKEN",
            },
        ]
    )
    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        try:
            return dict(next(samples))
        except StopIteration:
            return {
                "signature": "sig-2",
                "cursor_x": "1",
                "cursor_y": "16",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": (
                    "before prompt\n"
                    "❯ Reply with exactly DEBUG_CLAUDE_TOKEN and nothing else.\n"
                    "\n"
                    "⏺ DEBUG_CLAUDE_TOKEN\n"
                    "\n"
                    "────────────────────────────────────────────────────────────────────────────────\n"
                    "❯ \n"
                ),
                "pane_id": pane_id or "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "node",
                "capture_bytes": 0,
                "tail": "DEBUG_CLAUDE_TOKEN",
            }

    def fake_emit(
        session: str,
        *,
        event: str,
        summary: str,
        status: str,
        turn_id: str = "",
        cwd: str = "",
        source: str = "",
        tail_text: str = "",
    ):
        emitted.append(
            {
                "session": session,
                "event": event,
                "summary": summary,
                "status": status,
                "turn_id": turn_id,
                "cwd": cwd,
                "source": source,
                "tail_text": tail_text,
            }
        )
        meta = backend.load_meta(session)
        meta.pop("pending_turn", None)
        backend.save_meta(session, meta)
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-claude-complete-1",
        poll_interval=1.0,
        stalled_after=45.0,
        needs_input_after=120.0,
    )

    assert result == "completed"
    assert emitted == [
        {
            "session": "demo-session",
            "event": "completed",
            "summary": "DEBUG_CLAUDE_TOKEN",
            "status": "success",
            "turn_id": "turn-claude-complete-1",
            "cwd": "/repo",
            "source": "watchdog",
            "tail_text": "",
        }
    ]


def test_latest_turn_summary_retries_until_claude_completion_surface_appears(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "claude",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-latest-1",
                "prompt": "Reply with exactly DEBUG_LATEST_TOKEN and nothing else.",
                "before_capture": "before prompt\n",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
            },
        },
    )

    captures = iter(
        [
            (
                "before prompt\n"
                "❯ Reply with exactly DEBUG_LATEST_TOKEN and nothing else.\n"
                "\n"
                "✻ Baking… (thinking)\n"
            ),
            (
                "before prompt\n"
                "❯ Reply with exactly DEBUG_LATEST_TOKEN and nothing else.\n"
                "\n"
                "⏺ DEBUG_LATEST_TOKEN\n"
                "\n"
                "────────────────────────────────────────────────────────────────────────────────\n"
                "❯ \n"
            ),
        ]
    )

    monkeypatch.setattr(backend, "bridge_resolve", lambda session: "%1")
    monkeypatch.setattr(
        backend,
        "read_pane",
        lambda pane_id, lines=backend.DEFAULT_CAPTURE_LINES: next(captures),
    )
    monkeypatch.setattr(backend.time, "monotonic", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)
    monkeypatch.setattr(backend.time, "time", clock.time)

    summary = backend.latest_turn_summary("demo-session")
    meta = backend.load_meta("demo-session")

    assert summary == "DEBUG_LATEST_TOKEN"
    assert "pending_turn" not in meta
    assert meta["last_completed_turn"]["summary"] == "DEBUG_LATEST_TOKEN"


def test_run_session_watchdog_emits_failed_event_when_agent_exits(xdg_runtime, monkeypatch):
    clock = FakeClock()
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "codex",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-2",
                "prompt": "do the work",
                "before_capture": "",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "state": "queued",
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "idle_samples": 0,
                    "stop_requested": False,
                },
            },
        },
    )

    samples = iter(
        [
            {
                "signature": "sig-1",
                "cursor_x": "1",
                "cursor_y": "1",
                "cpu_percent": 0.0,
                "agent_running": True,
                "capture": "working",
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "0",
                "pane_current_command": "codex",
                "capture_bytes": 7,
                "tail": "working",
            },
            {
                "signature": "sig-1",
                "cursor_x": "1",
                "cursor_y": "1",
                "cpu_percent": 0.0,
                "agent_running": False,
                "capture": "working",
                "pane_id": "%1",
                "pane_in_mode": "0",
                "pane_dead": "1",
                "pane_current_command": "zsh",
                "capture_bytes": 7,
                "tail": "working",
            },
        ]
    )
    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        return dict(next(samples))

    def fake_emit(
        session: str,
        *,
        event: str,
        summary: str,
        status: str,
        turn_id: str = "",
        cwd: str = "",
        source: str = "",
        tail_text: str = "",
    ):
        emitted.append((event, summary, status, turn_id, cwd, source, tail_text))
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-2",
        poll_interval=1.0,
        stalled_after=30.0,
        needs_input_after=60.0,
    )

    assert result == "failed"
    assert emitted == [
        (
            "failed",
            "working",
            "failure",
            "turn-2",
            "/repo",
            "watchdog",
            "working",
        )
    ]


def test_run_session_watchdog_emits_periodic_reminder_after_last_notify(xdg_runtime, monkeypatch):
    clock = FakeClock()
    clock.now = 602.0
    backend.save_meta(
        "demo-session",
        {
            "session": "demo-session",
            "cwd": "/repo",
            "agent": "codex",
            "pane_id": "%1",
            "pending_turn": {
                "turn_id": "turn-3",
                "prompt": "do the work",
                "before_capture": "",
                "submitted_at": 0.0,
                "pane_id": "%1",
                "notifications": {
                    "stalled": {
                        "at": 1.0,
                        "source": "watchdog",
                        "status": "warning",
                        "summary": "working",
                    }
                },
                "watchdog": {
                    "state": "stalled",
                    "last_event": "stalled",
                    "last_event_at": 1.0,
                    "last_progress_at": 0.0,
                    "last_sample_at": 0.0,
                    "last_signature": "sig-1",
                    "last_cursor_x": "1",
                    "last_cursor_y": "1",
                    "idle_samples": 2,
                    "stop_requested": False,
                },
            },
        },
    )

    emitted = []

    def fake_sample(session: str, *, pane_id: str = ""):
        return {
            "signature": "sig-1",
            "cursor_x": "1",
            "cursor_y": "1",
            "cpu_percent": 0.0,
            "agent_running": True,
            "capture": "still waiting",
            "pane_id": pane_id or "%1",
            "pane_in_mode": "0",
            "pane_dead": "0",
            "pane_current_command": "codex",
            "capture_bytes": 13,
            "tail": "still waiting",
        }

    def fake_emit(
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
    ):
        emitted.append((event, summary, status, turn_id, cwd, source, notification_key, tail_text))
        if event == "reminder":
            meta = backend.load_meta(session)
            meta.pop("pending_turn", None)
            backend.save_meta(session, meta)
        return True

    monkeypatch.setattr(backend, "sample_watchdog_state", fake_sample)
    monkeypatch.setattr(backend, "emit_internal_notify", fake_emit)
    monkeypatch.setattr(backend.time, "time", clock.time)
    monkeypatch.setattr(backend.time, "sleep", clock.sleep)

    result = backend.run_session_watchdog(
        "demo-session",
        turn_id="turn-3",
        poll_interval=1.0,
        stalled_after=45.0,
        needs_input_after=10_000.0,
        reminder_after=600.0,
    )

    assert result == "completed"
    assert emitted == [
        (
            "reminder",
            "Session demo-session is still in stalled state and has gone 10 minutes without a successful notify. "
            "The agent session has shown no visible progress for an extended period. To reconnect with it, run "
            "`orche status demo-session` and `orche read demo-session --lines 120`.",
            "warning",
            "turn-3",
            "/repo",
            "watchdog",
            "reminder:stalled:1:1",
            "",
        )
    ]
