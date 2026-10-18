"""Safe exceptions shared by exchange services and transports."""

import re


_SAFE_CODE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


def safe_error_code(value, default="moysklad_error"):
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else default


class MoySkladError(Exception):
    """An expected failure containing diagnostic metadata, never API content."""

    def __init__(self, code="moysklad_error", *, retryable=False, retry_after=None, status_code=None):
        self.code = safe_error_code(code)
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.status_code = status_code
        super().__init__(self.code)


class LeaseLost(MoySkladError):
    def __init__(self):
        super().__init__("lease_lost", retryable=True)
