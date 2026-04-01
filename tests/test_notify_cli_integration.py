from __future__ import annotations

from typer.testing import CliRunner

from orche.cli import app
from orche.notify.http import HTTPResponse

from .conftest import FakeHTTPClient, write_runtime_config


class StubHTTPClientFactory:
    def __init__(self, client):
        self.client = client

    def __call__(self):
        return self.client


def test_notify_hidden_command_reads_stdin_and_sends_message(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("orche.notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "discord_channel_id": "1234567890",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    result = CliRunner().invoke(
        app,
        ["_notify-discord"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=discord detail=200" in result.output
    assert fake_client.requests[0]["url"].endswith("/channels/1234567890/messages")
    assert fake_client.requests[0]["json_body"]["content"].startswith("<@1475734550813605959> Done")


def test_notify_hidden_command_verbose_prints_config_and_message(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("orche.notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "discord_channel_id": "1234567890",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    result = CliRunner().invoke(
        app,
        ["_notify-discord", "--verbose"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify config:" in result.output
    assert "discord.bot_token: set" in result.output
    assert "runtime.channel_id: 1234567890" in result.output
    assert "notify message:" in result.output
    assert "content:" in result.output
    assert "<@1475734550813605959> Done" in result.output
    assert "notify ok: provider=discord detail=200" in result.output


def test_notify_hidden_command_failure_prints_error_and_returns_nonzero(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient(responses=[HTTPResponse(403, '{"message":"forbidden"}')])
    monkeypatch.setattr("orche.notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "discord_channel_id": "1234567890",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    result = CliRunner().invoke(
        app,
        ["_notify-discord"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 1
    assert "notify failed: provider=discord detail=discord delivery failed with status=403" in result.output
