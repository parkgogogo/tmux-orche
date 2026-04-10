from __future__ import annotations

import pytest

from notify.base import Notifier
from notify.config import NotifyConfig
from notify.models import DeliveryResult, NotifyEvent, ResolvedRoute
from notify.registry import NotifierRegistry
from notify.service import MAX_NOTIFY_WORKERS, NotificationService, dispatch_event, dispatch_payload, resolve_routes


class SuccessNotifier(Notifier):
    name = "alpha"

    def send(self, event, route):
        return DeliveryResult(provider=self.name, ok=True, detail=event.summary, target=route.target)


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


class BaseNotifier(Notifier):
    name = "base"

    def send(self, event, route):
        return super().send(event, route)


class EmptyRegistry(NotifierRegistry):
    def create_many_for(self, providers, config, *, http_client=None):
        _ = (providers, config, http_client)
        return []


def test_notification_service_returns_success_and_failure_results():
    registry = NotifierRegistry()
    registry.register("alpha", lambda config, http_client: SuccessNotifier())
    registry.register("beta", lambda config, http_client: FailingNotifier())
    service = NotificationService(registry=registry)

    results = service.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
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
        explicit_channel_id="123",
        explicit_session="demo",
        service=service,
    )

    assert results[0].ok is True
    event, routes, config = service.calls[0]
    assert event.session == "demo"
    assert routes[0].provider == "discord"
    assert config.provider == "discord"


def test_dispatch_event_uses_supplied_event_and_routes():
    service = CapturingService()
    event = NotifyEvent(event="stalled", summary="stuck", session="demo", status="warning")

    results = dispatch_event(
        event,
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        routes=(ResolvedRoute(provider="discord", target="123", session="demo"),),
        service=service,
    )

    assert results[0].ok is True
    sent_event, routes, _config = service.calls[0]
    assert sent_event.event == "stalled"
    assert routes[0].target == "123"


def test_dispatch_event_returns_empty_when_disabled():
    service = CapturingService()
    event = NotifyEvent(event="failed", summary="boom", session="demo", status="failure")

    results = dispatch_event(
        event,
        runtime_config={"notify_enabled": False, "discord_channel_id": "123"},
        service=service,
    )

    assert results == ()
    assert service.calls == []


def test_notification_service_returns_empty_when_no_notifiers():
    service = NotificationService(registry=NotifierRegistry())

    results = service.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
        (),
        NotifyConfig(provider=""),
    )

    assert results == ()


def test_notification_service_marks_route_without_notifier_as_failure():
    registry = NotifierRegistry()
    registry.register("alpha", lambda config, http_client: SuccessNotifier())
    service = NotificationService(registry=registry)

    results = service.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
        (ResolvedRoute(provider="beta", target="missing"),),
        NotifyConfig(provider="alpha"),
    )

    assert results == (
        DeliveryResult(
            provider="beta",
            ok=False,
            detail="Unsupported notifier: beta. Supported notifiers: alpha",
            target="missing",
        ),
    )


def test_notification_service_marks_empty_factory_result_as_failure():
    service = NotificationService(registry=EmptyRegistry())

    results = service.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
        (ResolvedRoute(provider="alpha", target="missing"),),
        NotifyConfig(provider="alpha"),
    )

    assert results == (
        DeliveryResult(
            provider="alpha",
            ok=False,
            detail="Unsupported notifier: alpha. Supported notifiers: ",
            target="missing",
        ),
    )


def test_notification_service_caps_thread_pool_workers(monkeypatch):
    registry = NotifierRegistry()
    registry.register("alpha", lambda config, http_client: SuccessNotifier())
    service = NotificationService(registry=registry)
    captured = {}

    class FakeExecutor:
        def __init__(self, *, max_workers):
            captured["max_workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn, event, route):
            class FakeFuture:
                def result(self_inner):
                    return fn(event, route)

            return FakeFuture()

    monkeypatch.setattr("notify.service.ThreadPoolExecutor", FakeExecutor)
    monkeypatch.setattr("notify.service.as_completed", lambda futures: list(futures))

    routes = tuple(ResolvedRoute(provider="alpha", target=str(index)) for index in range(MAX_NOTIFY_WORKERS + 5))
    results = service.send(
        NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
        routes,
        NotifyConfig(provider="alpha"),
    )

    assert captured["max_workers"] == MAX_NOTIFY_WORKERS
    assert len(results) == len(routes)


def test_dispatch_payload_returns_empty_for_invalid_payload():
    results = dispatch_payload(
        "not-json",
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_dispatch_payload_returns_empty_for_oversized_payload():
    payload = '{"summary":"' + ("x" * (10 * 1024 * 1024)) + '"}'

    results = dispatch_payload(
        payload,
        runtime_config={"discord_channel_id": "123", "discord_bot_token": "token"},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_resolve_routes_uses_explicit_channel_for_discord():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"discord_channel_id": "123"},
        notify_config=NotifyConfig(provider="discord"),
        explicit_channel_id="456",
    )

    assert routes == (ResolvedRoute(provider="discord", target="456", session="demo"),)


def test_resolve_routes_uses_session_notify_binding_for_tmux_bridge():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_binding": {"provider": "tmux-bridge", "target": "target-session"}},
        notify_config=NotifyConfig(provider="discord"),
    )

    assert routes == (
        ResolvedRoute(
            provider="tmux-bridge",
            target="target-session",
            session="demo",
            metadata={},
        ),
    )


