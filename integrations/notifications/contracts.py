"""Stable contracts shared by notification transports and the queue worker."""

from dataclasses import dataclass
import re


_SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    """A transport accepted the message for delivery."""

    accepted: bool = True


class DeliveryError(Exception):
    """An expected delivery failure with a safe, non-sensitive error code."""

    default_code = "delivery_error"

    def __init__(self, code=None):
        safe_code = code or self.default_code
        if not isinstance(safe_code, str) or not _SAFE_CODE.fullmatch(safe_code):
            safe_code = self.default_code
        self.code = safe_code
        super().__init__(safe_code)


class TemporaryDeliveryError(DeliveryError):
    """The delivery may succeed after a bounded retry delay."""

    default_code = "temporary_delivery_failure"


class PermanentDeliveryError(DeliveryError):
    """The delivery requires configuration changes or human review."""

    default_code = "permanent_delivery_failure"


class NotificationConfigurationError(Exception):
    """Backend configuration is invalid; the worker must not claim tasks."""

    def __init__(self, code="backend_configuration_invalid"):
        self.code = code if _SAFE_CODE.fullmatch(str(code)) else "backend_configuration_invalid"
        super().__init__(self.code)

