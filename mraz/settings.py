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

PUBLIC_PLAYER_HOST = os.environ.get("PUBLIC_PLAYER_HOST", "").strip().lower()
if PUBLIC_PLAYER_HOST:
    if (
        "://" in PUBLIC_PLAYER_HOST
        or "/" in PUBLIC_PLAYER_HOST
        or ":" in PUBLIC_PLAYER_HOST
    ):
        raise RuntimeError(
            "PUBLIC_PLAYER_HOST must be a bare hostname, without scheme, port, or path"
        )
    if PUBLIC_PLAYER_HOST not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(PUBLIC_PLAYER_HOST)
    public_player_origin = f"https://{PUBLIC_PLAYER_HOST}"
    if public_player_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(public_player_origin)

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
    "axes",
    "rpg",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "rpg.middleware.PublicPlayerHostMiddleware",
    "rpg.middleware.PlayerTokenSafetyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "rpg.middleware.GameMasterAccessMiddleware",
    "rpg.middleware.CampaignLifecycleMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",
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

LOGIN_URL = "gm_login"
LOGIN_REDIRECT_URL = "campaigns"
LOGOUT_REDIRECT_URL = "gm_login"

# django-axes uses the database across workers, covering both /auth/login/
# and Django Admin. Trust REMOTE_ADDR only; do not accept spoofable XFF data.
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_CLIENT_IP_CALLABLE = "rpg.security.client_ip_from_remote_addr"
AXES_FAILURE_LIMIT = int(os.environ.get("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = int(os.environ.get("AXES_COOLOFF_HOURS", "1"))
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]
AXES_RESET_ON_SUCCESS = True

# HTTPS is opt-in because local development uses HTTP. Enable this only behind
# a trusted TLS gateway which removes any client-supplied X-Forwarded-Proto.
DJANGO_HTTPS_ENABLED = _env_bool("DJANGO_HTTPS_ENABLED", False)
TRUST_PROXY_SSL_HEADER = _env_bool("TRUST_PROXY_SSL_HEADER", False)
SECURE_PROXY_SSL_HEADER = (
    ("HTTP_X_FORWARDED_PROTO", "https") if TRUST_PROXY_SSL_HEADER else None
)
SECURE_SSL_REDIRECT = DJANGO_HTTPS_ENABLED
SECURE_REDIRECT_EXEMPT = [r"^health/$"]
SESSION_COOKIE_SECURE = DJANGO_HTTPS_ENABLED
CSRF_COOKIE_SECURE = DJANGO_HTTPS_ENABLED
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(os.environ.get("GM_SESSION_AGE_SECONDS", "43200"))
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SECURE_HSTS_SECONDS = (
    int(os.environ.get("SECURE_HSTS_SECONDS", "3600")) if DJANGO_HTTPS_ENABLED else 0
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SECURE_HSTS_PRELOAD = False
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

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

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

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

# Token-gated player endpoints: limits are per scene token, per endpoint class.
# GET status/fragment polling is intentionally not throttled here.
PLAYER_RATE_LIMITS = {
    "turn": (int(os.environ.get("PLAYER_TURN_LIMIT", "12")), 60),
    "ooc": (int(os.environ.get("PLAYER_OOC_LIMIT", "10")), 60),
    "appearance": (int(os.environ.get("PLAYER_APPEARANCE_LIMIT", "20")), 60),
    "upload": (int(os.environ.get("PLAYER_UPLOAD_LIMIT", "3")), 300),
    "qr": (int(os.environ.get("PLAYER_QR_LIMIT", "30")), 60),
}

# Logging - never log API keys.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "redact_player_tokens": {"()": "rpg.player_security.PlayerTokenRedactionFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "INFO",
            "filters": ["redact_player_tokens"],
        },
    },
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "rpg": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}