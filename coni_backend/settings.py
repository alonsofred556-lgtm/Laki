import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "development-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
if not DEBUG and SECRET_KEY == "development-only-change-me":
    raise RuntimeError("DJANGO_SECRET_KEY must be set when DEBUG=false")

ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]
ROOT_URLCONF = "coni_backend.urls"
WSGI_APPLICATION = "coni_backend.wsgi.application"
ASGI_APPLICATION = "coni_backend.asgi.application"

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]

DATABASES = {}
if os.environ.get("DATABASE_URL"):
    import dj_database_url

    database_url = os.environ["DATABASE_URL"]
    is_sqlite = database_url.lower().startswith(("sqlite://", "sqlite3://"))
    DATABASES["default"] = dj_database_url.parse(
        database_url,
        conn_max_age=60,
        ssl_require=(not is_sqlite and os.environ.get("DATABASE_SSL", "require") != "disable"),
    )
elif DEBUG:
    DATABASES["default"] = {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}
else:
    raise RuntimeError("DATABASE_URL must be set when DJANGO_DEBUG=false")

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_NAME = "coni_session"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
SESSION_COOKIE_AGE = 60 * 60 * 12

CSRF_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SECURE_SSL_REDIRECT", "false" if DEBUG else "true").lower() == "true"
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_HSTS_SECONDS = 31536000 if not DEBUG else 0
SECURE_HSTS_INCLUDE_SUBDOMAINS = not DEBUG
SECURE_HSTS_PRELOAD = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "assets"]
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CONI_ALLOWED_ORIGINS = {item.strip() for item in os.environ.get("CONI_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",") if item.strip()}
CSRF_TRUSTED_ORIGINS = list(CONI_ALLOWED_ORIGINS)
CONI_AUTH_DOMAIN = os.environ.get("CONI_AUTH_DOMAIN", "localhost:8000")
CONI_AUTH_URI = os.environ.get("CONI_AUTH_URI", "http://localhost:8000")
CONI_OPS_TOKEN = os.environ.get("CONI_OPS_TOKEN", "")
CONI_EXECUTION_MODE = os.environ.get("CONI_EXECUTION_MODE", "disabled")
CONI_SIGNER_URL = os.environ.get("CONI_SIGNER_URL", "")
CONI_LIVE_TRADING_CONFIRMATION = os.environ.get("CONI_LIVE_TRADING_CONFIRMATION", "")
CONI_DEFAULT_COST_BUFFER_BPS = int(os.environ.get("CONI_DEFAULT_COST_BUFFER_BPS", "55"))
CONI_TRADING_FEE_BPS = int(os.environ.get("CONI_TRADING_FEE_BPS", "25"))
CONI_BOT_FUNDING_FEE_BPS = int(os.environ.get("CONI_BOT_FUNDING_FEE_BPS", "0"))
CONI_FUNDING_RECIPIENTS = os.environ.get("CONI_FUNDING_RECIPIENTS", "{}")
CONI_FEE_RECIPIENTS = os.environ.get("CONI_FEE_RECIPIENTS", "{}")

PANGEA_API = "https://www.pangea.trade/api"
LAYERZERO_ENDPOINT_IDS = {"ethereum": 30101, "base": 30184, "robinhood": 30416, "bsc": 30102, "solana": 30168}
