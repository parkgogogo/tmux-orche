from __future__ import annotations

from html import escape

from .base import Notifier
from .config import NotifyConfig
from .exceptions import NotifyConfigError, NotifyDeliveryError
from .http import HTTPClient, UrllibHTTPClient
from .models import DeliveryResult, NotifyEvent, ResolvedRoute

DEFAULT_USER_AGENT = "tmux-orche/0.1.1 (+https://github.com/parkgogogo/tmux-orche)"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        config: NotifyConfig,
        *,
        http_client: HTTPClient | None = None,
    ) -> None:
        self.config = config
        self.http_client = http_client or UrllibHTTPClient()

    def send(self, event: NotifyEvent, route: ResolvedRoute) -> DeliveryResult:
        bot_token = self.config.telegram.bot_token.strip()
        if not bot_token:
            raise NotifyConfigError("telegram bot token is required")
        chat_id = route.target.strip()
        if not chat_id:
            raise NotifyConfigError("telegram chat_id is required")
        response = self.http_client.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            headers={
                "Content-Type": "application/json",
                "User-Agent": DEFAULT_USER_AGENT,
            },
            json_body={
                "chat_id": chat_id,
                "text": self._render_text(event),
                "parse_mode": "HTML",
            },
            timeout=self.config.telegram.timeout_seconds,
        )
        if response.status_code >= 400:
            raise NotifyDeliveryError(
                f"telegram delivery failed with status={response.status_code}: {response.body.strip()}"
            )
        return DeliveryResult(provider=self.name, ok=True, detail=str(response.status_code), target=chat_id)

    def _render_text(self, event: NotifyEvent) -> str:
        summary = escape(event.summary or self.config.default_message_prefix)
        normalized_status = event.status.strip().lower() or "success"
        if normalized_status != "success":
            summary = f"[{escape(normalized_status)}] {summary}"
        if self.config.include_cwd and event.cwd:
            summary += f"\ncwd: <code>{escape(event.cwd)}</code>"
        if self.config.include_session and event.session:
            summary += f"\nsession: <code>{escape(event.session)}</code>"
        tail_text = str(event.metadata.get("tail_text") or "").strip()
        if tail_text:
            summary += f"\n\nRecent output:\n<pre>{escape(tail_text)}</pre>"
        return summary[: self.config.max_message_chars]
