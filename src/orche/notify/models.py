from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    content: str
    channel_id: str
    session: str
    status: str


@dataclass(frozen=True)
class DeliveryResult:
    provider: str
    ok: bool
    detail: str = ""
