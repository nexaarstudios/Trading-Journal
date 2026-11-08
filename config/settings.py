"""Django settings. Everything that differs between your laptop and production comes from environment variables."""
import os
import sys
import warnings
from datetime import timedelta
from pathlib import Path

import dj_database_url
from dotenv import load_dotenv

from config.observability import install_log_fields

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")  # real environment variables (on Render, for example) win over this file
install_log_fields()            # lets every log line carry the request id and user id

TESTING = "test" in sys.argv


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


# ---------- Core ----------
SECRET_KEY = os.environ["SECRET_KEY"]
DEBUG = env_bool("DEBUG", False)  # off unless switched on: your local .env sets DEBUG=True

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS")
if DEBUG:
    ALLOWED_HOSTS += ["localhost", "127.0.0.1", "[::1]"]
if os.environ.get("RENDER_EXTERNAL_HOSTNAME"):  # set by Render itself
    ALLOWED_HOSTS.append(os.environ["RENDER_EXTERNAL_HOSTNAME"])

CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
if os.environ.get("RENDER_EXTERNAL_URL"):
    CSRF_TRUSTED_ORIGINS.append(os.environ["RENDER_EXTERNAL_URL"])

ADMIN_URL = os.environ.get("ADMIN_URL", "admin/").strip("/") + "/"  # move the admin somewhere less obvious in production

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "accounts",
    "trades",
]

MIDDLEWARE = [
    "config.observability.RequestLogMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

# ---------- Database and cache ----------
DATABASES = {
    "default": dj_database_url.config(
        default=os.environ["DATABASE_URL"], conn_max_age=600, conn_health_checks=True
    )
}

if os.environ.get("REDIS_URL") and not TESTING:
    CACHES = {"default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": os.environ["REDIS_URL"],
    }}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    if not DEBUG and not TESTING:
        warnings.warn(
            "REDIS_URL is not set: login lockouts and rate limits are kept per process "
            "and will not add up across workers."
        )

# ---------- Users and passwords ----------
AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"  # every time is stored in UTC and shown in the user's own timezone
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------- Files ----------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

SCREENSHOT_URL_TTL = 15 * 60                  # seconds a signed link stays valid
SCREENSHOT_MAX_BYTES = 2 * 1024 * 1024        # per image
SCREENSHOT_MAX_PER_TRADE = 5
SCREENSHOT_USER_QUOTA_BYTES = 100 * 1024 * 1024
SCREENSHOT_MAX_PIXELS = 50_000_000            # refuse absurdly large images
SCREENSHOT_THUMBNAIL_SIZE = 480               # longest side of a thumbnail, in pixels

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
    # Private on purpose: nothing here is served directly, only through signed links
    "screenshots": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": BASE_DIR / "private_media"},
    },
}
SCREENSHOT_LINKS = "app"  # "app": the API serves the file from a signed link; "storage": the bucket does

if os.environ.get("STORAGE_BACKEND") == "s3":  # Supabase Storage, AWS S3, Cloudflare R2, ...
    STORAGES["screenshots"] = {
        "BACKEND": "storages.backends.s3.S3Storage",
        "OPTIONS": {
            "bucket_name": os.environ["S3_BUCKET"],
            "endpoint_url": os.environ.get("S3_ENDPOINT_URL") or None,
            "region_name": os.environ.get("S3_REGION") or None,
            "access_key": os.environ["S3_ACCESS_KEY_ID"],
            "secret_key": os.environ["S3_SECRET_ACCESS_KEY"],
            "addressing_style": os.environ.get("S3_ADDRESSING_STYLE", "path"),  # Supabase needs path-style
            "signature_version": "s3v4",
            "default_acl": None,
            "querystring_auth": True,
            "querystring_expire": SCREENSHOT_URL_TTL,
            "file_overwrite": False,
        },
    }
    SCREENSHOT_LINKS = os.environ.get("SCREENSHOT_LINKS", "storage")

# ---------- API ----------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "accounts.authentication.TrackedJWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_THROTTLE_RATES": {"auth": "10/min", "fx": "60/min", "email": "5/hour"},
}
if not DEBUG:  # no browsable HTML API in production: JSON only
    REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"] = ["rest_framework.renderers.JSONRenderer"]

