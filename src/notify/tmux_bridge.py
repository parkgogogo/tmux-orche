from __future__ import annotations

from backend import deliver_notify_to_session, load_meta

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
        target_agent = str(load_meta(target_session).get("agent") or "").strip().lower()
        prompt = self._render_prompt(event, target_agent=target_agent)
        try:
            deliver_notify_to_session(target_session, prompt)
        except Exception as exc:
            raise NotifyDeliveryError(f"tmux-bridge delivery failed: {exc}") from exc
        return DeliveryResult(provider=self.name, ok=True, detail="delivered", target=target_session)

    def _render_prompt(self, event: NotifyEvent, *, target_agent: str = "") -> str:
        status = event.status.strip().lower() or "success"
        event_name = event.event.strip().lower() or "completed"
        session = event.session or "-"
        cwd = event.cwd or "-"
        summary = event.summary or self.config.default_message_prefix
        tail_text = str(event.metadata.get("tail_text") or "").strip()
        if target_agent == "claude":
            parts = [
                "orche notify",
                f"source={session}",
                f"event={event_name}",
                f"status={status}",
                f"summary={self._compact(summary)}",
            ]
            return " | ".join(part for part in parts if part)
        lines = [
            "orche notify",
            f"source session: {session}",
            f"event: {event_name}",
            f"status: {status}",
            f"cwd: {cwd}",
            "",
            summary,
        ]
        if tail_text:
            lines.extend(["", "Recent output:", tail_text])
        return "\n".join(lines)

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(str(text).split())
