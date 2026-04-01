from __future__ import annotations

from orche.notify.config import DEFAULT_MENTION_USER_ID, load_notify_config


def test_load_notify_config_reads_env_and_targets(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
    config = load_notify_config(
        {
            "notify_enabled": "true",
            "notify_targets": ["discord", "discord"],
            "discord_bot_token": "file-token",
            "notify_include_cwd": "false",
            "notify_include_session": "no",
            "notify_timeout_seconds": "11",
        }
    )

    assert config.enabled is True
    assert config.providers == ("discord", "discord")
    assert config.include_cwd is False
    assert config.include_session is False
    assert config.discord.bot_token == "env-token"
    assert config.discord.timeout_seconds == 11


def test_load_notify_config_defaults_are_stable(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({})

    assert config.providers == ("discord",)
    assert config.default_message_prefix == "Codex turn complete"
    assert config.discord.mention_user_id == DEFAULT_MENTION_USER_ID


def test_load_notify_config_handles_string_targets_and_invalid_values(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config(
        {
            "notify_enabled": "maybe",
            "notify_targets": "discord;telegram",
            "notify_timeout_seconds": "oops",
        }
    )

    assert config.enabled is True
    assert config.providers == ("discord", "telegram")
    assert config.discord.timeout_seconds == 8


def test_load_notify_config_handles_unknown_target_container(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_targets": {"discord"}})

    assert config.providers == ("discord",)
