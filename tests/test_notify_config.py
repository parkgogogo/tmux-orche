from __future__ import annotations

from notify.config import DEFAULT_MENTION_USER_ID, load_notify_config


def test_load_notify_config_reads_env_and_provider(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-telegram-token")
    config = load_notify_config(
        {
            "notify_enabled": "true",
            "notify_provider": "discord",
            "discord_bot_token": "file-token",
            "telegram_bot_token": "file-telegram-token",
            "notify_include_cwd": "false",
            "notify_include_session": "no",
            "notify_timeout_seconds": "11",
        }
    )

    assert config.enabled is True
    assert config.provider == "discord"
    assert config.include_cwd is False
    assert config.include_session is False
    assert config.discord.bot_token == "env-token"
    assert config.telegram.bot_token == "env-telegram-token"
    assert config.discord.timeout_seconds == 11
    assert config.telegram.timeout_seconds == 11


def test_load_notify_config_defaults_are_stable(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MENTION_USER_ID", raising=False)
    config = load_notify_config({})

    assert config.provider == "discord"
    assert config.default_message_prefix == "Agent turn complete"
    assert config.discord.mention_user_id == DEFAULT_MENTION_USER_ID
    assert config.telegram.bot_token == ""


def test_load_notify_config_reads_telegram_provider_from_config(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    config = load_notify_config(
        {
            "notify_provider": "telegram",
            "telegram_bot_token": "file-telegram-token",
        }
    )

    assert config.provider == "telegram"
    assert config.telegram.bot_token == "file-telegram-token"


def test_load_notify_config_reads_mention_user_from_env(monkeypatch):
    monkeypatch.setenv("MENTION_USER_ID", "123456")

    config = load_notify_config({})

    assert config.discord.mention_user_id == "123456"


def test_load_notify_config_handles_legacy_targets_and_invalid_values(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config(
        {
            "notify_enabled": "maybe",
            "notify_targets": "discord;telegram",
            "notify_timeout_seconds": "oops",
        }
    )

    assert config.enabled is True
    assert config.provider == "discord"
    assert config.discord.timeout_seconds == 8


def test_load_notify_config_handles_unknown_provider_container(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_targets": {"discord"}})

    assert config.provider == "discord"


def test_load_notify_config_defaults_empty_string_provider_to_discord(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_provider": " ; , "})

    assert config.provider == "discord"


def test_load_notify_config_uses_first_provider_from_sequence(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_providers": ["tmux-bridge", "discord"]})

    assert config.provider == "tmux-bridge"


def test_load_notify_config_skips_blank_sequence_entries(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_providers": ["", "  ", "tmux-bridge"]})

    assert config.provider == "tmux-bridge"


def test_load_notify_config_defaults_all_blank_sequence_to_discord(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    config = load_notify_config({"notify_providers": ["", "  "]})

    assert config.provider == "discord"
