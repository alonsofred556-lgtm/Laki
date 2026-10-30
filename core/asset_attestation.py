"""Read-only attestation of configured token addresses and decimal scales.

The browser and Pangea metadata are never authoritative for funding units.
This module reads the configured private RPCs, verifies the expected chain
identity, and compares on-chain token/mint decimals with the server registry.
It intentionally does not mutate configuration or enable funding.
"""

from __future__ import annotations

import json
import re

from django.conf import settings

from .assets import token_registry
from .rpc import RpcError, result as rpc_result


EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
SOLANA_ADDRESS_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
DECIMALS_SELECTOR = "0x313ce567"
EVM_CHAINS = {"ethereum", "base", "robinhood", "bsc"}


def _expected_chain_ids():
    try:
        value = json.loads(getattr(settings, "CONI_EXPECTED_CHAIN_IDS", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _hex_quantity(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", value):
        raise ValueError("RPC returned an invalid hexadecimal quantity")
    return int(value, 16)


def _configured_decimals(config):
    value = config.get("decimals") if isinstance(config, dict) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 36 else None


def _evm_attestation(chain, config, expected_chain_ids, chain_cache):
    address = str(config.get("address", "")) if isinstance(config, dict) else ""
    configured = _configured_decimals(config)
    if not EVM_ADDRESS_RE.fullmatch(address):
        return {"chain": chain, "address": address or None, "status": "invalid-address", "configuredDecimals": configured}
    try:
        observed_chain = chain_cache.get(chain)
        if observed_chain is None:
            observed_chain = rpc_result(chain, "eth_chainId", [])
            chain_cache[chain] = observed_chain
        observed_chain = str(observed_chain).lower()
        expected_chain = str(expected_chain_ids.get(chain, "")).lower()
        if not expected_chain or observed_chain != expected_chain:
            return {"chain": chain, "address": address, "status": "chain-identity-mismatch", "expectedChainId": expected_chain or None, "observedChainId": observed_chain}
        if configured is None:
            return {"chain": chain, "address": address, "status": "missing-configured-decimals", "observedChainId": observed_chain}
        raw = rpc_result(chain, "eth_call", [{"to": address, "data": DECIMALS_SELECTOR}, "latest"])
        observed = _hex_quantity(raw)
        if observed > 36:
            return {"chain": chain, "address": address, "status": "invalid-onchain-decimals", "configuredDecimals": configured, "observedDecimals": observed}
        status = "verified" if observed == configured else "decimal-mismatch"
        return {"chain": chain, "address": address, "status": status, "configuredDecimals": configured, "observedDecimals": observed, "observedChainId": observed_chain}
    except (RpcError, ValueError, TypeError) as exc:
        return {"chain": chain, "address": address, "status": "provider-unavailable", "error": str(exc)[:120]}


def _solana_attestation(chain, config):
    address = str(config.get("address", "")) if isinstance(config, dict) else ""
    configured = _configured_decimals(config)
    if not SOLANA_ADDRESS_RE.fullmatch(address):
        return {"chain": chain, "address": address or None, "status": "invalid-address", "configuredDecimals": configured}
    if configured is None:
        return {"chain": chain, "address": address, "status": "missing-configured-decimals"}
    try:
        response = rpc_result("solana", "getAccountInfo", [address, {"encoding": "jsonParsed", "commitment": "finalized"}]) or {}
        value = response.get("value") if isinstance(response, dict) else None
        parsed = ((value or {}).get("data") or {}).get("parsed") or {}
        info = parsed.get("info") or {}
        observed = info.get("decimals")
        if not isinstance(observed, int) or isinstance(observed, bool) or not 0 <= observed <= 36:
            raise ValueError("Solana account did not return a valid mint decimal scale")
        status = "verified" if observed == configured else "decimal-mismatch"
        return {"chain": chain, "address": address, "status": status, "configuredDecimals": configured, "observedDecimals": observed}
    except (RpcError, ValueError, TypeError) as exc:
        return {"chain": chain, "address": address, "status": "provider-unavailable", "error": str(exc)[:120]}


def verify_asset_registry():
    """Return a machine-readable, non-mutating registry attestation report."""
    expected_chain_ids = _expected_chain_ids()
    chain_cache = {}
    assets = []
    for asset, chains in token_registry().items():
        if not isinstance(chains, dict):
            assets.append({"asset": str(asset).upper(), "status": "invalid-asset-config"})
            continue
        for chain, config in chains.items():
            if chain == "solana":
                item = _solana_attestation(chain, config if isinstance(config, dict) else {"address": config})
            elif chain in EVM_CHAINS:
                item = _evm_attestation(chain, config if isinstance(config, dict) else {"address": config}, expected_chain_ids, chain_cache)
            else:
                item = {"chain": chain, "status": "unsupported-chain"}
            assets.append({"asset": str(asset).upper(), **item})
    return {"ready": bool(assets) and all(item.get("status") == "verified" for item in assets), "assets": assets, "expectedChainIds": expected_chain_ids}
