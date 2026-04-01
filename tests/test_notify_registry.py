from __future__ import annotations

import pytest

from orche.notify.base import Notifier
from orche.notify.config import NotifyConfig
from orche.notify.exceptions import NotifyConfigError
from orche.notify.models import DeliveryResult
from orche.notify.registry import NotifierRegistry


class DummyNotifier(Notifier):
    name = "dummy"

    def send(self, message):
        return DeliveryResult(provider=self.name, ok=True, detail=message.content)


def test_registry_registers_custom_notifier():
    registry = NotifierRegistry()
    registry.register("dummy", lambda config, http_client: DummyNotifier())

    notifiers = registry.create_many(NotifyConfig(providers=("dummy",)))

    assert [notifier.name for notifier in notifiers] == ["dummy"]


def test_registry_rejects_unknown_provider():
    registry = NotifierRegistry()

    with pytest.raises(NotifyConfigError):
        registry.create_many(NotifyConfig(providers=("telegram",)))
