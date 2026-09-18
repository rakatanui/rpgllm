"""MRAZ Master settings."""
from pathlib import Path
import os
import secrets

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, str(default)).lower()
    return v in ("1", "true", "yes", "on")


# SECURITY
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_urlsafe(50)
DEBUG = _env_bool("DEBUG", False)

ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("ALLOWED_HOSTS", "mraz.local,127.0.0.100").split(",")
    if h.strip()
]

CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "http://mraz.local").split(",")
    if o.strip()
]

# SECURITY: never allow "*" in ALLOWED_HOSTS in any normal deployment.
if "*" in ALLOWED_HOSTS:
    raise RuntimeError("ALLOWED_HOSTS must not contain '*'")

# Apps
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rpg",
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
]

ROOT_URLCONF = "mraz.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "rpg" / "templates"],
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

WSGI_APPLICATION = "mraz.wsgi.application"
ASGI_APPLICATION = "mraz.asgi.application"

# Database
_db_host = os.environ.get("POSTGRES_HOST", "db")
_db_port = os.environ.get("POSTGRES_PORT", "5432")
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "mraz"),
        "USER": os.environ.get("POSTGRES_USER", "mraz"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "changeme_pg_password"),
        "HOST": _db_host,
        "PORT": _db_port,
        "CONN_MAX_AGE": 60,
    }
}

# If using SQLite for tests, allow override via env.
if os.environ.get("DB_ENGINE") == "sqlite":
    DATABASES["default"] = {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }

# Password hashing
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "rpg" / "static"]

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---- MRAZ Master app config ----
LLM_BACKEND = os.environ.get("LLM_BACKEND", "mock")  # "mock" | "litellm"
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_API_KEY = os.environ.get("LITELLM_MASTER_KEY", "sk-litellm-default")
DEFAULT_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60"))

# Context manager. Character budgets are provider-agnostic and intentionally
# conservative; set <= 0 to disable a specific limit.
CONTEXT_HISTORY_MAX_CHARS = int(os.environ.get("CONTEXT_HISTORY_MAX_CHARS", "40000"))
CONTEXT_LORE_MAX_CHARS = int(os.environ.get("CONTEXT_LORE_MAX_CHARS", "50000"))

# Logging - never log API keys.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "level": "INFO"},
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "rpg": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}