def test_resolve_routes_uses_session_notify_binding_for_telegram():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_binding": {"provider": "telegram", "target": "12345", "thread": "ops"}},
        notify_config=NotifyConfig(provider="telegram"),
    )

    assert routes == (
        ResolvedRoute(
            provider="telegram",
            target="12345",
            session="demo",
            metadata={"thread": "ops"},
        ),
    )


def test_resolve_routes_uses_explicit_target_for_telegram_binding():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_binding": {"provider": "telegram", "target": "12345"}},
        notify_config=NotifyConfig(provider="telegram"),
        explicit_channel_id="67890",
    )

    assert routes == (ResolvedRoute(provider="telegram", target="67890", session="demo"),)


def test_resolve_routes_skips_discord_without_channel_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={},
        notify_config=NotifyConfig(provider="discord"),
    )

    assert routes == ()


def test_resolve_routes_skips_telegram_without_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_binding": {"provider": "telegram", "target": ""}},
        notify_config=NotifyConfig(provider="telegram"),
    )

    assert routes == ()


def test_resolve_routes_prefers_session_binding_over_global_discord_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={
            "notify_binding": {"provider": "discord", "target": "789"},
            "discord_channel_id": "123",
        },
        notify_config=NotifyConfig(provider="discord"),
    )

    assert routes == (ResolvedRoute(provider="discord", target="789", session="demo"),)


def test_resolve_routes_falls_back_to_global_target_when_binding_target_missing():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={
            "notify_binding": {"provider": "discord", "target": ""},
            "discord_channel_id": "123",
        },
        notify_config=NotifyConfig(provider="discord"),
    )

    assert routes == ()


def test_resolve_routes_ignores_empty_tmux_binding_and_falls_back_to_global_discord():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={
            "notify_binding": {"provider": "tmux-bridge", "target": ""},
            "discord_channel_id": "123",
        },
        notify_config=NotifyConfig(provider="discord"),
    )

    assert routes == ()


def test_resolve_routes_uses_global_tmux_provider_with_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={
            "notify_provider": "tmux-bridge",
            "notify_target_session": "target-session",
        },
        notify_config=NotifyConfig(provider="tmux-bridge"),
    )

    assert routes == ()


def test_resolve_routes_skips_global_tmux_provider_without_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={"notify_provider": "tmux-bridge"},
        notify_config=NotifyConfig(provider="tmux-bridge"),
    )

    assert routes == ()


def test_resolve_routes_skips_tmux_provider_without_any_runtime_target():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={},
        notify_config=NotifyConfig(provider="tmux-bridge"),
    )

    assert routes == ()


def test_resolve_routes_returns_empty_when_provider_is_blank():
    event = NotifyEvent(event="turn-complete", summary="done", session="demo", status="success")

    routes = resolve_routes(
        event=event,
        runtime_config={},
        notify_config=NotifyConfig(provider=""),
    )

    assert routes == ()


def test_dispatch_payload_returns_empty_when_event_has_no_routes():
    results = dispatch_payload(
        '{"event":"turn-complete","summary":"done"}',
        runtime_config={"notify_enabled": True},
        summary_loader=lambda session: "",
    )

    assert results == ()


def test_notifier_base_raises_not_implemented():
    notifier = BaseNotifier()

    with pytest.raises(NotImplementedError):
        notifier.send(
            NotifyEvent(event="turn-complete", summary="done", session="demo", status="success"),
            ResolvedRoute(provider="base"),
        )
