from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Tuple


DEFAULT_MENTION_USER_ID = ""


def _as_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def _as_provider(value: Any) -> str:
    if value is None:
        return "discord"
    if isinstance(value, str):
        items = [item.strip() for item in value.replace(";", ",").split(",")]
        for item in items:
            if item:
                return item
        return "discord"
    if isinstance(value, (list, tuple)):
        for item in value:
            text = str(item).strip()
            if text:
                return text
        return "discord"
    return "discord"


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DiscordNotifyConfig:
    bot_token: str = ""
    webhook_url: str = ""
    mention_user_id: str = DEFAULT_MENTION_USER_ID
    timeout_seconds: int = 8


@dataclass(frozen=True)
class NotifyConfig:
    enabled: bool = True
    provider: str = "discord"
    include_cwd: bool = True
    include_session: bool = True
    default_message_prefix: str = "Agent turn complete"
    max_message_chars: int = 1500
    summary_max_chars: int = 1200
    discord: DiscordNotifyConfig = field(default_factory=DiscordNotifyConfig)

    @property
    def providers(self) -> Tuple[str, ...]:
        return (self.provider,) if self.provider else ()


def load_notify_config(
    config: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> NotifyConfig:
    values = dict(config)
    environ = os.environ if env is None else env
    provider = _as_provider(
        values.get("notify_provider")
        or values.get("notify.provider")
        or values.get("notify_target")
        or values.get("notify_providers")
        or values.get("notify_provider_csv")
        or values.get("notify.providers")
        or values.get("notify_targets_csv")
        or values.get("notify_targets")
    )
    discord = DiscordNotifyConfig(
        bot_token=str(environ.get("DISCORD_BOT_TOKEN") or values.get("discord_bot_token") or "").strip(),
        webhook_url=str(environ.get("DISCORD_WEBHOOK_URL") or values.get("discord_webhook_url") or "").strip(),
        mention_user_id=str(
            environ.get("MENTION_USER_ID")
            or values.get("notify_mention_user_id")
            or DEFAULT_MENTION_USER_ID
        ).strip(),
        timeout_seconds=_as_int(values.get("notify_timeout_seconds"), 8),
    )
    return NotifyConfig(
        enabled=_as_bool(values.get("notify_enabled"), True),
        provider=provider,
        include_cwd=_as_bool(values.get("notify_include_cwd"), True),
        include_session=_as_bool(values.get("notify_include_session"), True),
        default_message_prefix=str(values.get("notify_default_message_prefix") or "Agent turn complete").strip()
        or "Agent turn complete",
        max_message_chars=max(1, _as_int(values.get("notify_max_message_chars"), 1500)),
        summary_max_chars=max(1, _as_int(values.get("notify_summary_max_chars"), 1200)),
        discord=discord,
    )
