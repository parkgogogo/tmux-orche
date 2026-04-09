from __future__ import annotations

import pytest

from notify.config import NotifyConfig
from notify.exceptions import NotifyConfigError, NotifyDeliveryError
from notify.models import NotifyEvent, ResolvedRoute
from notify.tmux_bridge import TmuxBridgeNotifier


def test_tmux_bridge_notifier_delivers_prompt_through_backend_helper(monkeypatch):
    captured = []

    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: captured.append((session, prompt)) or "%42",
    )

    notifier = TmuxBridgeNotifier(NotifyConfig())

    result = notifier.send(
        NotifyEvent(
            event="completed",
            summary="review source session output",
            session="source-session",
            cwd="/tmp/repo",
            status="success",
        ),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert result.ok is True
    assert result.target == "target-session"
    assert captured == [
        (
            "target-session",
            "orche notify\nsource session: source-session\nevent: completed\ncwd: /tmp/repo\n\nreview source session output\n\nstatus: success",
        )
    ]


def test_tmux_bridge_notifier_uses_default_prefix_for_empty_summary(monkeypatch):
    captured = []

    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: captured.append(prompt) or "%42",
    )

    notifier = TmuxBridgeNotifier(NotifyConfig(default_message_prefix="Codex turn complete"))

    notifier.send(
        NotifyEvent(event="completed", summary="", session="", cwd="", status=""),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert captured == [
        "orche notify\nsource session: -\nevent: completed\ncwd: -\n\nCodex turn complete\n\nstatus: success"
    ]


def test_tmux_bridge_notifier_appends_recent_output(monkeypatch):
    captured = []

    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: captured.append(prompt) or "%42",
    )

    notifier = TmuxBridgeNotifier(NotifyConfig())

    notifier.send(
        NotifyEvent(
            event="startup-blocked",
            summary="Codex startup blocked",
            session="source",
            cwd="/tmp/repo",
            status="startup-blocked",
            metadata={"tail_text": "line1\nline2"},
        ),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert captured == [
        "orche notify\nsource session: source\nevent: startup-blocked\ncwd: /tmp/repo\n\nCodex startup blocked\n\nRecent output:\nline1\nline2\n\nstatus: startup-blocked"
    ]


def test_tmux_bridge_notifier_preserves_multiline_prompt_for_claude_target(monkeypatch):
    captured = []

    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: captured.append((session, prompt)) or "%42",
    )

    notifier = TmuxBridgeNotifier(NotifyConfig())

    notifier.send(
        NotifyEvent(
            event="completed",
            summary="review source session output",
            session="source-session",
            cwd="/tmp/repo",
            status="success",
            metadata={"tail_text": "line1\nline2"},
        ),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert captured == [
        (
            "target-session",
            "orche notify\nsource session: source-session\nevent: completed\ncwd: /tmp/repo\n\nreview source session output\n\nRecent output:\nline1\nline2\n\nstatus: success",
        )
    ]


def test_tmux_bridge_notifier_requires_target_session():
    notifier = TmuxBridgeNotifier(NotifyConfig())

    with pytest.raises(NotifyConfigError):
        notifier.send(
            NotifyEvent(event="completed", summary="done", session="source", status="success"),
            ResolvedRoute(provider="tmux-bridge", target=""),
        )


def test_tmux_bridge_notifier_wraps_backend_errors(monkeypatch):
    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: (_ for _ in ()).throw(RuntimeError("broken bridge")),
    )
    notifier = TmuxBridgeNotifier(NotifyConfig())

    with pytest.raises(NotifyDeliveryError, match="tmux-bridge delivery failed: broken bridge"):
        notifier.send(
            NotifyEvent(event="completed", summary="done", session="source", status="success"),
            ResolvedRoute(provider="tmux-bridge", target="target-session"),
        )
