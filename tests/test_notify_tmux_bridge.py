from __future__ import annotations

import pytest

from notify.config import NotifyConfig
from notify.exceptions import NotifyConfigError, NotifyDeliveryError
from notify.models import NotifyEvent, ResolvedRoute
from notify.tmux_bridge import TmuxBridgeNotifier


def test_tmux_bridge_notifier_types_prompt_and_presses_enter(monkeypatch):
    actions = []

    monkeypatch.setattr("notify.tmux_bridge.bridge_resolve", lambda session: "%42" if session == "target-session" else None)
    monkeypatch.setattr("notify.tmux_bridge.bridge_type", lambda session, text: actions.append(("type", session, text)))
    monkeypatch.setattr("notify.tmux_bridge.bridge_keys", lambda session, keys: actions.append(("keys", session, list(keys))))

    notifier = TmuxBridgeNotifier(NotifyConfig())

    result = notifier.send(
        NotifyEvent(
            event="turn-complete",
            summary="review source session output",
            session="source-session",
            cwd="/tmp/repo",
            status="success",
        ),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert result.ok is True
    assert result.target == "target-session"
    assert actions == [
        (
            "type",
            "target-session",
            "orche notify\nsource session: source-session\nstatus: success\ncwd: /tmp/repo\n\nreview source session output",
        ),
        ("keys", "target-session", ["Enter"]),
    ]


def test_tmux_bridge_notifier_uses_default_prefix_for_empty_summary(monkeypatch):
    actions = []

    monkeypatch.setattr("notify.tmux_bridge.bridge_resolve", lambda session: "%42")
    monkeypatch.setattr("notify.tmux_bridge.bridge_type", lambda session, text: actions.append(text))
    monkeypatch.setattr("notify.tmux_bridge.bridge_keys", lambda session, keys: None)

    notifier = TmuxBridgeNotifier(NotifyConfig(default_message_prefix="Codex turn complete"))

    notifier.send(
        NotifyEvent(event="turn-complete", summary="", session="", cwd="", status=""),
        ResolvedRoute(provider="tmux-bridge", target="target-session"),
    )

    assert actions == [
        "orche notify\nsource session: -\nstatus: success\ncwd: -\n\nCodex turn complete"
    ]


def test_tmux_bridge_notifier_requires_target_session():
    notifier = TmuxBridgeNotifier(NotifyConfig())

    with pytest.raises(NotifyConfigError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="source", status="success"),
            ResolvedRoute(provider="tmux-bridge", target=""),
        )


def test_tmux_bridge_notifier_requires_existing_target_session(monkeypatch):
    monkeypatch.setattr("notify.tmux_bridge.bridge_resolve", lambda session: None)
    notifier = TmuxBridgeNotifier(NotifyConfig())

    with pytest.raises(NotifyDeliveryError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="source", status="success"),
            ResolvedRoute(provider="tmux-bridge", target="missing-session"),
        )


def test_tmux_bridge_notifier_wraps_bridge_errors(monkeypatch):
    monkeypatch.setattr("notify.tmux_bridge.bridge_resolve", lambda session: "%42")

    def raise_error(session, text):
        raise RuntimeError("broken bridge")

    monkeypatch.setattr("notify.tmux_bridge.bridge_type", raise_error)
    monkeypatch.setattr("notify.tmux_bridge.bridge_keys", lambda session, keys: None)
    notifier = TmuxBridgeNotifier(NotifyConfig())

    with pytest.raises(NotifyDeliveryError, match="tmux-bridge delivery failed: broken bridge"):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="source", status="success"),
            ResolvedRoute(provider="tmux-bridge", target="target-session"),
        )
