import requests
from django.conf import settings


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
        result.append({"slug": launch.get("slug"), "symbol": launch.get("symbol"), "buyChain": buy.get("chain"), "sellChain": sell.get("chain"), "grossBps": gross_bps, "netBps": net_bps, "depthUsd": min(float(buy.get("depth") or 0), float(sell.get("depth") or 0)), "executable": net_bps > 0})
    return sorted(result, key=lambda item: item["netBps"], reverse=True)[:100]
