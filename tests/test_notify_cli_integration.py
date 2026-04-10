from __future__ import annotations

import signal

from typer.testing import CliRunner

from cli import app
from notify.http import HTTPResponse

from .conftest import FakeHTTPClient, write_runtime_config


class StubHTTPClientFactory:
    def __init__(self, client):
        self.client = client

    def __call__(self):
        return self.client


def test_notify_hidden_command_requires_explicit_route(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
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
        ["notify-internal"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify skipped: no notifier routes resolved" in result.output
    assert fake_client.requests == []


def test_notify_hidden_command_sends_message_with_explicit_channel(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
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
        ["notify-internal", "--channel-id", "1234567890"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=discord detail=200" in result.output
    assert fake_client.requests[0]["url"].endswith("/channels/1234567890/messages")
    assert fake_client.requests[0]["json_body"]["content"].startswith("Done")


def test_notify_hidden_command_session_start_updates_internal_startup_state_without_delivery(xdg_runtime):
    from backend import load_meta, save_meta

    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": False,
            "session": "repo-claude-main",
            "cwd": "/tmp/repo",
        },
    )
    save_meta(
        "repo-claude-main",
        {
            "session": "repo-claude-main",
            "cwd": "/tmp/repo",
            "agent": "claude",
            "startup": {
                "state": "launching",
                "started_at": 1.0,
            },
        },
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-claude-main"],
        input='{"hook_event_name":"SessionStart","source":"startup"}',
    )

    assert result.exit_code == 0
    assert "notify ok: internal event=session-start detail=startup-ready" in result.output
    assert load_meta("repo-claude-main")["startup"]["state"] == "ready"


def test_notify_hidden_command_prompt_submit_acknowledges_pending_turn_without_delivery(xdg_runtime):
    from backend import load_meta, save_meta

    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": False,
            "session": "repo-claude-main",
            "cwd": "/tmp/repo",
        },
    )
    save_meta(
        "repo-claude-main",
        {
            "session": "repo-claude-main",
            "cwd": "/tmp/repo",
            "agent": "claude",
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "hello",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {},
                "prompt_ack": {
                    "state": "pending",
                    "accepted_at": 0.0,
                    "source": "",
                },
            },
        },
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-claude-main"],
        input='{"hook_event_name":"UserPromptSubmit","prompt":"hello"}',
    )

    assert result.exit_code == 0
    assert "notify ok: internal event=prompt-accepted detail=pending-turn-updated" in result.output
    assert load_meta("repo-claude-main")["pending_turn"]["prompt_ack"]["state"] == "accepted"


def test_notify_hidden_command_verbose_prints_config_and_event(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
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
        ["notify-internal", "--verbose"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify config:" in result.output
    assert "discord.bot_token: set" in result.output
    assert "runtime.channel_id: 1234567890" in result.output
    assert "notify event:" in result.output
    assert "summary:" in result.output
    assert "Done" in result.output
    assert "routes:" in result.output
    assert "<none>" in result.output
    assert "notify skipped: no notifier routes resolved" in result.output


def test_notify_hidden_command_failure_prints_error_and_returns_nonzero(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient(responses=[HTTPResponse(403, '{"message":"forbidden"}')])
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
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
        ["notify-internal"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify skipped: no notifier routes resolved" in result.output


def test_notify_hidden_command_prefers_session_meta_channel_over_global_config(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "discord_channel_id": "2222222222",
            "discord_session": "agent:main:discord:channel:2222222222",
            "session": "other-session",
            "cwd": "/tmp/other",
        },
    )

    from backend import save_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
        },
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert fake_client.requests[0]["url"].endswith("/channels/1111111111/messages")


def test_notify_hidden_command_uses_session_notify_binding_for_tmux_bridge(xdg_runtime, monkeypatch):
    captured = []
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "session": "other-session",
            "cwd": "/tmp/other",
        },
    )

    from backend import save_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "tmux-bridge",
                "target": "target-session",
            },
        },
    )

    monkeypatch.setattr(
        "notify.tmux_bridge.deliver_notify_to_session",
        lambda session, prompt: captured.append((session, prompt)) or "%2",
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=tmux-bridge detail=delivered" in result.output
    assert captured[0][0] == "target-session"


def test_notify_hidden_command_uses_session_notify_binding_for_telegram(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.telegram.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "telegram_bot_token": "bot-token",
            "session": "other-session",
            "cwd": "/tmp/other",
        },
    )

    from backend import save_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "telegram",
                "target": "12345",
            },
        },
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=telegram detail=200" in result.output
    assert fake_client.requests[0]["url"] == "https://api.telegram.org/botbot-token/sendMessage"
    assert fake_client.requests[0]["json_body"]["chat_id"] == "12345"


