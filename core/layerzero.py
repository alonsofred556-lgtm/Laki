from django.conf import settings


def endpoint_id(chain: str) -> int:
    try:
        return settings.LAYERZERO_ENDPOINT_IDS[chain]
    except KeyError as exc:
        raise ValueError(f"unsupported LayerZero route: {chain}") from exc


def route_summary(source: str, destination: str) -> dict:
    if source == destination:
        raise ValueError("LayerZero source and destination must differ")
    return {"from": source, "to": destination, "fromEid": endpoint_id(source), "toEid": endpoint_id(destination), "provider": "pangea-layerzero-oft"}
