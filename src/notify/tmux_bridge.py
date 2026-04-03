from __future__ import annotations

from backend import bridge_keys, bridge_resolve, bridge_type

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
        if not bridge_resolve(target_session):
            raise NotifyDeliveryError(f"tmux-bridge target session not found: {target_session}")
        prompt = self._render_prompt(event)
        try:
            bridge_type(target_session, prompt)
            bridge_keys(target_session, ["Enter"])
        except Exception as exc:
            raise NotifyDeliveryError(f"tmux-bridge delivery failed: {exc}") from exc
        return DeliveryResult(provider=self.name, ok=True, detail="delivered", target=target_session)

    def _render_prompt(self, event: NotifyEvent) -> str:
        status = event.status.strip().lower() or "success"
        session = event.session or "-"
        cwd = event.cwd or "-"
        summary = event.summary or self.config.default_message_prefix
        return "\n".join(
            [
                "orche notify",
                f"source session: {session}",
                f"status: {status}",
                f"cwd: {cwd}",
                "",
                summary,
            ]
        )
