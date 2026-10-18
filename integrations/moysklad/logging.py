"""Redact the receiver secret in Django request, access and exception logs."""

import logging
import re
from collections.abc import MutableMapping


_SECRET_PATH = re.compile(r"(/integrations/moysklad/webhooks/prices/)[^/\s?'\"]+")


def redact(value):
    if isinstance(value, str):
        return _SECRET_PATH.sub(r"\1[redacted]", value)
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


class WebhookSecretFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.msg)
        record.args = redact(record.args)
        request = getattr(record, "request", None)
        if request is not None:
            for name in ("path", "path_info"):
                value = getattr(request, name, None)
                if isinstance(value, str):
                    setattr(request, name, redact(value))
            metadata = getattr(request, "META", None)
            if isinstance(metadata, MutableMapping):
                for key in ("RAW_URI", "REQUEST_URI", "PATH_INFO"):
                    if key in metadata:
                        metadata[key] = redact(metadata[key])
        if record.exc_info:
            # Exception messages can carry the full incoming URL. Render the
            # traceback once and clear the original exception reference.
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True
