import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
# The test profile must never load application/Gmail credentials.
_TEST_PROFILE = os.environ.get("DJANGO_SETTINGS_MODULE") == "fishsite.settings_test"
if not _TEST_PROFILE and os.environ.get("FISHSITE_SKIP_DOTENV") != "1":
    load_dotenv(BASE_DIR / ".env", override=False)
_environment = {} if _TEST_PROFILE else os.environ


def _boolean_environment_setting(name, default=False):
    value = _environment.get(name)
    if value is None:
        return default
    normalized = value.lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ImproperlyConfigured(f"{name} must be set to true or false.")


def _integer_environment_setting(name, default):
    try:
        return int(_environment.get(name, default))
    except (TypeError, ValueError, OverflowError):
        raise ImproperlyConfigured(f"{name} must be an integer.") from None


def _float_environment_setting(name, default):
    try:
        return float(_environment.get(name, default))
    except (TypeError, ValueError, OverflowError):
        raise ImproperlyConfigured(f"{name} must be a number.") from None


SECRET_KEY = _environment.get("DJANGO_SECRET_KEY", "development-only-change-before-deploy")
DEBUG = _environment.get("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [
    host.strip()
    for host in _environment.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,[::1]").split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in _environment.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "catalog.apps.CatalogConfig",
    "content.apps.ContentConfig",
    "orders.apps.OrdersConfig",
    "inquiries.apps.InquiriesConfig",
    "integrations.apps.IntegrationsConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "fishsite.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "catalog.context_processors.navigation_categories",
                "orders.context_processors.cart_count",
            ],
        },
    }
]
WSGI_APPLICATION = "fishsite.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _environment.get("DJANGO_DB_NAME", "fishsite"),
        "USER": _environment.get("DJANGO_DB_USER", "fishsite"),
        "PASSWORD": _environment.get("DJANGO_DB_PASSWORD", ""),
        "HOST": _environment.get("DJANGO_DB_HOST", "127.0.0.1"),
        "PORT": _environment.get("DJANGO_DB_PORT", "5432"),
        "CONN_MAX_AGE": int(_environment.get("DJANGO_DB_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

PUBLIC_FORM_RATE_LIMIT = int(_environment.get("PUBLIC_FORM_RATE_LIMIT", "20"))
PUBLIC_FORM_RATE_WINDOW_SECONDS = int(_environment.get("PUBLIC_FORM_RATE_WINDOW_SECONDS", "3600"))
TRUST_X_REAL_IP = _boolean_environment_setting("DJANGO_TRUST_X_REAL_IP")

# Production HTTPS is terminated by the controlled Nginx reverse proxy. Keep
# the proxy header opt-in so local HTTP deployments do not appear secure.
if _boolean_environment_setting("DJANGO_TRUST_X_FORWARDED_PROTO"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = _boolean_environment_setting("DJANGO_SECURE_SSL_REDIRECT")
SESSION_COOKIE_SECURE = _boolean_environment_setting("DJANGO_SESSION_COOKIE_SECURE")
CSRF_COOKIE_SECURE = _boolean_environment_setting("DJANGO_CSRF_COOKIE_SECURE")
SECURE_HSTS_SECONDS = _integer_environment_setting("DJANGO_SECURE_HSTS_SECONDS", "0")
SECURE_HSTS_INCLUDE_SUBDOMAINS = _boolean_environment_setting(
    "DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS"
)
SECURE_HSTS_PRELOAD = _boolean_environment_setting("DJANGO_SECURE_HSTS_PRELOAD")

# Request notifications are sent by a separately run worker. SMTP-specific
# settings are only validated when the SMTP adapter is selected.
NOTIFICATION_BACKEND = _environment.get(
    "NOTIFICATION_BACKEND",
    "integrations.notifications.smtp.SMTPNotificationBackend",
)
NOTIFICATION_TO_EMAIL = _environment.get("NOTIFICATION_TO_EMAIL", "")
NOTIFICATION_MAX_ATTEMPTS = _integer_environment_setting("NOTIFICATION_MAX_ATTEMPTS", "5")
NOTIFICATION_RETRY_BASE_SECONDS = _integer_environment_setting("NOTIFICATION_RETRY_BASE_SECONDS", "30")
NOTIFICATION_RETRY_MAX_SECONDS = _integer_environment_setting("NOTIFICATION_RETRY_MAX_SECONDS", "3600")
NOTIFICATION_SCAN_INTERVAL_SECONDS = _integer_environment_setting("NOTIFICATION_SCAN_INTERVAL_SECONDS", "60")
NOTIFICATION_LEASE_SECONDS = _integer_environment_setting("NOTIFICATION_LEASE_SECONDS", "300")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = _environment.get("SMTP_HOST", "")
EMAIL_PORT = _integer_environment_setting("SMTP_PORT", "587")
EMAIL_HOST_USER = _environment.get("SMTP_USERNAME", "")
EMAIL_HOST_PASSWORD = _environment.get("SMTP_PASSWORD", "")
EMAIL_USE_TLS = _boolean_environment_setting("SMTP_USE_TLS")
EMAIL_USE_SSL = _boolean_environment_setting("SMTP_USE_SSL")
SMTP_ALLOW_INSECURE_LOCAL_AUTH = _boolean_environment_setting("SMTP_ALLOW_INSECURE_LOCAL_AUTH")
EMAIL_TIMEOUT = _float_environment_setting("SMTP_TIMEOUT_SECONDS", "10")
DEFAULT_FROM_EMAIL = _environment.get("NOTIFICATION_FROM_EMAIL", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"notification_json": {"format": "%(message)s"}},
    "handlers": {
        "notification_console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "notification_json",
        },
    },
    "loggers": {
        "integrations.notifications": {
            "handlers": ["notification_console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(_environment.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# Refuse alternate entry points (including django-admin test) on normal settings.
TEST_RUNNER = "fishsite.test_runner.IsolatedTestRunner"
