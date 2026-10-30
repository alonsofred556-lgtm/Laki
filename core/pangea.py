from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache
from requests.adapters import HTTPAdapter


PANGEA_ROOT = "https://www.pangea.trade"
LAUNCH_FEED_CACHE_KEY = "coni:pangea:launch-feed"
LAUNCH_FEED_STALE_KEY = "coni:pangea:launch-feed:stale"

# Keep a small connection pool for the quote chain. A fresh TCP/TLS handshake
# for every buy -> bridge -> sell request adds avoidable latency, while the
# pool stays bounded and does not change the quote or signing policy.
_PANGEA_SESSION = requests.Session()
_PANGEA_SESSION.mount("https://", HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=0))


class PangeaError(RuntimeError):
    pass


def request_json(path: str, method: str = "GET", payload: dict | None = None, *, timeout: float | None = None):
    url = f"{settings.PANGEA_API}{path}"
    read_timeout = settings.CONI_PANGEA_READ_TIMEOUT_SECONDS if timeout is None else float(timeout)
    if read_timeout <= 0:
        raise ValueError("Pangea read timeout must be positive")
    try:
        response = _PANGEA_SESSION.request(
            method,
            url,
            json=payload,
            headers={"Accept": "application/json", "User-Agent": "ConiDjango/0.1"},
            timeout=(
                settings.CONI_PANGEA_CONNECT_TIMEOUT_SECONDS,
                read_timeout,
            ),
        )
    except requests.RequestException as exc:
        raise PangeaError("Pangea request failed; retryable upstream outage") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise PangeaError("Pangea returned invalid JSON") from exc
    if not response.ok:
        raise PangeaError(data.get("error", f"Pangea responded {response.status_code}"))
    return data


def scan_opportunities():
    # Kept as the compatibility import used by views and the legacy proxy.
    from .arbitrage import scan_opportunities as quote_aware_scan
    return quote_aware_scan()


def _image_url(image):
    if isinstance(image, dict):
        image = image.get("url") or image.get("src") or image.get("uri")
    if not isinstance(image, str) or not image:
        return None
    if image.startswith("/api/logo/"):
        # Pangea's launch feed returns same-origin relative logo paths. Only
        # allow its fixed logo route; never turn an arbitrary path into a
        # browser URL or proxy an untrusted host.
        return f"{PANGEA_ROOT}{image}"
    if image.startswith("ipfs://"):
        parts = image[7:].split("/", 1)
        return f"{PANGEA_ROOT}/api/logo/{parts[0]}/256.webp"
    parsed = urlparse(image)
    if parsed.scheme == "https" and parsed.netloc:
        return image
    return None


def _launch_items(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("launches"), list):
        return payload["launches"]
    return []


def market_snapshot():
    """Return one normalized live pool/launch snapshot for all consumers.

    `/pools` is the authoritative source for current prices and route pools.
    The launch feed is merged for newly listed launches and metadata that may
    not yet be present in the pool response. A launch-feed outage does not
    hide already-known pools; a pool outage still fails closed because route
    discovery cannot safely proceed without current market data.
    """
    pools_payload = request_json("/pools")
    if isinstance(pools_payload, list):
        pools_payload = {"launches": pools_payload}
    if not isinstance(pools_payload, dict):
        raise PangeaError("Pangea pools response is not an object")

    by_slug = {
        str(item.get("slug")): dict(item)
        for item in _launch_items(pools_payload)
        if isinstance(item, dict) and item.get("slug")
    }
    launch_payload = cache.get(LAUNCH_FEED_CACHE_KEY)
    if launch_payload is None:
        stale_launch_payload = cache.get(LAUNCH_FEED_STALE_KEY)
        try:
            launch_payload = request_json(
                "/v1/launches?limit=100",
                timeout=settings.CONI_PANGEA_LAUNCH_FEED_TIMEOUT_SECONDS,
            )
            if isinstance(launch_payload, (dict, list)):
                cache.set(
                    LAUNCH_FEED_CACHE_KEY,
                    launch_payload,
                    settings.CONI_PANGEA_LAUNCH_FEED_CACHE_SECONDS,
                )
                cache.set(LAUNCH_FEED_STALE_KEY, launch_payload, 3600)
        except PangeaError:
            # A stale metadata feed is safe to merge with fresh pools. It can
            # enrich logos/names, but it never authorizes a route or trade.
            launch_payload = stale_launch_payload
    for item in _launch_items(launch_payload):
        if not isinstance(item, dict) or not item.get("slug"):
            continue
        slug = str(item["slug"])
        merged = dict(item)
        merged.update(by_slug.get(slug, {}))
        if not isinstance(merged.get("pools"), list) and isinstance(item.get("pools"), list):
            merged["pools"] = item["pools"]
        by_slug[slug] = merged

    return {
        **pools_payload,
        "launches": list(by_slug.values()),
    }


def _chain_addresses(launch):
    addresses = {}
    chains = launch.get("chains")
    if isinstance(chains, dict):
        for chain, value in chains.items():
            if isinstance(value, dict) and value.get("token"):
                addresses[chain] = value["token"]
    elif isinstance(chains, list):
        for value in chains:
            if isinstance(value, dict) and value.get("chain") and value.get("token"):
                addresses[value["chain"]] = value["token"]
    for pool in launch.get("pools", []) or []:
        if isinstance(pool, dict) and pool.get("chain") and pool.get("token"):
            addresses[pool["chain"]] = pool["token"]
    return addresses


def token_registry_snapshot():
    """Return a stable, frontend-safe token list assembled from Pangea feeds."""
    cached = cache.get("coni:pangea:tokens")
    if cached:
        return cached
    stale_key = "coni:pangea:tokens:stale"
    stale = cache.get(stale_key)
    try:
        pools = market_snapshot()
    except PangeaError:
        # A public market read should continue serving the last known-good
        # snapshot during an upstream outage. It is explicitly stale data;
        # trading workers still request fresh quotes and apply their own gates.
        if stale:
            return stale
        raise
    launches = {item.get("slug"): item for item in _launch_items(pools) if item.get("slug")}
    result = []
    for slug, launch in launches.items():
        image = _image_url(launch.get("image"))
        if not image:
            # Do not make the critical token-list request wait on one metadata
            # call per launch. The frontend and `/market/token-meta` endpoint
            # can resolve uncached logos separately; a slow metadata provider
            # must never take down a Gunicorn worker serving the token list.
            image = _image_url(cache.get(f"coni:pangea:token-image:{slug}"))
        result.append({
            "slug": slug,
            "name": launch.get("name") or slug,
            "symbol": launch.get("symbol") or slug.upper(),
            "image": image,
            "metadataUri": launch.get("metadataURI"),
            "addresses": _chain_addresses(launch),
            # The market endpoint already carries the live pool rows. Keep
            # this registry response compact so logo/name refreshes do not
            # serialize the entire multi-chain market snapshot a second time.
            "launchChains": launch.get("launchChains") or list(_chain_addresses(launch)),
            "readAt": pools.get("readAt"),
        })
    result.sort(key=lambda item: item["symbol"])
    cache.set("coni:pangea:tokens", result, 45)
    cache.set(stale_key, result, 3600)
    return result