SIMPLE_JWT = {
    # Long tokens only while DEBUG is on, so hand-testing doesn't mean re-logging in constantly
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=8) if DEBUG else timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "CHECK_REVOKE_TOKEN": True,  # changing or resetting a password ends every older session
}

# The frontend is on another address, so the browser must be told it may call this API
CORS_ALLOWED_ORIGINS = env_list(
    "CORS_ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:3000" if DEBUG else ""
)
CORS_EXPOSE_HEADERS = ["X-Request-ID", "Retry-After", "Content-Disposition"]  # headers the frontend may read

# ---------- Accounts ----------
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")  # where emailed links point
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Trading Journal <no-reply@tradingjournal.local>")
PASSWORD_RESET_TIMEOUT = 60 * 60              # reset links last one hour
EMAIL_VERIFICATION_MAX_AGE = 3 * 24 * 3600    # verification links last three days
EMAIL_CHANGE_MAX_AGE = 24 * 3600              # email-change links last a day
LOGIN_MAX_FAILURES = 5                        # wrong passwords before an account is locked
LOGIN_LOCKOUT_SECONDS = 15 * 60
TWO_FACTOR_ISSUER = "Trading Journal"         # the name shown in the authenticator app
TWO_FACTOR_KEY = os.environ.get("TWO_FACTOR_KEY", "")  # encrypts authenticator secrets: set it in production and never change it
TWO_FACTOR_CHALLENGE_SECONDS = 5 * 60         # time allowed between the password and the code
GOOGLE_CLIENT_IDS = env_list("GOOGLE_CLIENT_IDS")
APPLE_CLIENT_IDS = env_list("APPLE_CLIENT_IDS")
TRUSTED_DEVICE_DAYS = 30                      # how long "remember this device" lasts
TRUSTED_DEVICE_MAX = 10                       # devices kept per user; the oldest is dropped

# ---------- Exchange rates ----------
FX_AUTO_FETCH = env_bool("FX_AUTO_FETCH", True)  # download missing rates on demand
FX_FETCH_TIMEOUT = 5                             # seconds

# ---------- Email ----------
# Emails print in the server window unless a provider is configured: Resend first, any other SMTP service second.
MAILERS = {"default": {"BACKEND": "django.core.mail.backends.console.EmailBackend"}}
if os.environ.get("RESEND_API_KEY"):
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": "smtp.resend.com",
                "port": 587,
                "use_tls": True,
                "username": "resend",  # always the word "resend"; the API key is the password
                "password": os.environ["RESEND_API_KEY"],
            },
        },
    }
elif os.environ.get("MAIL_HOST"):
    MAILERS = {
        "default": {
            "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
            "OPTIONS": {
                "host": os.environ["MAIL_HOST"],
                "port": int(os.environ.get("MAIL_PORT", "587")),
                "use_tls": os.environ.get("MAIL_USE_TLS", "True") == "True",
                "username": os.environ.get("MAIL_USER", ""),
                "password": os.environ.get("MAIL_PASSWORD", ""),
            },
        },
    }

# ---------- Security in production ----------
if not DEBUG and not TESTING:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")  # Render's proxy handles HTTPS and says so here
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^api/health/$"]                    # the host's health check must not be redirected
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "3600"))  # raise to a year once all is well
    SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)

# ---------- Logging ----------
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {
            "format": "%(asctime)s %(levelname)s %(name)s request=%(request_id)s user=%(user_id)s %(message)s",
        },
    },
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
    # Django's own handlers would print everything a second time, so hand its records to the root logger only
    "loggers": {
        "django": {"handlers": [], "level": "INFO", "propagate": True},
        "django.request": {"handlers": [], "level": "ERROR", "propagate": True},  # 5xx only; our request line covers the rest
    },
}

# ---------- Test runs ----------
if TESTING:  # nothing in a test run may touch a real bucket, a real mail service, the network or Redis
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]  # fast and weak: only ever for tests
    FX_AUTO_FETCH = False
    SCREENSHOT_LINKS = "app"
    STORAGES["screenshots"] = {"BACKEND": "django.core.files.storage.InMemoryStorage"}
    LOGGING["handlers"]["console"]["level"] = "CRITICAL"  # quiet test output; assertLogs still sees everything