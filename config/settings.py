"""
Django settings for the Cinagi WhatsApp Platform.

Everything is environment driven. Secrets never live in this file. A
git-ignored ``local_settings.py`` next to this module is imported last and may
override anything - that is the escape hatch for machine specific tweaks.
"""

import base64
import hashlib
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --- Core -------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-insecure-key-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,web")
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
CSRF_TRUSTED_ORIGINS = [
    origin
    for origin in [PUBLIC_BASE_URL] + env_list("CSRF_TRUSTED_ORIGINS")
    if origin.startswith("http")
]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "channels",
    "django_celery_beat",
    "apps.core",
    "apps.accounts",
    "apps.workspaces",
    "apps.channels_wa",
    "apps.contacts",
    "apps.agents",
    "apps.library",
    "apps.inbox",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.workspaces.middleware.ActiveWorkspaceMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.workspaces.context_processors.workspace_context",
                "apps.core.context_processors.site_context",
            ],
        },
    },
]

AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --- Database ---------------------------------------------------------------

if env("DB_ENGINE", "postgres") == "sqlite":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "cinagi_wa"),
            "USER": env("DB_USER", "cinagi"),
            "PASSWORD": env("DB_PASSWORD", "cinagi"),
            "HOST": env("DB_HOST", "postgres"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- I18N / time ------------------------------------------------------------

LANGUAGE_CODE = "en-za"
TIME_ZONE = "Africa/Johannesburg"
USE_I18N = True
USE_TZ = True

# --- Static / media ---------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Tailwind is loaded from the CDN in development so no node build is needed;
# production serves the compiled bundle built by the Dockerfile's node stage.
TAILWIND_CDN = env_bool("TAILWIND_CDN", DEBUG)

# --- Redis: optional in development ------------------------------------------
# With USE_REDIS=False the app runs on Postgres alone: background jobs execute
# inline, the live inbox uses an in-process channel layer, and the cache is
# local memory. Fine for one developer on one machine; turn it on before
# staging, where several processes must share state.

USE_REDIS = env_bool("USE_REDIS", env("DB_ENGINE", "postgres") != "sqlite")

# --- Celery -----------------------------------------------------------------

REDIS_URL = env("REDIS_URL", "redis://redis:6379")
CELERY_BROKER_URL = env("CELERY_BROKER_URL", f"{REDIS_URL}/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", f"{REDIS_URL}/1")
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 300
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_TASK_ALWAYS_EAGER = env_bool("CELERY_TASK_ALWAYS_EAGER", not USE_REDIS)
CELERY_TASK_EAGER_PROPAGATES = False
CELERY_TASK_ROUTES = {
    "apps.channels_wa.tasks.process_inbound_payload": {"queue": "webhooks"},
    "apps.channels_wa.tasks.send_message": {"queue": "outbound"},
    "apps.channels_wa.tasks.sync_templates": {"queue": "default"},
    "apps.ai.*": {"queue": "ai"},
}

# --- Channels (websockets) --------------------------------------------------

CHANNELS_REDIS_URL = env("CHANNELS_REDIS_URL", f"{REDIS_URL}/2")
if env_bool("CHANNELS_IN_MEMORY", not USE_REDIS):
    CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
else:
    CHANNEL_LAYERS = {
        "default": {
            "BACKEND": "channels_redis.core.RedisChannelLayer",
            "CONFIG": {"hosts": [CHANNELS_REDIS_URL]},
        }
    }

# --- Cache ------------------------------------------------------------------

if not USE_REDIS:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": env("CACHE_REDIS_URL", f"{REDIS_URL}/3"),
        }
    }

# --- WhatsApp / outbound safety --------------------------------------------

WHATSAPP_GRAPH_VERSION = env("WHATSAPP_GRAPH_VERSION", "v21.0")
WHATSAPP_TIMEOUT = int(env("WHATSAPP_TIMEOUT", "30"))
# suppress | allowlist | live  - see apps/channels_wa/comms_guard.py
OUTBOUND_COMMS_MODE = env("OUTBOUND_COMMS_MODE", "suppress" if DEBUG else "live")
OUTBOUND_ALLOWLIST = env_list("OUTBOUND_ALLOWLIST")

# Encryption key for credentials held in the database.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY") or base64.urlsafe_b64encode(
    hashlib.sha256(SECRET_KEY.encode()).digest()
).decode()

# --- Security ---------------------------------------------------------------

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = False  # HTMX reads the token from a meta tag
if not DEBUG:
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", False)
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- Logging ----------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact": {"()": "apps.core.logging_filters.RedactFilter"}},
    "formatters": {
        "standard": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["redact"],
        }
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING"},
        "apps": {"level": env("APP_LOG_LEVEL", "INFO"), "propagate": True},
    },
}

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

try:  # pragma: no cover - machine specific overrides
    from .local_settings import *  # noqa: F401,F403
except ImportError:
    pass
