from __future__ import annotations

import pytest

import notify.tmux_bridge as tmux_bridge
from notify.config import NotifyConfig
from notify.models import NotifyEvent, ResolvedRoute

pytestmark = pytest.mark.unit


def test_tmux_bridge_notifier_delivers_to_pane_targets(monkeypatch):
    captured: list[tuple[str, str]] = []
    notifier = tmux_bridge.TmuxBridgeNotifier(NotifyConfig(provider="tmux-bridge"))

    monkeypatch.setattr(
        tmux_bridge,
        "deliver_notify_to_pane",
        lambda pane_id, prompt: captured.append((pane_id, prompt)) or pane_id,
    )
    monkeypatch.setattr(
        tmux_bridge,
        "deliver_notify_to_session",
        lambda session, prompt: pytest.fail("session delivery should not be used"),
    )

    result = notifier.send(
        NotifyEvent(
            event="completed",
            summary="done",
            session="worker",
            status="success",
            cwd="/repo",
        ),
        ResolvedRoute(provider="tmux-bridge", target="pane:%9"),
    )

    assert result.ok is True
    assert result.target == "pane:%9"
    assert captured == [
        (
            "%9",
            "orche notify\n"
            "source session: worker\n"
            "event: completed\n"
            "cwd: /repo\n\n"
            "done\n\n"
            "status: success",
        )
    ]


def test_tmux_bridge_notifier_delivers_to_session_targets(monkeypatch):
    captured: list[tuple[str, str]] = []
    notifier = tmux_bridge.TmuxBridgeNotifier(NotifyConfig(provider="tmux-bridge"))

    monkeypatch.setattr(
        tmux_bridge,
        "deliver_notify_to_session",
        lambda session, prompt: captured.append((session, prompt)) or session,
    )
    monkeypatch.setattr(
        tmux_bridge,
        "deliver_notify_to_pane",
        lambda pane_id, prompt: pytest.fail("pane delivery should not be used"),
    )

    result = notifier.send(
        NotifyEvent(
            event="completed",
            summary="done",
            session="worker",
            status="success",
            cwd="/repo",
        ),
        ResolvedRoute(provider="tmux-bridge", target="reviewer"),
    )

    assert result.ok is True
    assert result.target == "reviewer"
    assert captured == [
        (
            "reviewer",
            "orche notify\n"
            "source session: worker\n"
            "event: completed\n"
            "cwd: /repo\n\n"
            "done\n\n"
            "status: success",
        )
    ]
