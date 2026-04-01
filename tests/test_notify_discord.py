from __future__ import annotations

import pytest

from orche.notify.config import DiscordNotifyConfig
from orche.notify.discord import DiscordNotifier
from orche.notify.exceptions import NotifyConfigError, NotifyDeliveryError
from orche.notify.http import HTTPResponse
from orche.notify.models import Message


def test_discord_notifier_sends_via_bot_token(fake_http_client):
    notifier = DiscordNotifier(
        DiscordNotifyConfig(bot_token="bot-token", mention_user_id="42"),
        http_client=fake_http_client,
    )

    result = notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))

    assert result.ok is True
    assert fake_http_client.requests[0]["headers"]["Authorization"] == "Bot bot-token"
    assert fake_http_client.requests[0]["json_body"]["allowed_mentions"]["users"] == ["42"]


def test_discord_notifier_sends_via_webhook(fake_http_client):
    notifier = DiscordNotifier(
        DiscordNotifyConfig(webhook_url="https://discord.test/webhook"),
        http_client=fake_http_client,
    )

    notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))

    assert fake_http_client.requests[0]["url"] == "https://discord.test/webhook"
    assert "Authorization" not in fake_http_client.requests[0]["headers"]


def test_discord_notifier_requires_token_or_webhook(fake_http_client):
    notifier = DiscordNotifier(DiscordNotifyConfig(), http_client=fake_http_client)

    with pytest.raises(NotifyConfigError):
        notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))


def test_discord_notifier_requires_channel_for_bot_delivery(fake_http_client):
    notifier = DiscordNotifier(DiscordNotifyConfig(bot_token="bot-token"), http_client=fake_http_client)

    with pytest.raises(NotifyConfigError):
        notifier.send(Message(content="done", channel_id="", session="demo", status="success"))


def test_discord_notifier_raises_delivery_error(fake_http_client):
    fake_http_client.responses = [HTTPResponse(500, "boom")]
    notifier = DiscordNotifier(DiscordNotifyConfig(bot_token="bot-token"), http_client=fake_http_client)

    with pytest.raises(NotifyDeliveryError):
        notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))


def test_discord_notifier_supports_empty_mentions(fake_http_client):
    notifier = DiscordNotifier(
        DiscordNotifyConfig(bot_token="bot-token", mention_user_id=""),
        http_client=fake_http_client,
    )

    notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))

    assert fake_http_client.requests[0]["json_body"]["allowed_mentions"] == {"parse": []}
