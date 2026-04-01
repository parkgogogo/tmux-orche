from __future__ import annotations

from abc import ABC, abstractmethod

from .models import DeliveryResult, Message


class Notifier(ABC):
    name = "unknown"

    @abstractmethod
    def send(self, message: Message) -> DeliveryResult:
        raise NotImplementedError
