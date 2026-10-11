"""Emit only the compact, safe fields needed to operate delivery."""

import json
import logging
import time


class NotificationConsoleFormatter(logging.Formatter):
    """Render safe notification events as timestamped, readable console lines."""

    converter = time.gmtime

    RESULT_LABELS = {
        "sent": "Уведомление принято транспортом",
        "retry_scheduled": "Временная ошибка; повтор запланирован",
        "manual_review": "Требуется ручная проверка",
        "stale_owner": "Попытка завершена; lease больше не принадлежит worker",
        "uncertain_database_failure": "Сбой БД; результат отправки не определён",
        "database_failure_before_send": "Сбой БД до начала отправки",
        "reconnecting": "Переподключение к БД",
        "started": "Worker запущен",
        "stopped": "Worker остановлен",
    }

    EVENT_LABELS = {
        "worker_started": "старт",
        "worker_stopped": "остановка",
        "worker_database_reconnect": "соединение с БД",
        "delivery_attempt": "отправка уведомления",
        "database_failure_during_delivery": "ошибка БД при отправке",
        "attempt_limit_reached": "лимит попыток исчерпан",
    }

    FIELD_ORDER = (
        ("outbox_id", "outbox_id"),
        ("backend", "backend"),
        ("result", "result"),
        ("duration_seconds", "duration_seconds"),
        ("error_code", "error_code"),
    )

    def __init__(self, *, datefmt="%Y-%m-%d %H:%M:%S"):
        super().__init__(datefmt=datefmt)

    @staticmethod
    def _single_line(value):
        return str(value).replace("\r", " ").replace("\n", " ")

    def format(self, record):
        try:
            fields = json.loads(record.getMessage())
        except (TypeError, ValueError):
            fields = {}

        event = fields.get("event") or getattr(record, "notification_event", "notification_event")
        result = fields.get("result")
        description = self.RESULT_LABELS.get(result, "")
        if not description:
            description = self.EVENT_LABELS.get(event, "Событие уведомлений")

        parts = [
            f"{self.formatTime(record, self.datefmt)} UTC",
            record.levelname,
            f"[{self._single_line(event)}]",
            description,
        ]
        for field, label in self.FIELD_ORDER:
            value = fields.get(field)
            if value is None or value == "":
                continue
            if field == "duration_seconds":
                value = f"{value}s"
            parts.append(f"{label}={self._single_line(value)}")
        return " | ".join(parts)


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

