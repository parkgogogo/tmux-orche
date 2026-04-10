from __future__ import annotations

import pytest

from notify.base import Notifier
from notify.config import NotifyConfig
from notify.exceptions import NotifyConfigError
from notify.models import DeliveryResult
from notify.registry import DEFAULT_REGISTRY, NotifierRegistry


class DummyNotifier(Notifier):
    name = "dummy"

    def send(self, event, route):
        return DeliveryResult(provider=self.name, ok=True, detail=event.summary, target=route.target)


def test_registry_registers_custom_notifier():
    registry = NotifierRegistry()
    registry.register("dummy", lambda config, http_client: DummyNotifier())

    notifiers = registry.create_many(NotifyConfig(provider="dummy"))

    assert [notifier.name for notifier in notifiers] == ["dummy"]


def test_registry_rejects_unknown_provider():
    registry = NotifierRegistry()

    with pytest.raises(NotifyConfigError):
        registry.create_many(NotifyConfig(provider="pagerduty"))


def test_default_registry_supports_tmux_bridge_provider():
    notifiers = DEFAULT_REGISTRY.create_many(NotifyConfig(provider="tmux-bridge"))

    assert [notifier.name for notifier in notifiers] == ["tmux-bridge"]


def test_default_registry_supports_telegram_provider():
    notifiers = DEFAULT_REGISTRY.create_many(NotifyConfig(provider="telegram"))

    assert [notifier.name for notifier in notifiers] == ["telegram"]
