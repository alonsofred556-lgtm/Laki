import os
from pathlib import Path
from urllib.parse import urlsplit

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "").strip()
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "development-only-change-me"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set when DJANGO_DEBUG=false")

def _normalise_host(value):
    value = value.strip()
    if not value:
        return ""
    if "://" in value:
        value = urlsplit(value).hostname or ""
    return value.split("/", 1)[0].strip()


configured_hosts = {
    host
    for host in (_normalise_host(item) for item in os.environ.get("DJANGO_ALLOWED_HOSTS", "").split(","))
    if host
}
ALLOWED_HOSTS = sorted(configured_hosts | {
    "127.0.0.1",
    "localhost",
    "laki-production.up.railway.app",
})
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

import dj_database_url

# Production and normal development always require PostgreSQL. The explicit
# test switch exists only so the suite can run without a local Postgres
# service; it must never be enabled in a deployed web/worker process.
test_mode = os.environ.get("CONI_TEST_MODE", "false").lower() == "true"
if test_mode and not DEBUG:
    raise RuntimeError("CONI_TEST_MODE requires DJANGO_DEBUG=true")
if test_mode and os.environ.get("CONI_EXECUTION_MODE", "disabled") == "live":
    raise RuntimeError("CONI_TEST_MODE cannot be used with live execution")
if test_mode:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }
else:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL must be set to a PostgreSQL connection string")
    if not database_url.lower().startswith(("postgres://", "postgresql://")):
        raise RuntimeError("DATABASE_URL must use PostgreSQL (postgres:// or postgresql://)")
    DATABASES = {
        "default": dj_database_url.parse(
            database_url,
            conn_max_age=60,
            ssl_require=os.environ.get("DATABASE_SSL", "require").lower() != "disable",
        )
    }

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
# Chrome extensions do not use the website's HTTP origin. Keep their origins
# in a separate explicit allowlist; never accept an arbitrary
# chrome-extension:// origin or a wildcard.
CONI_EXTENSION_ORIGINS = {item.strip() for item in os.environ.get("CONI_EXTENSION_ORIGINS", "").split(",") if item.strip()}
CSRF_TRUSTED_ORIGINS = list(CONI_ALLOWED_ORIGINS)
CONI_AUTH_DOMAIN = os.environ.get("CONI_AUTH_DOMAIN", "localhost:8000")
CONI_AUTH_URI = os.environ.get("CONI_AUTH_URI", "http://localhost:8000")
CONI_OPS_TOKEN = os.environ.get("CONI_OPS_TOKEN", "")
CONI_EXECUTION_MODE = os.environ.get("CONI_EXECUTION_MODE", "disabled")
CONI_SIGNER_URL = os.environ.get("CONI_SIGNER_URL", "")
CONI_SIGNER_PROVISION_SECRET = os.environ.get("CONI_SIGNER_PROVISION_SECRET", "")
CONI_SIGNER_EVENT_SECRET = os.environ.get("CONI_SIGNER_EVENT_SECRET", "")
CONI_SIGNER_CALLBACK_REQUIRE_HMAC = os.environ.get("CONI_SIGNER_CALLBACK_REQUIRE_HMAC", "true" if not DEBUG else "false").lower() == "true"
CONI_SIGNER_CALLBACK_MAX_SKEW_SECONDS = int(os.environ.get("CONI_SIGNER_CALLBACK_MAX_SKEW_SECONDS", "60"))
CONI_LIVE_TRADING_CONFIRMATION = os.environ.get("CONI_LIVE_TRADING_CONFIRMATION", "")
CONI_SIGNER_AUDIT_APPROVED = os.environ.get("CONI_SIGNER_AUDIT_APPROVED", "false").lower() == "true"
CONI_SIGNER_WITHDRAWAL_KEY_REFS = os.environ.get("CONI_SIGNER_WITHDRAWAL_KEY_REFS", "{}")
CONI_SIGNER_FEE_KEY_REFS = os.environ.get("CONI_SIGNER_FEE_KEY_REFS", "{}")
CONI_WITHDRAWAL_APPROVALS_ENABLED = os.environ.get("CONI_WITHDRAWAL_APPROVALS_ENABLED", "false").lower() == "true"
CONI_DEFAULT_COST_BUFFER_BPS = int(os.environ.get("CONI_DEFAULT_COST_BUFFER_BPS", "55"))
_fee_default_bps = "0" if test_mode else "10"
CONI_TRADING_FEE_BPS = int(os.environ.get("CONI_TRADING_FEE_BPS", _fee_default_bps))
CONI_DEFAULT_LAUNCH_FEE_BPS = int(os.environ.get("CONI_DEFAULT_LAUNCH_FEE_BPS", "500"))
CONI_BOT_FUNDING_FEE_BPS = int(os.environ.get("CONI_BOT_FUNDING_FEE_BPS", _fee_default_bps))
CONI_BOT_WITHDRAWAL_FEE_BPS = int(os.environ.get("CONI_BOT_WITHDRAWAL_FEE_BPS", _fee_default_bps))
CONI_WITHDRAWAL_FEE_BPS = int(os.environ.get("CONI_WITHDRAWAL_FEE_BPS", _fee_default_bps))
CONI_FUNDING_RECIPIENTS = os.environ.get("CONI_FUNDING_RECIPIENTS", "{}")
CONI_DEPOSIT_RECIPIENTS = os.environ.get("CONI_DEPOSIT_RECIPIENTS", "{}")
CONI_FEE_RECIPIENTS = os.environ.get("CONI_FEE_RECIPIENTS", "{}")
CONI_TOKEN_REGISTRY = os.environ.get("CONI_TOKEN_REGISTRY", "{}")
CONI_RPC_URLS = os.environ.get("CONI_RPC_URLS", "{}")
CONI_EXPECTED_CHAIN_IDS = os.environ.get("CONI_EXPECTED_CHAIN_IDS", "{}")
CONI_SOLANA_RPC_URL = os.environ.get("CONI_SOLANA_RPC_URL", "")
CONI_DEPOSIT_CONFIRMATIONS = int(os.environ.get("CONI_DEPOSIT_CONFIRMATIONS", "12"))
CONI_DEPOSIT_MAX_AGE_MINUTES = int(os.environ.get("CONI_DEPOSIT_MAX_AGE_MINUTES", "1440"))
CONI_DEPOSIT_SCAN_BATCH = int(os.environ.get("CONI_DEPOSIT_SCAN_BATCH", "50"))
CONI_DEPOSIT_SCAN_LOOKBACK_BLOCKS = int(os.environ.get("CONI_DEPOSIT_SCAN_LOOKBACK_BLOCKS", "2000"))
CONI_RECONCILE_ONCHAIN_BALANCES = os.environ.get(
    "CONI_RECONCILE_ONCHAIN_BALANCES",
    "false" if DEBUG else "true",
).lower() == "true"
CONI_REDIS_URL = os.environ.get("CONI_REDIS_URL", "")
CONI_REDIS_REQUIRED = os.environ.get("CONI_REDIS_REQUIRED", "true" if not DEBUG else "false").lower() == "true"

