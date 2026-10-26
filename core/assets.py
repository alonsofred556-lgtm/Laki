"""Validated asset and recipient configuration shared by API and workers."""

import json

from django.conf import settings


NATIVE_ASSETS = {
    "ethereum": ("ETH", 18),
    "base": ("ETH", 18),
    "robinhood": ("ETH", 18),
    "bsc": ("BNB", 18),
    "solana": ("SOL", 9),
}


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def token_registry():
    return _json_object(getattr(settings, "CONI_TOKEN_REGISTRY", "{}"))


def token_config(asset, chain):
    asset_config = token_registry().get(str(asset).upper(), {})
    if not isinstance(asset_config, dict):
        return None
    config = asset_config.get(chain)
    if isinstance(config, str):
        return {"address": config}
    return config if isinstance(config, dict) else None


def asset_decimals(asset, chain=None):
    asset = str(asset).upper()
    if chain in NATIVE_ASSETS and NATIVE_ASSETS[chain][0] == asset:
        return NATIVE_ASSETS[chain][1]
    config = token_config(asset, chain) if chain else None
    decimals = config.get("decimals") if config else None
    return int(decimals) if isinstance(decimals, int) and 0 <= decimals <= 36 else None


def supported_asset(asset, chain):
    return asset_decimals(asset, chain) is not None or bool(token_config(asset, chain))


def recipient_for(asset, chain):
    """Resolve a chain-specific recipient, with legacy config compatibility."""
    asset, chain = str(asset).upper(), str(chain)
    nested = _json_object(getattr(settings, "CONI_DEPOSIT_RECIPIENTS", "{}"))
    configured = nested.get(asset, {})
    if isinstance(configured, dict) and configured.get(chain):
        return str(configured[chain])
    legacy = _json_object(getattr(settings, "CONI_FUNDING_RECIPIENTS", "{}"))
    configured = legacy.get(asset)
    if isinstance(configured, dict):
        return str(configured.get(chain, ""))
    return str(configured or "")


def native_asset(chain):
    return NATIVE_ASSETS.get(chain, (None, None))[0]
