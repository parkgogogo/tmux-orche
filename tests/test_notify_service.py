from __future__ import annotations

import pytest

from orche.notify.base import Notifier
from orche.notify.config import NotifyConfig
from orche.notify.models import DeliveryResult, Message
from orche.notify.registry import NotifierRegistry
from orche.notify.service import NotificationService, dispatch_payload


class SuccessNotifier(Notifier):
    name = "alpha"

    def send(self, message):
        return DeliveryResult(provider=self.name, ok=True, detail=message.content)


class FailingNotifier(Notifier):
    name = "beta"

    def send(self, message):
        raise RuntimeError("boom")


class CapturingService:
    def __init__(self):
        self.calls = []

    def send(self, message, config):
        self.calls.append((message, config))
        return (DeliveryResult(provider="discord", ok=True, detail=message.content),)


class BaseNotifier(Notifier):
    name = "base"

    def send(self, message):
        return super().send(message)


def test_notification_service_returns_success_and_failure_results():
    registry = NotifierRegistry()
    registry.register("alpha", lambda config, http_client: SuccessNotifier())
    registry.register("beta", lambda config, http_client: FailingNotifier())
    service = NotificationService(registry=registry)

    results = service.send(
        Message(content="done", channel_id="123", session="demo", status="success"),
        NotifyConfig(providers=("alpha", "beta")),
    )

    assert list(results) == [
        DeliveryResult(provider="alpha", ok=True, detail="done"),
        DeliveryResult(provider="beta", ok=False, detail="boom"),
    ]


def test_dispatch_payload_returns_empty_when_disabled():
    results = dispatch_payload(
        '{"event":"turn-complete","summary":"done"}',
        runtime_config={"notify_enabled": False, "discord_channel_id": "123"},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_dispatch_payload_builds_message_and_uses_service():
    service = CapturingService()

    results = dispatch_payload(
        '{"event":"turn-complete","summary":"done"}',
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        summary_loader=lambda session: "",
        explicit_session="demo",
        service=service,
    )

    assert results[0].ok is True
    message, config = service.calls[0]
    assert message.session == "demo"
    assert config.providers == ("discord",)


def test_notification_service_returns_empty_when_no_notifiers():
    service = NotificationService(registry=NotifierRegistry())

    results = service.send(
        Message(content="done", channel_id="123", session="demo", status="success"),
        NotifyConfig(providers=()),
    )

    assert results == ()


def test_dispatch_payload_returns_empty_for_invalid_payload():
    results = dispatch_payload(
        "not-json",
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_notifier_base_raises_not_implemented():
    notifier = BaseNotifier()

    with pytest.raises(NotImplementedError):
        notifier.send(Message(content="done", channel_id="123", session="demo", status="success"))
