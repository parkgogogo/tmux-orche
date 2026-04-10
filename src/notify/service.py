from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Any, Callable, Mapping, Sequence

from .config import load_notify_config
from .exceptions import NotifyConfigError
from .http import HTTPClient
from .models import DeliveryResult, NotifyEvent, ResolvedRoute
from .payload import build_message_from_payload
from .registry import DEFAULT_REGISTRY, NotifierRegistry

MAX_NOTIFY_WORKERS = 10


class NotificationService:
    def __init__(
        self,
        *,
        registry: NotifierRegistry | None = None,
        http_client: HTTPClient | None = None,
    ) -> None:
        self.registry = registry or DEFAULT_REGISTRY
        self.http_client = http_client

    def send(
        self,
        event: NotifyEvent,
        routes: Sequence[ResolvedRoute],
        config,
    ) -> Sequence[DeliveryResult]:
        if not routes:
            return ()
        requested_providers = tuple(dict.fromkeys(route.provider for route in routes if route.provider))
        notifiers = {}
        for provider in requested_providers:
            try:
                created = self.registry.create_many_for(
                    (provider,),
                    config,
                    http_client=self.http_client,
                )
            except NotifyConfigError:
                continue
            if created:
                notifiers[provider] = created[0]
        results: list[DeliveryResult] = []
        with ThreadPoolExecutor(max_workers=max(1, min(MAX_NOTIFY_WORKERS, len(routes)))) as executor:
            future_map = {}
            for route in routes:
                notifier = notifiers.get(route.provider)
                if notifier is None:
                    supported = ", ".join(sorted(notifiers)) or ", ".join(self.registry.names())
                    results.append(
                        DeliveryResult(
                            provider=route.provider,
                            ok=False,
                            detail=f"Unsupported notifier: {route.provider}. Supported notifiers: {supported}",
                            target=route.target,
                        )
                    )
                    continue
                future = executor.submit(notifier.send, event, route)
                future_map[future] = route
            for future in as_completed(future_map):
                route = future_map[future]
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        DeliveryResult(
                            provider=route.provider,
                            ok=False,
                            detail=str(exc),
                            target=route.target,
                        )
                    )
        return tuple(sorted(results, key=lambda result: (result.provider, result.target)))


def resolve_routes(
    *,
    event: NotifyEvent,
    runtime_config: Mapping[str, Any],
    notify_config,
    explicit_channel_id: str = "",
) -> Sequence[ResolvedRoute]:
    normalized_channel_id = re.sub(r"\s+", "", str(explicit_channel_id or ""))
    if normalized_channel_id:
        explicit_provider = "discord"
        raw_binding = runtime_config.get("notify_binding")
        binding = raw_binding if isinstance(raw_binding, Mapping) else {}
        if str(binding.get("provider") or "").strip() == "telegram":
            explicit_provider = "telegram"
        return (ResolvedRoute(provider=explicit_provider, target=normalized_channel_id, session=event.session),)

    raw_binding = runtime_config.get("notify_binding")
    binding = raw_binding if isinstance(raw_binding, Mapping) else {}
    binding_provider = str(binding.get("provider") or "").strip()
    binding_target = str(binding.get("target") or "").strip()
    if binding_provider:
        if binding_provider == "discord":
            binding_target = re.sub(r"\s+", "", binding_target)
        if binding_provider == "telegram" and binding_target:
            metadata = {
                key: value
                for key, value in dict(binding).items()
                if key not in {"provider", "target", "session"} and str(value).strip()
            }
            return (
                ResolvedRoute(
                    provider="telegram",
                    target=binding_target,
                    session=event.session,
                    metadata=metadata,
                ),
            )
        if binding_target:
            metadata = {
                key: value
                for key, value in dict(binding).items()
                if key not in {"provider", "target", "session"} and str(value).strip()
            }
            return (
                ResolvedRoute(
                    provider=binding_provider,
                    target=binding_target,
                    session=event.session,
                    metadata=metadata,
                ),
            )
    return ()


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
    event = build_message_from_payload(
        payload_text,
        notify_config=notify_config,
        runtime_config=runtime_config,
        summary_loader=summary_loader,
        explicit_session=explicit_session,
        explicit_channel_id=explicit_channel_id,
        status=status,
    )
    if event is None:
        return ()
    routes = resolve_routes(
        event=event,
        runtime_config=runtime_config,
        notify_config=notify_config,
        explicit_channel_id=explicit_channel_id,
    )
    return dispatch_event(
        event,
        runtime_config=runtime_config,
        notify_config=notify_config,
        routes=routes,
        service=service,
    )


def dispatch_event(
    event: NotifyEvent,
    *,
    runtime_config: Mapping[str, Any],
    notify_config=None,
    explicit_channel_id: str = "",
    routes: Sequence[ResolvedRoute] | None = None,
    env: Mapping[str, str] | None = None,
    service: NotificationService | None = None,
) -> Sequence[DeliveryResult]:
    effective_config = notify_config or load_notify_config(runtime_config, env=env)
    if not effective_config.enabled:
        return ()
    resolved_routes = tuple(
        routes
        if routes is not None
        else resolve_routes(
            event=event,
            runtime_config=runtime_config,
            notify_config=effective_config,
            explicit_channel_id=explicit_channel_id,
        )
    )
    if not resolved_routes:
        return ()
    notifier_service = service or NotificationService()
    return notifier_service.send(event, resolved_routes, effective_config)