def test_notify_hidden_command_deduplicates_same_turn_event(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    from backend import save_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "hello",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {},
            },
        },
    )

    payload = '{"event":"completed","summary":"Done","metadata":{"turn_id":"turn-1","source":"watchdog"}}'
    runner = CliRunner()
    first = runner.invoke(app, ["notify-internal", "--session", "repo-codex-main"], input=payload)
    second = runner.invoke(app, ["notify-internal", "--session", "repo-codex-main"], input=payload)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert len(fake_client.requests) == 1
    assert "notify skipped: duplicate completed event" in second.output


def test_notify_hidden_command_native_completed_finalizes_pending_turn_and_stops_watchdog(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    from backend import save_meta, load_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
            "pending_turn": {
                "turn_id": "turn-1",
                "prompt": "hello",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "pid": 4242,
                },
            },
        },
    )

    killed = []
    monkeypatch.setattr("backend.process_is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr("backend.os.kill", lambda pid, sig: killed.append((pid, sig)))

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"event":"turn-complete","summary":"Done"}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=discord detail=200" in result.output
    assert killed == [(4242, signal.SIGTERM)]
    meta = load_meta("repo-codex-main")
    assert "pending_turn" not in meta
    assert meta["last_completed_turn"]["summary"] == "Done"
    assert meta["last_completed_turn"]["notifications"] == {}


def test_notify_hidden_command_native_completed_matches_pending_prompt_when_turn_id_differs(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    from backend import save_meta, load_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
            "pending_turn": {
                "turn_id": "orche-turn-1",
                "prompt": "只回复 OK",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {},
                "watchdog": {
                    "pid": 4242,
                },
            },
        },
    )

    killed = []
    monkeypatch.setattr("backend.process_is_alive", lambda pid: pid == 4242)
    monkeypatch.setattr("backend.os.kill", lambda pid, sig: killed.append((pid, sig)))

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"type":"agent-turn-complete","thread-id":"thread-1","turn-id":"codex-turn-9","last-assistant-message":"OK","input-messages":["只回复 OK"]}',
    )

    assert result.exit_code == 0
    meta = load_meta("repo-codex-main")
    assert "pending_turn" not in meta
    assert meta["last_completed_turn"]["summary"] == "OK"
    assert meta["last_completed_turn"]["notifications"] == {}
    assert killed == [(4242, signal.SIGTERM)]


def test_notify_hidden_command_new_completed_does_not_deduplicate_against_old_last_turn(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    from backend import save_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
            "last_completed_turn": {
                "turn_id": "orche-turn-old",
                "prompt": "old prompt",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {
                    "completed": {
                        "at": 1.0,
                        "source": "hook",
                        "status": "success",
                        "summary": "Old",
                    }
                },
                "summary": "Old",
                "completed_at": 2.0,
            },
        },
    )

    result = CliRunner().invoke(
        app,
        ["notify-internal", "--session", "repo-codex-main"],
        input='{"type":"agent-turn-complete","thread-id":"thread-1","turn-id":"codex-turn-9","last-assistant-message":"New","input-messages":["new prompt"]}',
    )

    assert result.exit_code == 0
    assert "notify ok: provider=discord detail=200" in result.output
    assert len(fake_client.requests) == 1


def test_notify_hidden_command_native_completed_bypasses_existing_watchdog_completed_marker(xdg_runtime, monkeypatch):
    fake_client = FakeHTTPClient()
    monkeypatch.setattr("notify.discord.UrllibHTTPClient", StubHTTPClientFactory(fake_client))
    write_runtime_config(
        xdg_runtime["config_path"],
        {
            "notify_enabled": True,
            "discord_bot_token": "bot-token",
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
        },
    )

    from backend import save_meta, load_meta

    save_meta(
        "repo-codex-main",
        {
            "session": "repo-codex-main",
            "cwd": "/tmp/repo",
            "agent": "codex",
            "pane_id": "%1",
            "notify_binding": {
                "provider": "discord",
                "target": "1111111111",
                "session": "agent:main:discord:channel:1111111111",
            },
            "pending_turn": {
                "turn_id": "orche-turn-1",
                "prompt": "只回复 OK",
                "submitted_at": 1.0,
                "pane_id": "%1",
                "notifications": {
                    "completed": {
                        "at": 1.0,
                        "source": "watchdog",
                        "status": "success",
                        "summary": "Stale",
                    }
                },
            },
        },
    )

    payload = '{"type":"agent-turn-complete","thread-id":"thread-1","turn-id":"codex-turn-9","last-assistant-message":"OK","input-messages":["只回复 OK"]}'
    runner = CliRunner()
    first = runner.invoke(app, ["notify-internal", "--session", "repo-codex-main"], input=payload)
    second = runner.invoke(app, ["notify-internal", "--session", "repo-codex-main"], input=payload)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "notify ok: provider=discord detail=200" in first.output
    assert "notify ok: provider=discord detail=200" in second.output
    assert len(fake_client.requests) == 2
    meta = load_meta("repo-codex-main")
    assert meta["last_completed_turn"]["summary"] == "OK"