# Use the same Redis service for Django's shared cache when it is configured.
# A process-local cache is acceptable only for local development; Gunicorn
# workers otherwise do not share Pangea snapshots or token logo results.
if CONI_REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": CONI_REDIS_URL,
            "OPTIONS": {
                "socket_connect_timeout": 2,
                "socket_timeout": 2,
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "coni-local-cache",
        }
    }

CONI_AUDIT_HASH_KEY = os.environ.get("CONI_AUDIT_HASH_KEY", "")
CONI_ARBITRAGE_NOTIONAL_USD = os.environ.get("CONI_ARBITRAGE_NOTIONAL_USD", "10")
CONI_ARBITRAGE_MIN_NET_BPS = int(os.environ.get("CONI_ARBITRAGE_MIN_NET_BPS", "100"))
CONI_ARBITRAGE_MIN_PROFIT_USD = os.environ.get("CONI_ARBITRAGE_MIN_PROFIT_USD", "0.05")
CONI_ARBITRAGE_MAX_NOTIONAL_USD = os.environ.get("CONI_ARBITRAGE_MAX_NOTIONAL_USD", "25")
CONI_ARBITRAGE_RISK_BUFFER_BPS = int(os.environ.get("CONI_ARBITRAGE_RISK_BUFFER_BPS", "75"))
# Explicit per-chain native base-unit reserves required before planning. Do not
# infer gas from a positive balance; the operator must size these conservatively.
CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS = os.environ.get("CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS", "{}")
CONI_ARBITRAGE_SLIPPAGE_BPS = int(os.environ.get("CONI_ARBITRAGE_SLIPPAGE_BPS", "100"))
CONI_ARBITRAGE_REQUIRE_QUOTES = os.environ.get("CONI_ARBITRAGE_REQUIRE_QUOTES", "true").lower() == "true"
CONI_ARBITRAGE_MAX_BRIDGE_SECONDS = int(os.environ.get("CONI_ARBITRAGE_MAX_BRIDGE_SECONDS", "120"))
CONI_ARBITRAGE_MAX_QUOTE_REQUESTS = int(os.environ.get("CONI_ARBITRAGE_MAX_QUOTE_REQUESTS", "20"))
CONI_ARBITRAGE_SCAN_INTERVAL_SECONDS = int(os.environ.get("CONI_ARBITRAGE_SCAN_INTERVAL_SECONDS", "10"))
CONI_ARBITRAGE_QUOTE_INTERVAL_SECONDS = int(os.environ.get("CONI_ARBITRAGE_QUOTE_INTERVAL_SECONDS", "5"))
CONI_ARBITRAGE_PLAN_INTERVAL_SECONDS = int(os.environ.get("CONI_ARBITRAGE_PLAN_INTERVAL_SECONDS", "5"))
# Enumerate every valid Pangea route by default. Set a positive value only as
# an explicit operational safety cap; quote requests remain separately bounded.
CONI_ARBITRAGE_MAX_CANDIDATES = int(os.environ.get("CONI_ARBITRAGE_MAX_CANDIDATES", "0"))
CONI_ARBITRAGE_MAX_QUOTE_AGE_SECONDS = int(os.environ.get("CONI_ARBITRAGE_MAX_QUOTE_AGE_SECONDS", "5"))
CONI_ARBITRAGE_MAX_PRICE_MOVE_BPS = int(os.environ.get("CONI_ARBITRAGE_MAX_PRICE_MOVE_BPS", "200"))
CONI_ARBITRAGE_MAX_MARKET_CAP_MOVE_BPS = int(os.environ.get("CONI_ARBITRAGE_MAX_MARKET_CAP_MOVE_BPS", "2000"))
CONI_ARBITRAGE_REQUIRE_MARKET_CAP_DATA = os.environ.get(
    "CONI_ARBITRAGE_REQUIRE_MARKET_CAP_DATA",
    "false" if DEBUG else "true",
).lower() == "true"
CONI_ARBITRAGE_MIN_DEPTH_MULTIPLE = os.environ.get("CONI_ARBITRAGE_MIN_DEPTH_MULTIPLE", "3")
CONI_ARBITRAGE_MAX_PRICE_IMPACT_BPS = int(os.environ.get("CONI_ARBITRAGE_MAX_PRICE_IMPACT_BPS", "100"))
CONI_ARBITRAGE_MIN_BRIDGE_RETENTION_BPS = int(os.environ.get("CONI_ARBITRAGE_MIN_BRIDGE_RETENTION_BPS", "9900"))
CONI_ARBITRAGE_MIN_CONFIDENCE_BPS = int(os.environ.get("CONI_ARBITRAGE_MIN_CONFIDENCE_BPS", "8500"))
CONI_DEFAULT_COMPOUND_PROFIT_BPS = int(os.environ.get("CONI_DEFAULT_COMPOUND_PROFIT_BPS", "7000"))
CONI_BOT_WITHDRAWAL_LIMITS = os.environ.get("CONI_BOT_WITHDRAWAL_LIMITS", "{}")
CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED = os.environ.get(
    "CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED",
    "false" if DEBUG else "true",
).lower() == "true"
CONI_BOT_WALLETS = os.environ.get("CONI_BOT_WALLETS", "{}")
CONI_PROFIT_SETTLEMENT_ASSET = "USDG"
CONI_PROFIT_SETTLEMENT_CHAIN = "robinhood"
CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS = os.environ.get("CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS", "")
_profit_decimals = os.environ.get("CONI_PROFIT_SETTLEMENT_DECIMALS", "")
CONI_PROFIT_SETTLEMENT_DECIMALS = int(_profit_decimals) if _profit_decimals.isdigit() and int(_profit_decimals) <= 36 else None
CONI_PROFIT_SETTLEMENT_ENABLED = os.environ.get("CONI_PROFIT_SETTLEMENT_ENABLED", "false").lower() == "true"
CONI_PROFIT_SETTLEMENT_PRICE_USD = os.environ.get("CONI_PROFIT_SETTLEMENT_PRICE_USD", "")

