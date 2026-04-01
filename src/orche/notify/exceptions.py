from __future__ import annotations


class NotifyError(RuntimeError):
    pass


class NotifyConfigError(NotifyError):
    pass


class NotifyDeliveryError(NotifyError):
    pass
