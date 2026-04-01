from __future__ import annotations

from pathlib import Path

import sys

from typer.testing import CliRunner

import backend
import cli
from cli import app


def test_history_command_shows_recent_entries(xdg_runtime):
    backend.append_history_entry(
        "demo-session",
        {
            "timestamp": "2026-04-01T12:34:56",
            "action": "send",
            "session": "demo-session",
            "prompt": "echo hello",
        },
    )
    backend.append_history_entry(
        "demo-session",
        {
            "timestamp": "2026-04-01T12:35:10",
            "action": "cancel",
            "session": "demo-session",
        },
    )

    result = CliRunner().invoke(app, ["history", "--session", "demo-session", "--limit", "20"])

    assert result.exit_code == 0
    assert "2026-04-01T12:34:56" in result.stdout
    assert "send" in result.stdout
    assert 'prompt: "echo hello"' in result.stdout
    assert "2026-04-01T12:35:10" in result.stdout
    assert "cancel" in result.stdout


def test_history_command_shows_empty_message(xdg_runtime):
    result = CliRunner().invoke(app, ["history", "--session", "missing-session"])

    assert result.exit_code == 0
    assert "No history yet" in result.stdout


def test_config_supports_discord_mention_user_id(xdg_runtime):
    runner = CliRunner()

    set_result = runner.invoke(app, ["config", "set", "discord.mention-user-id", "123456"])
    get_result = runner.invoke(app, ["config", "get", "discord.mention-user-id"])
    list_result = runner.invoke(app, ["config", "list"])

    assert set_result.exit_code == 0
    assert "123456" in set_result.stdout
    assert get_result.exit_code == 0
    assert get_result.stdout.strip() == "123456"
    assert list_result.exit_code == 0
    assert "discord.mention-user-id" in list_result.stdout
    assert "123456" in list_result.stdout


def test_version_works_without_subcommand(xdg_runtime):
    result = CliRunner().invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.stdout.strip().startswith("orche ")


def test_session_new_expands_cwd_user_home(xdg_runtime, monkeypatch):
    runner = CliRunner()
    project_dir = xdg_runtime["home"] / "project"
    project_dir.mkdir()
    captured: dict[str, Path] = {}

    def fake_ensure_session(session, cwd, agent, **kwargs):
        captured["session"] = session
        captured["cwd"] = cwd
        captured["agent"] = agent
        return "%1"

    monkeypatch.setattr(cli, "ensure_session", fake_ensure_session)
    monkeypatch.setattr(cli, "append_action_history", lambda *args, **kwargs: None)

    result = runner.invoke(app, ["session-new", "--cwd", "~/project", "--agent", "codex"])

    assert result.exit_code == 0
    assert captured["cwd"] == project_dir.resolve()
    assert captured["agent"] == "codex"


def test_unknown_command_shows_clean_error(xdg_runtime, capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["orche", "invalidcmd"])
    exit_code = cli.main()

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Error: Unknown command: invalidcmd" in captured.err
