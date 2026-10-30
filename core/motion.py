"""Optional, server-side Motion market-data integration.

Motion is used only as a supplementary discovery feed.  Pangea remains the
authoritative source for Coni's cross-chain route candidates and executable
quotes.  This module never accepts a browser-supplied API key, transaction,
calldata, or signing request.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter


MOTION_CHAINS = {
    "robinhood": "robinhood",
    "bsc": "bsc",
    "solana": "sol",
}
_SESSION = requests.Session()
_SESSION.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0))
_API_KEY_RE = re.compile(r"^msk_[A-Za-z0-9_-]{43}$")


class MotionError(RuntimeError):
    """Raised when the optional Motion feed cannot be used safely."""


def configured() -> bool:
    key = str(getattr(settings, "CONI_MOTION_API_KEY", "") or "")
    url = str(getattr(settings, "CONI_MOTION_API_URL", "") or "")
    parsed = urlparse(url)
    return (
        bool(getattr(settings, "CONI_MOTION_ENABLED", False))
        and _API_KEY_RE.fullmatch(key) is not None
        and parsed.scheme == "https"
        and bool(parsed.netloc)
    )


def _chain(chain: str | None) -> str:
    value = str(chain or "").strip().lower()
    if value not in MOTION_CHAINS:
        raise ValueError("Motion supports only robinhood, bsc, and solana discovery")
    return value


def _limit(limit) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid Motion limit") from exc
    maximum = max(1, min(50, int(getattr(settings, "CONI_MOTION_MAX_LIMIT", 20))))
    if not 1 <= value <= maximum:
        raise ValueError("invalid Motion limit")
    return value


def request_json(path: str, *, params: dict | None = None, timeout: float | None = None):
    if not configured():
        raise MotionError("Motion discovery is not configured")
    if not isinstance(path, str) or not path.startswith("/") or ".." in path:
        raise ValueError("invalid Motion path")
    base = str(settings.CONI_MOTION_API_URL).rstrip("/")
    read_timeout = float(timeout if timeout is not None else settings.CONI_MOTION_READ_TIMEOUT_SECONDS)
    if read_timeout <= 0:
        raise ValueError("Motion read timeout must be positive")
    try:
        response = _SESSION.get(
            f"{base}{path}",
            params=params or {},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {settings.CONI_MOTION_API_KEY}",
                "User-Agent": "ConiDjango/0.1",
            },
            timeout=(float(settings.CONI_MOTION_CONNECT_TIMEOUT_SECONDS), read_timeout),
        )
    except requests.RequestException as exc:
        raise MotionError("Motion request failed; retryable upstream outage") from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise MotionError("Motion returned invalid JSON") from exc
    if not response.ok or not isinstance(payload, dict) or payload.get("ok") is not True:
        code = payload.get("error") if isinstance(payload, dict) else None
        raise MotionError(str(code or f"Motion responded {response.status_code}"))
    return payload


def _normalize_row(row: dict, *, chain: str, meta: dict | None) -> dict | None:
    address = row.get("address")
    symbol = row.get("symbol")
    if not isinstance(address, str) or not address or not isinstance(symbol, str) or not symbol:
        return None
    return {
        "chain": chain,
        "motionChain": MOTION_CHAINS[chain],
        "address": address,
        "symbol": symbol,
        "name": row.get("name") or symbol,
        "launchpad": row.get("launchpad"),
        "stage": row.get("stage"),
        "priceUsd": row.get("priceUsd"),
        "mcapUsd": row.get("mcapUsd"),
        "liquidityUsd": row.get("liquidityUsd"),
        "volume24hUsd": row.get("volume24hUsd"),
        "change5mPct": row.get("change5mPct"),
        "change1hPct": row.get("change1hPct"),
        "change24hPct": row.get("change24hPct"),
        "trades": row.get("trades"),
        "holders": row.get("holders"),
        "ageSeconds": row.get("ageSeconds"),
        "signals": row.get("signals") if isinstance(row.get("signals"), dict) else {},
        "url": row.get("url"),
        "readAt": (meta or {}).get("at"),
        "source": "motion",
    }


def _read_chain(chain: str, limit: int) -> tuple[list[dict], bool]:
    key = f"coni:motion:trending:{chain}"
    cached = cache.get(key)
    if isinstance(cached, list):
        return cached[:limit], False
    stale_key = f"{key}:stale"
    try:
        payload = request_json("/trending", params={"chain": MOTION_CHAINS[chain], "source": "motion", "limit": 50})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        rows = [_normalize_row(item, chain=chain, meta=payload.get("meta")) for item in data.get("rows", []) if isinstance(item, dict)]
        rows = [item for item in rows if item]
        cache_seconds = max(60, int(getattr(settings, "CONI_MOTION_CACHE_SECONDS", 300)))
        cache.set(key, rows, cache_seconds)
        cache.set(stale_key, rows, 3600)
        return rows[:limit], False
    except (MotionError, ValueError):
        stale = cache.get(stale_key)
        if isinstance(stale, list):
            return stale[:limit], True
        raise


def discoveries(*, chain: str | None = None, limit: int = 20) -> dict:
    """Return normalized read-only discoveries, marked stale on fallback."""
    requested_limit = _limit(limit)
    if not configured():
        return {"enabled": False, "source": "motion", "rows": [], "chains": [], "stale": False}
    chains = [_chain(chain)] if chain else list(MOTION_CHAINS)
    rows = []
    stale = False
    errors = []
    for item in chains:
        try:
            chain_rows, chain_stale = _read_chain(item, requested_limit)
            rows.extend(chain_rows)
            stale = stale or chain_stale
        except (MotionError, ValueError) as exc:
            errors.append({"chain": item, "error": str(exc)})
    return {
        "enabled": True,
        "source": "motion",
        "chains": chains,
        "rows": rows,
        "stale": stale,
        "errors": errors,
        "readAt": datetime.now(timezone.utc).isoformat(),
    }
