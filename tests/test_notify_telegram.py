from __future__ import annotations

import pytest

from notify.config import NotifyConfig, TelegramNotifyConfig
from notify.exceptions import NotifyConfigError, NotifyDeliveryError
from notify.http import HTTPResponse
from notify.models import NotifyEvent, ResolvedRoute
from notify.telegram import DEFAULT_USER_AGENT, TelegramNotifier


def test_telegram_notifier_sends_message(fake_http_client):
    notifier = TelegramNotifier(
        NotifyConfig(
            telegram=TelegramNotifyConfig(bot_token="bot-token"),
        ),
        http_client=fake_http_client,
    )

    result = notifier.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
        ResolvedRoute(provider="telegram", target="12345"),
    )

    assert result.ok is True
    assert fake_http_client.requests[0]["url"] == "https://api.telegram.org/botbot-token/sendMessage"
    assert fake_http_client.requests[0]["headers"]["User-Agent"] == DEFAULT_USER_AGENT
    assert fake_http_client.requests[0]["json_body"]["chat_id"] == "12345"
    assert fake_http_client.requests[0]["json_body"]["parse_mode"] == "HTML"
    assert fake_http_client.requests[0]["json_body"]["text"] == "done\nsession: <code>demo</code>"


def test_telegram_notifier_requires_bot_token(fake_http_client):
    notifier = TelegramNotifier(NotifyConfig(), http_client=fake_http_client)

    with pytest.raises(NotifyConfigError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
            ResolvedRoute(provider="telegram", target="12345"),
        )


def test_telegram_notifier_requires_chat_id(fake_http_client):
    notifier = TelegramNotifier(
        NotifyConfig(telegram=TelegramNotifyConfig(bot_token="bot-token")),
        http_client=fake_http_client,
    )

    with pytest.raises(NotifyConfigError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
            ResolvedRoute(provider="telegram", target=""),
        )


def test_telegram_notifier_raises_delivery_error(fake_http_client):
    fake_http_client.responses = [HTTPResponse(500, "boom")]
    notifier = TelegramNotifier(
        NotifyConfig(telegram=TelegramNotifyConfig(bot_token="bot-token")),
        http_client=fake_http_client,
    )

    with pytest.raises(NotifyDeliveryError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
            ResolvedRoute(provider="telegram", target="12345"),
        )


def test_telegram_notifier_renders_status_cwd_session_and_tail(fake_http_client):
    notifier = TelegramNotifier(
        NotifyConfig(
            telegram=TelegramNotifyConfig(bot_token="bot-token"),
        ),
        http_client=fake_http_client,
    )

    notifier.send(
        NotifyEvent(
            event="startup-blocked",
            summary="blocked <now>",
            session="demo&1",
            cwd="/tmp/repo",
            status="startup-blocked",
            metadata={"tail_text": "line1\n<line2>"},
        ),
        ResolvedRoute(provider="telegram", target="12345"),
    )

    assert fake_http_client.requests[0]["json_body"]["text"] == (
        "[startup-blocked] blocked &lt;now&gt;\n"
        "cwd: <code>/tmp/repo</code>\n"
        "session: <code>demo&amp;1</code>\n\n"
        "Recent output:\n"
        "<pre>line1\n&lt;line2&gt;</pre>"
    )


def test_telegram_notifier_skips_session_line_when_disabled(fake_http_client):
    notifier = TelegramNotifier(
        NotifyConfig(
            include_session=False,
            telegram=TelegramNotifyConfig(bot_token="bot-token"),
        ),
        http_client=fake_http_client,
    )

    notifier.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", cwd="/tmp/repo", status="success"),
        ResolvedRoute(provider="telegram", target="12345"),
    )

    assert fake_http_client.requests[0]["json_body"]["text"] == "done\ncwd: <code>/tmp/repo</code>"
