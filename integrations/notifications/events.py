"""Emit only the compact, safe fields needed to operate delivery."""

import json


def log_notification_event(
    logger,
    level,
    *,
    event,
    outbox_id=None,
    backend=None,
    result=None,
    duration_seconds=None,
    error_code=None,
):
    fields = {
        "event": event,
        "outbox_id": outbox_id,
        "backend": backend,
        "result": result,
        "duration_seconds": duration_seconds,
        "error_code": error_code,
    }
    logger.log(
        level,
        json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        extra={"notification_event": event, **fields},
    )

