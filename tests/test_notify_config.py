from __future__ import annotations

import pytest

from notify.config import DEFAULT_MENTION_USER_ID, load_notify_config

pytestmark = pytest.mark.unit


def test_load_notify_config_prefers_env_and_normalizes_flags(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-token")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-telegram")

    config = load_notify_config(
        {
            "notify_enabled": "true",
            "notify_provider": "discord",
            "discord_bot_token": "file-token",
            "telegram_bot_token": "file-telegram",
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
    assert config.telegram.bot_token == "env-telegram"
    assert config.discord.timeout_seconds == 11


def test_load_notify_config_defaults_provider_and_invalid_values(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.delenv("MENTION_USER_ID", raising=False)

    config = load_notify_config(
        {
            "notify_enabled": "maybe",
            "notify_targets": " ; , ",
            "notify_timeout_seconds": "oops",
        }
    )

    assert config.enabled is True
    assert config.provider == "discord"
    assert config.discord.timeout_seconds == 8
    assert config.discord.mention_user_id == DEFAULT_MENTION_USER_ID
