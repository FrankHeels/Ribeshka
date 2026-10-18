from django.core.exceptions import ValidationError
from PIL import Image, UnidentifiedImageError


ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 5000
MAX_IMAGE_PIXELS = 20_000_000


def validate_site_image(upload):
    """Accept only small, decodable JPEG, PNG and WebP image uploads."""
    if not upload:
        return
    # Preserve previously stored media when editing text or ordering. Validate
    # new uploads, including uncommitted model FieldFiles, before storage.
    if getattr(upload, "_committed", False):
        return
    if upload.size > MAX_IMAGE_BYTES:
        raise ValidationError("Файл изображения должен быть не больше 8 МБ.")

    try:
        if hasattr(upload, "seek"):
            upload.seek(0)
        with Image.open(upload) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError) as exc:
        raise ValidationError("Загрузите корректное изображение JPEG, PNG или WebP.") from exc
    finally:
        if hasattr(upload, "seek"):
            upload.seek(0)

    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise ValidationError("Разрешены изображения JPEG, PNG и WebP.")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
        raise ValidationError("Изображение должно быть не больше 5000 × 5000 пикселей.")
