from __future__ import annotations

from pathlib import Path

import pytest

import backend
import cli
from agents import AgentRuntime
from session.meta import load_meta

pytestmark = pytest.mark.unit


class _FakePlugin:
    name = "codex"
    display_name = "Codex"
    runtime_label = "CODEX_HOME"


def test_create_session_allows_missing_notify_binding(xdg_runtime, monkeypatch):
    plugin = _FakePlugin()
    monkeypatch.setattr(backend, "get_agent", lambda agent: plugin)
    monkeypatch.setattr(backend, "ensure_pane", lambda *args, **kwargs: "%1")
    monkeypatch.setattr(
        backend,
        "prepare_managed_runtime",
        lambda plugin, session, *, cwd, discord_channel_id: AgentRuntime(
            home=str(Path(cwd) / ".runtime"),
            managed=True,
            label=plugin.runtime_label,
        ),
    )
    monkeypatch.setattr(backend, "is_agent_running", lambda *args, **kwargs: False)
    monkeypatch.setattr(backend, "initialize_session_startup", lambda session: {})
    monkeypatch.setattr(backend, "ensure_agent_running", lambda *args, **kwargs: "%1")
    monkeypatch.setattr(
        backend, "wait_for_managed_startup_ready", lambda *args, **kwargs: "%1"
    )

    pane_id = backend.create_session(
        "demo-session",
        Path(xdg_runtime["home"]),
        "codex",
    )
    meta = load_meta("demo-session")

    assert pane_id == "%1"
    assert meta["session"] == "demo-session"
    assert meta["agent"] == "codex"
    assert "notify_binding" not in meta
    assert meta["runtime_home"]


def test_open_session_rejects_raw_agent_cli_args(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "session_exists", lambda session: False)

    with pytest.raises(backend.OrcheError, match="does not support raw agent CLI args"):
        cli._open_session(
            cwd=tmp_path,
            agent="codex",
            name="demo-session",
            notify=None,
            cli_args=["--help"],
        )


def test_open_session_resolves_tmux_self_notify(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    monkeypatch.setattr(cli, "session_exists", lambda session: False)
    monkeypatch.setattr(cli, "current_pane_id", lambda: "%9")
    monkeypatch.setattr(
        cli,
        "create_session",
        lambda session, cwd, agent, *, notify_to, notify_target: (
            captured.update(
                {
                    "session": session,
                    "cwd": str(cwd),
                    "agent": agent,
                    "notify_to": str(notify_to),
                    "notify_target": str(notify_target),
                }
            )
            or "%1"
        ),
    )
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    session, pane_id = cli._open_session(
        cwd=tmp_path,
        agent="codex",
        name="demo-session",
        notify="tmux:self",
        cli_args=[],
    )

    assert session == "demo-session"
    assert pane_id == "%1"
    assert captured["notify_to"] == "tmux-bridge"
    assert captured["notify_target"] == "pane:%9"


def test_open_session_rejects_tmux_self_outside_live_tmux(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "session_exists", lambda session: False)
    monkeypatch.setattr(cli, "current_pane_id", lambda: "")

    with pytest.raises(
        backend.OrcheError,
        match="--notify tmux:self requires running inside a live tmux pane",
    ):
        cli._open_session(
            cwd=tmp_path,
            agent="codex",
            name="demo-session",
            notify="tmux:self",
            cli_args=[],
        )


def test_open_session_resolves_explicit_tmux_pane_notify(monkeypatch, tmp_path):
    captured: dict[str, str] = {}

    monkeypatch.setattr(cli, "session_exists", lambda session: False)
    monkeypatch.setattr(cli, "pane_exists", lambda pane_id: pane_id == "%12")
    monkeypatch.setattr(
        cli,
        "create_session",
        lambda session, cwd, agent, *, notify_to, notify_target: (
            captured.update(
                {
                    "notify_to": str(notify_to),
                    "notify_target": str(notify_target),
                }
            )
            or "%1"
        ),
    )
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    _session, pane_id = cli._open_session(
        cwd=tmp_path,
        agent="codex",
        name="demo-session",
        notify="tmux:%12",
        cli_args=[],
    )

    assert pane_id == "%1"
    assert captured["notify_to"] == "tmux-bridge"
    assert captured["notify_target"] == "pane:%12"


def test_open_session_rejects_dead_explicit_tmux_pane_notify(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "session_exists", lambda session: False)
    monkeypatch.setattr(cli, "pane_exists", lambda pane_id: False)

    with pytest.raises(
        backend.OrcheError,
        match="--notify tmux:%12 requires a live tmux pane target",
    ):
        cli._open_session(
            cwd=tmp_path,
            agent="codex",
            name="demo-session",
            notify="tmux:%12",
            cli_args=[],
        )
