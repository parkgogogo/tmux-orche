from __future__ import annotations

from typer.testing import CliRunner

from orche import backend
from orche.cli import app


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
