from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Mapping, Sequence

from .config import load_notify_config
from .http import HTTPClient
from .models import DeliveryResult, Message
from .payload import build_message_from_payload
from .registry import DEFAULT_REGISTRY, NotifierRegistry


class NotificationService:
    def __init__(
        self,
        *,
        registry: NotifierRegistry | None = None,
        http_client: HTTPClient | None = None,
    ) -> None:
        self.registry = registry or DEFAULT_REGISTRY
        self.http_client = http_client

    def send(self, message: Message, config) -> Sequence[DeliveryResult]:
        notifiers = self.registry.create_many(config, http_client=self.http_client)
        if not notifiers:
            return ()
        results: list[DeliveryResult] = []
        with ThreadPoolExecutor(max_workers=max(1, len(notifiers))) as executor:
            future_map = {executor.submit(notifier.send, message): notifier.name for notifier in notifiers}
            for future in as_completed(future_map):
                provider = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(DeliveryResult(provider=provider, ok=False, detail=str(exc)))
        return tuple(sorted(results, key=lambda result: result.provider))


def dispatch_payload(
    payload_text: str,
    *,
    runtime_config: Mapping[str, Any],
    summary_loader: Callable[[str], str],
    explicit_channel_id: str = "",
    explicit_session: str = "",
    status: str = "success",
    env: Mapping[str, str] | None = None,
    service: NotificationService | None = None,
) -> Sequence[DeliveryResult]:
    notify_config = load_notify_config(runtime_config, env=env)
    if not notify_config.enabled:
        return ()
    message = build_message_from_payload(
        payload_text,
        notify_config=notify_config,
        runtime_config=runtime_config,
        summary_loader=summary_loader,
        explicit_channel_id=explicit_channel_id,
        explicit_session=explicit_session,
        status=status,
    )
    if message is None:
        return ()
    notifier_service = service or NotificationService()
    return notifier_service.send(message, notify_config)
