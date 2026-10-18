from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator


def validate_site_link(value):
    """Allow local paths/fragments or absolute HTTP(S) links only."""
    if not value:
        return
    value = value.strip()
    if not value or any(ord(char) < 32 for char in value) or "\\" in value:
        raise ValidationError("Укажите безопасную ссылку без управляющих символов.")
    if value.startswith("//"):
        raise ValidationError("Ссылка на другой сайт должна начинаться с https:// или http://.")
    if value.startswith(("/", "#")):
        return
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValidationError("Разрешены адрес сайта, локальный путь или ссылка-якорь.")
        URLValidator(schemes=("http", "https"))(value)
    except (ValueError, ValidationError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError("Укажите корректную ссылку http:// или https://.") from exc
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("Разрешены адрес сайта, локальный путь или ссылка-якорь.")
