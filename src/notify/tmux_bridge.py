from __future__ import annotations

from backend import deliver_notify_to_session

from .base import Notifier
from .config import NotifyConfig
from .exceptions import NotifyConfigError, NotifyDeliveryError
from .models import DeliveryResult, NotifyEvent, ResolvedRoute


class TmuxBridgeNotifier(Notifier):
    name = "tmux-bridge"

    def __init__(self, config: NotifyConfig) -> None:
        self.config = config

    def send(self, event: NotifyEvent, route: ResolvedRoute) -> DeliveryResult:
        target_session = route.target.strip()
        if not target_session:
            raise NotifyConfigError("tmux-bridge target session is required")
        prompt = self._render_prompt(event)
        try:
            deliver_notify_to_session(target_session, prompt)
        except Exception as exc:
            raise NotifyDeliveryError(f"tmux-bridge delivery failed: {exc}") from exc
        return DeliveryResult(provider=self.name, ok=True, detail="delivered", target=target_session)

    def _render_prompt(self, event: NotifyEvent) -> str:
        status = event.status.strip().lower() or "success"
        event_name = event.event.strip().lower() or "completed"
        session = event.session or "-"
        cwd = event.cwd or "-"
        summary = event.summary or self.config.default_message_prefix
        tail_text = str(event.metadata.get("tail_text") or "").strip()
        lines = [
            "orche notify",
            f"source session: {session}",
            f"event: {event_name}",
            f"cwd: {cwd}",
            "",
            summary,
        ]
        if tail_text:
            lines.extend(["", "Recent output:", tail_text])
        lines.extend(["", f"status: {status}"])
        return "\n".join(lines)