PANGEA_API = "https://www.pangea.trade/api"
CONI_PANGEA_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONI_PANGEA_CONNECT_TIMEOUT_SECONDS", "3.05"))
CONI_PANGEA_READ_TIMEOUT_SECONDS = float(os.environ.get("CONI_PANGEA_READ_TIMEOUT_SECONDS", "6"))
CONI_PANGEA_LAUNCH_FEED_TIMEOUT_SECONDS = float(os.environ.get("CONI_PANGEA_LAUNCH_FEED_TIMEOUT_SECONDS", "2"))
CONI_PANGEA_LAUNCH_FEED_CACHE_SECONDS = max(1, int(os.environ.get("CONI_PANGEA_LAUNCH_FEED_CACHE_SECONDS", "15")))
CONI_MOTION_ENABLED = os.environ.get("CONI_MOTION_ENABLED", "false").lower() == "true"
CONI_MOTION_API_URL = os.environ.get("CONI_MOTION_API_URL", "https://motion.gift/api/v1")
CONI_MOTION_API_KEY = os.environ.get("MOTION_API_KEY", "")
CONI_MOTION_CONNECT_TIMEOUT_SECONDS = float(os.environ.get("CONI_MOTION_CONNECT_TIMEOUT_SECONDS", "3.05"))
CONI_MOTION_READ_TIMEOUT_SECONDS = float(os.environ.get("CONI_MOTION_READ_TIMEOUT_SECONDS", "6"))
CONI_MOTION_CACHE_SECONDS = max(60, int(os.environ.get("CONI_MOTION_CACHE_SECONDS", "300")))
CONI_MOTION_MAX_LIMIT = max(1, min(50, int(os.environ.get("CONI_MOTION_MAX_LIMIT", "20"))))
LAYERZERO_ENDPOINT_IDS = {"ethereum": 30101, "base": 30184, "robinhood": 30416, "bsc": 30102, "solana": 30168}
