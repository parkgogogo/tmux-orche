from __future__ import annotations

from .base import Notifier
from .config import DiscordNotifyConfig
from .exceptions import NotifyConfigError, NotifyDeliveryError
from .http import HTTPClient, UrllibHTTPClient
from .models import DeliveryResult, Message

DEFAULT_USER_AGENT = "tmux-orche/0.1.1 (+https://github.com/parkgogogo/tmux-orche)"


class DiscordNotifier(Notifier):
    name = "discord"

    def __init__(
        self,
        config: DiscordNotifyConfig,
        *,
        http_client: HTTPClient | None = None,
    ) -> None:
        self.config = config
        self.http_client = http_client or UrllibHTTPClient()

    def send(self, message: Message) -> DeliveryResult:
        request_body = {"content": message.content, "allowed_mentions": self._allowed_mentions()}
        base_headers = {
            "Content-Type": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if self.config.webhook_url:
            response = self.http_client.post(
                self.config.webhook_url,
                headers=base_headers,
                json_body=request_body,
                timeout=self.config.timeout_seconds,
            )
        else:
            if not self.config.bot_token:
                raise NotifyConfigError("discord bot token is required when webhook_url is not configured")
            if not message.channel_id:
                raise NotifyConfigError("discord channel_id is required for bot-token delivery")
            response = self.http_client.post(
                f"https://discord.com/api/v10/channels/{message.channel_id}/messages",
                headers={
                    **base_headers,
                    "Authorization": f"Bot {self.config.bot_token}",
                },
                json_body=request_body,
                timeout=self.config.timeout_seconds,
            )
        if response.status_code >= 400:
            raise NotifyDeliveryError(
                f"discord delivery failed with status={response.status_code}: {response.body.strip()}"
            )
        return DeliveryResult(provider=self.name, ok=True, detail=str(response.status_code))

    def _allowed_mentions(self) -> dict:
        if self.config.mention_user_id:
            return {"parse": [], "users": [self.config.mention_user_id]}
        return {"parse": []}
