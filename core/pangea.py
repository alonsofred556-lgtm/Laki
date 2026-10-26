from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.cache import cache


PANGEA_ROOT = "https://www.pangea.trade"


class PangeaError(RuntimeError):
    pass


def request_json(path: str, method: str = "GET", payload: dict | None = None):
    url = f"{settings.PANGEA_API}{path}"
    response = requests.request(method, url, json=payload, headers={"Accept": "application/json", "User-Agent": "ConiDjango/0.1"}, timeout=10)
    try:
        data = response.json()
    except ValueError as exc:
        raise PangeaError("Pangea returned invalid JSON") from exc
    if not response.ok:
        raise PangeaError(data.get("error", f"Pangea responded {response.status_code}"))
    return data


def scan_opportunities():
    data = request_json("/pools")
    result = []
    for launch in data.get("launches", []):
        pools = [pool for pool in launch.get("pools", []) if float(pool.get("priceUsd") or 0) > 0]
        if len(pools) < 2:
            continue
        buy = min(pools, key=lambda item: float(item["priceUsd"]))
        sell = max(pools, key=lambda item: float(item["priceUsd"]))
        gross_bps = int(((float(sell["priceUsd"]) - float(buy["priceUsd"])) / float(buy["priceUsd"])) * 10_000)
        net_bps = gross_bps - settings.CONI_DEFAULT_COST_BUFFER_BPS
        result.append({"slug": launch.get("slug"), "name": launch.get("name"), "symbol": launch.get("symbol"), "image": launch.get("image"), "buyChain": buy.get("chain"), "sellChain": sell.get("chain"), "grossBps": gross_bps, "netBps": net_bps, "depthUsd": min(float(buy.get("depth") or 0), float(sell.get("depth") or 0)), "executable": net_bps > 0})
    return sorted(result, key=lambda item: item["netBps"], reverse=True)[:100]


def _image_url(image):
    if isinstance(image, dict):
        image = image.get("url") or image.get("src") or image.get("uri")
    if not isinstance(image, str) or not image:
        return None
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
    pools = request_json("/pools")
    launches = {item.get("slug"): item for item in _launch_items(pools) if item.get("slug")}
    try:
        launch_feed = request_json("/v1/launches?limit=100")
        for item in _launch_items(launch_feed):
            if item.get("slug"):
                launches[item["slug"]] = {**launches.get(item["slug"], {}), **item}
    except PangeaError:
        pass
    result = []
    for slug, launch in launches.items():
        pools_for_launch = [item for item in launch.get("pools", []) if isinstance(item, dict)]
        image = _image_url(launch.get("image"))
        if not image:
            try:
                image = _image_url(request_json(f"/token/meta?launch={slug}").get("image"))
            except PangeaError:
                image = None
        result.append({
            "slug": slug,
            "name": launch.get("name") or slug,
            "symbol": launch.get("symbol") or slug.upper(),
            "image": image,
            "metadataUri": launch.get("metadataURI"),
            "addresses": _chain_addresses(launch),
            "pools": pools_for_launch,
            "launchChains": launch.get("launchChains") or list(_chain_addresses(launch)),
            "readAt": pools.get("readAt"),
        })
    result.sort(key=lambda item: item["symbol"])
    cache.set("coni:pangea:tokens", result, 45)
    return result
