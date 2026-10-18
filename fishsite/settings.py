import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from dotenv import load_dotenv
from django.templatetags.static import static

BASE_DIR = Path(__file__).resolve().parent.parent
# The test profile must never load application/SMTP credentials.
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
    for host in _environment.get(
        "DJANGO_ALLOWED_HOSTS",
        "localhost,127.0.0.1,[::1],",
    ).split(",")
    if host.strip()
]
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in _environment.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Public browser key. Restrict its domains in the Yandex developer dashboard.
YANDEX_MAPS_API_KEY = _environment.get("YANDEX_MAPS_API_KEY", "").strip()

INSTALLED_APPS = [
    "unfold.apps.BasicAppConfig",
    "fishsite.backoffice.apps.BackofficeAdminConfig",
    "fishsite.backoffice.apps.BackofficeConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "django_otp",
    "django_otp.plugins.otp_totp",
    "django_otp.plugins.otp_static",
    "two_factor",
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
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
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
                "content.context_processors.site_settings_context",
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

# Each exchange stream is opt-in. Credentials are server-only, and the test
# profile reads none of the application's environment variables.
MOYSKLAD_DEFERRED_PRODUCT_IDS = _environment.get("MOYSKLAD_DEFERRED_PRODUCT_IDS", "").strip()
MOYSKLAD = {
    name: _environment.get(f"MOYSKLAD_{name.upper()}", "").strip()
    for name in (
        "account_id", "namespace", "api_token", "admin_token", "price_type_id", "currency_id",
        "unit_id", "organization_id", "agent_id", "initial_state_id", "webhook_secret", "webhook_url",
    )
}
MOYSKLAD["environment"] = _environment.get("MOYSKLAD_ENVIRONMENT", "production").strip()
MOYSKLAD.update({
    name: _boolean_environment_setting(f"MOYSKLAD_{name.upper()}")
    for name in ("catalog_enabled", "prices_enabled", "export_enabled", "statuses_enabled")
})
MOYSKLAD_LEASE_SECONDS = _integer_environment_setting("MOYSKLAD_LEASE_SECONDS", "300")

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = _environment.get("SMTP_HOST", "")
EMAIL_PORT = _integer_environment_setting("SMTP_PORT", "465")
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
    "filters": {
        "moysklad_webhook_secret": {
            "()": "integrations.moysklad.logging.WebhookSecretFilter",
        },
    },
    "formatters": {
        "notification_console": {
            "()": "integrations.notifications.events.NotificationConsoleFormatter",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "notification_console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "formatter": "notification_console",
        },
        "request_console": {
            "class": "logging.StreamHandler",
            "filters": ["moysklad_webhook_secret"],
        },
        "django.server": {
            "class": "logging.StreamHandler",
            "filters": ["moysklad_webhook_secret"],
            "formatter": "django.server",
        },
        "null": {"class": "logging.NullHandler"},
    },
    "loggers": {
        "integrations.notifications": {
            "handlers": ["notification_console"],
            "level": "INFO",
            "propagate": False,
        },
        "django": {"handlers": ["request_console"], "level": "INFO", "propagate": False},
        "django.request": {"handlers": ["request_console"], "level": "WARNING", "propagate": False},
        "django.server": {"handlers": ["django.server"], "level": "INFO", "propagate": False},
        "gunicorn.access": {"handlers": ["request_console"], "level": "INFO", "propagate": False},
        "gunicorn.error": {"handlers": ["request_console"], "level": "INFO", "propagate": False},
        "httpx": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
        "httpcore": {"handlers": ["null"], "level": "CRITICAL", "propagate": False},
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

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = 1
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_USERNAME_FORM_FIELD = "username"
AXES_USERNAME_CALLABLE = "fishsite.backoffice.axes.get_attempt_username"
AXES_CLIENT_IP_CALLABLE = "fishsite.backoffice.axes.get_attempt_ip"
AXES_PASSWORD_FORM_FIELD = "auth-password"
AXES_SENSITIVE_PARAMETERS = [
    "username", "ip_address", "auth-username", "auth-password",
    "token-otp_token", "backup-otp_token",
]
AXES_RESET_ON_SUCCESS = True
AXES_ENABLE_ADMIN = False
if TRUST_X_REAL_IP:
    AXES_IPWARE_META_PRECEDENCE_ORDER = ("HTTP_X_REAL_IP", "REMOTE_ADDR")
else:
    AXES_IPWARE_META_PRECEDENCE_ORDER = ("REMOTE_ADDR",)

TWO_FACTOR_PATCH_ADMIN = False
LOGIN_URL = "two_factor:login"
LOGIN_REDIRECT_URL = "/admin/"
TWO_FACTOR_LOGIN_TIMEOUT = 600

UNFOLD = {
    "THEME": None,
    "SITE_TITLE": "FishSite",
    "SITE_HEADER": "FishSite — управление сайтом",
    "SITE_SUBHEADER": "Каталог, заявки и контент",
    "SITE_ICON": lambda request: static("img/brand-logo.png"),
    "SHOW_HISTORY": True,
    "SHOW_VIEW_ON_SITE": False,
    # Resolve static URLs after app loading. ``STATIC_URL`` is relative in
    # local installs, so make admin assets root-relative for /admin/ routes.
    "STYLES": [lambda request: static("admin/backoffice.css") + "?v=2026-10-18-3"],
    "SCRIPTS": [lambda request: static("admin/theme-default.js") + "?v=2026-10-12-1"],
    "COLORS": {
        "primary": {
            "50": "#f8f4e9", "100": "#efe5c9", "200": "#e3d2a2",
            "300": "#d3b873", "400": "#bd9a4f", "500": "#a98239",
            "600": "#8a692e", "700": "#6d5128", "800": "#574124",
            "900": "#493720", "950": "#291e11",
        },
    },
    "DASHBOARD_CALLBACK": "fishsite.backoffice.dashboard.dashboard_callback",
    "SIDEBAR": {
        "show_search": False,
        "show_all_applications": False,
        "navigation": [
            {
                "title": "Обзор",
                "permission": lambda request: any(
                    request.user.has_perm(permission)
                    for permission in ("catalog.view_product", "orders.view_order", "inquiries.view_inquiry", "content.view_contentpage")
                ),
                "items": [
                    {"title": "Главная панель", "icon": "space_dashboard", "link": "/admin/"},
                ],
            },
            {
                "title": "Каталог",
                "permission": lambda request: request.user.has_perm("catalog.view_product") or request.user.has_perm("content.change_homepagerecommendation"),
                "items": [
                    {"title": "Товары", "icon": "inventory_2", "link": "/admin/catalog/product/", "permission": lambda request: request.user.has_perm("catalog.view_product")},
                    {"title": "Категории", "icon": "category", "link": "/admin/catalog/category/", "permission": lambda request: request.user.has_perm("catalog.view_category")},
                    {"title": "Рекомендуемые товары", "icon": "star", "link": "/admin/content/homepagerecommendation/", "permission": lambda request: request.user.has_perm("content.change_homepagerecommendation")},
                ],
            },
            {
                "title": "Заявки",
                "permission": lambda request: request.user.has_perm("orders.view_order") or request.user.has_perm("inquiries.view_inquiry"),
                "items": [
                    {"title": "Розничные", "icon": "shopping_bag", "link": "/admin/orders/order/", "permission": lambda request: request.user.has_perm("orders.view_order")},
                    {"title": "Оптовые", "icon": "request_quote", "link": "/admin/inquiries/inquiry/", "permission": lambda request: request.user.has_perm("inquiries.view_inquiry")},
                ],
            },
            {
                "title": "Контент сайта",
                "permission": lambda request: (
                    request.user.has_perm("content.view_contentpage")
                    or request.user.has_perm("content.change_sitesettings")
                ),
                "items": [
                    {"title": "Страницы", "icon": "article", "link": "/admin/content/contentpage/", "permission": lambda request: request.user.has_perm("content.view_contentpage")},
                    {"title": "Контакты и общие настройки", "icon": "contact_page", "link": "/admin/content/sitesettings/1/change/", "permission": lambda request: request.user.has_perm("content.change_sitesettings")},
                ],
            },
            {
                "title": "Интеграция",
                "permission": lambda request: request.user.has_perm("integrations.view_moyskladsyncstate"),
                "items": [
                    {"title": "МойСклад", "icon": "sync", "link": "/admin/moysklad/", "permission": lambda request: request.user.has_perm("integrations.view_moyskladsyncstate")},
                    {"title": "Технические настройки МойСклад", "icon": "settings", "link": "/admin/moysklad/technical/", "permission": lambda request: request.user.is_superuser},
                ],
            },
        ],
    },
}
