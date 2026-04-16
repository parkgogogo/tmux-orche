from __future__ import annotations

import pytest

from notify.base import Notifier
from notify.config import NotifyConfig
from notify.models import DeliveryResult, NotifyEvent, ResolvedRoute
from notify.registry import NotifierRegistry
from notify.service import (
    NotificationService,
    dispatch_event,
    dispatch_payload,
    resolve_routes,
)

pytestmark = pytest.mark.unit


class SuccessNotifier(Notifier):
    name = "alpha"

    def send(self, event, route):
        return DeliveryResult(
            provider=self.name, ok=True, detail=event.summary, target=route.target
        )


class FailingNotifier(Notifier):
    name = "beta"

    def send(self, event, route):
        raise RuntimeError("boom")


class CapturingService:
    def __init__(self):
        self.calls = []

    def send(self, event, routes, config):
        self.calls.append((event, routes, config))
        return (DeliveryResult(provider="discord", ok=True, detail=event.summary),)


def test_notification_service_returns_success_and_failure_results():
    registry = NotifierRegistry()
    registry.register("alpha", lambda config, http_client: SuccessNotifier())
    registry.register("beta", lambda config, http_client: FailingNotifier())
    service = NotificationService(registry=registry)

    results = service.send(
        NotifyEvent(
            event="completed", summary="done", session="demo", status="success"
        ),
        (
            ResolvedRoute(provider="alpha", target="one"),
            ResolvedRoute(provider="beta", target="two"),
        ),
        NotifyConfig(provider="alpha"),
    )

    assert list(results) == [
        DeliveryResult(provider="alpha", ok=True, detail="done", target="one"),
        DeliveryResult(provider="beta", ok=False, detail="boom", target="two"),
    ]


def test_resolve_routes_prefers_explicit_channel_and_binding_provider():
    event = NotifyEvent(
        event="completed", summary="done", session="demo", status="success"
    )

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_binding": {"provider": "telegram", "target": "chat-1"}},
        notify_config=NotifyConfig(provider="discord"),
        explicit_channel_id=" 123 456 ",
    )

    assert routes == (
        ResolvedRoute(provider="telegram", target="123456", session="demo"),
    )


def test_resolve_routes_preserves_binding_metadata():
    event = NotifyEvent(
        event="completed", summary="done", session="demo", status="success"
    )

    routes = resolve_routes(
        event=event,
        runtime_config={
            "notify_binding": {
                "provider": "telegram",
                "target": "chat-1",
                "thread": "ops",
            }
        },
        notify_config=NotifyConfig(provider="telegram"),
    )

    assert routes == (
        ResolvedRoute(
            provider="telegram",
            target="chat-1",
            session="demo",
            metadata={"thread": "ops"},
        ),
    )


def test_dispatch_payload_short_circuits_when_disabled():
    results = dispatch_payload(
        '{"event":"turn-complete","summary":"done"}',
        runtime_config={"notify_enabled": False, "discord_channel_id": "123"},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_dispatch_event_uses_supplied_routes_and_service():
    service = CapturingService()
    event = NotifyEvent(
        event="stalled", summary="stuck", session="demo", status="warning"
    )

    results = dispatch_event(
        event,
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        routes=(ResolvedRoute(provider="discord", target="123", session="demo"),),
        service=service,
    )

    sent_event, routes, config = service.calls[0]
    assert results[0].ok is True
    assert sent_event.event == "stalled"
    assert routes[0].target == "123"
    assert config.provider == "discord"
