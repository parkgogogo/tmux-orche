from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

from .base import Notifier
from .config import NotifyConfig
from .discord import DiscordNotifier
from .exceptions import NotifyConfigError
from .http import HTTPClient

NotifierFactory = Callable[[NotifyConfig, Optional[HTTPClient]], Notifier]


class NotifierRegistry:
    def __init__(self) -> None:
        self._factories: Dict[str, NotifierFactory] = {}

    def register(self, name: str, factory: NotifierFactory) -> None:
        self._factories[name] = factory

    def names(self) -> Iterable[str]:
        return tuple(sorted(self._factories))

    def create_many(
        self,
        config: NotifyConfig,
        *,
        http_client: HTTPClient | None = None,
    ) -> List[Notifier]:
        notifiers: List[Notifier] = []
        for provider in config.providers:
            factory = self._factories.get(provider)
            if factory is None:
                supported = ", ".join(self.names())
                raise NotifyConfigError(f"Unsupported notifier: {provider}. Supported notifiers: {supported}")
            notifiers.append(factory(config, http_client))
        return notifiers


def _discord_factory(config: NotifyConfig, http_client: HTTPClient | None) -> Notifier:
    return DiscordNotifier(config.discord, http_client=http_client)


DEFAULT_REGISTRY = NotifierRegistry()
DEFAULT_REGISTRY.register("discord", _discord_factory)
