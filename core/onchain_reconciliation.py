"""Read-only reconciliation between bot-wallet ledgers and chain balances.

This module is deliberately observational.  It never credits a ledger, marks a
withdrawal complete, or treats a provider response as signer authorization.
It compares every active attested bot wallet's configured assets with a
finalized/latest RPC balance and returns bounded anomaly records for operators.
"""

from __future__ import annotations

import json
from decimal import Decimal

from django.conf import settings

from .assets import asset_decimals, native_asset, token_config, token_registry
from .bot_wallets import BOT_WALLET_CHAINS
from .funding import ledger_balance
from .models import BotWallet, LedgerAccount
from .rpc import RpcError, result as rpc_result


EVM_CHAINS = {"ethereum", "base", "robinhood", "bsc"}
BALANCE_OF_SELECTOR = "70a08231"


def _expected_chain_ids():
    try:
        value = json.loads(getattr(settings, "CONI_EXPECTED_CHAIN_IDS", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _hex_quantity(value):
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("RPC returned an invalid hexadecimal quantity")
    return int(value, 16)


def _evm_balance(chain, address, asset):
    if asset == native_asset(chain):
        return _hex_quantity(rpc_result(chain, "eth_getBalance", [address, "latest"]))
    config = token_config(asset, chain) or {}
    token_address = str(config.get("address", ""))
    if not token_address.startswith("0x") or len(token_address) != 42:
        raise ValueError("token contract is not configured")
    data = "0x" + BALANCE_OF_SELECTOR + address[2:].lower().rjust(64, "0")
    return _hex_quantity(rpc_result(chain, "eth_call", [{"to": token_address, "data": data}, "latest"]))


def _solana_balance(address, asset):
    if asset == native_asset("solana"):
        response = rpc_result("solana", "getBalance", [address, {"commitment": "finalized"}]) or {}
        return int(response.get("value", 0)) if isinstance(response, dict) else 0
    config = token_config(asset, "solana") or {}
    mint = str(config.get("address", ""))
    if not mint:
        raise ValueError("Solana mint is not configured")
    response = rpc_result(
        "solana",
        "getTokenAccountsByOwner",
        [address, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "finalized"}],
    ) or {}
    total = 0
    for row in (response.get("value", []) if isinstance(response, dict) else []):
        parsed = (((row or {}).get("account") or {}).get("data") or {}).get("parsed") or {}
        info = parsed.get("info") or {}
        token_amount = info.get("tokenAmount") or {}
        try:
            total += int(token_amount.get("amount", 0))
        except (TypeError, ValueError):
            raise ValueError("Solana token account returned an invalid amount")
    return total


def _chain_identity_ok(chain, expected_ids, cache):
    if chain not in EVM_CHAINS:
        return True
    if chain not in cache:
        cache[chain] = str(rpc_result(chain, "eth_chainId", [])).lower()
    expected = str(expected_ids.get(chain, "")).lower()
    return bool(expected) and cache[chain] == expected


def _assets_for_chain(chain):
    assets = []
    native = native_asset(chain)
    if native:
        assets.append(native)
    for asset in token_registry():
        if token_config(asset, chain) and asset_decimals(asset, chain) is not None:
            assets.append(str(asset).upper())
    return list(dict.fromkeys(assets))


def reconcile_bot_wallets(*, wallets=None):
    """Return bounded chain-vs-ledger checks for active bot wallets."""
    expected_ids = _expected_chain_ids()
    chain_cache = {}
    checks = []
    anomalies = []
    queryset = wallets if wallets is not None else BotWallet.objects.filter(
        status="active", custody="audited-signer", signer_key_ref__gt="",
    ).exclude(address="")
    for wallet in queryset:
        chain = str(wallet.chain)
        address = str(wallet.address)
        if chain not in BOT_WALLET_CHAINS:
            continue
        try:
            if not _chain_identity_ok(chain, expected_ids, chain_cache):
                raise ValueError("RPC chain identity does not match configuration")
            for asset in _assets_for_chain(chain):
                observed = _solana_balance(address, asset) if chain == "solana" else _evm_balance(chain, address, asset)
                available = LedgerAccount.objects.filter(
                    owner=wallet.owner, bot_wallet=wallet, asset=asset,
                    chain=chain, kind="bot-available",
                ).first()
                reserved = LedgerAccount.objects.filter(
                    owner=wallet.owner, bot_wallet=wallet, asset=asset,
                    chain=chain, kind="bot-withdrawal-reserved",
                ).first()
                expected = (ledger_balance(available) if available else Decimal("0")) + (ledger_balance(reserved) if reserved else Decimal("0"))
                check = {
                    "walletId": str(wallet.id),
                    "chain": chain,
                    "address": address,
                    "asset": asset,
                    "observedBaseUnits": str(observed),
                    "ledgerBaseUnits": str(expected),
                    "status": "matched" if Decimal(observed) == Decimal(expected) else "mismatch",
                }
                checks.append(check)
                if check["status"] != "matched":
                    anomalies.append({"type": "bot-wallet-chain-ledger-mismatch", **check})
        except (RpcError, ValueError, TypeError) as exc:
            anomaly = {
                "type": "bot-wallet-chain-balance-unavailable",
                "walletId": str(wallet.id),
                "chain": chain,
                "address": address,
                "error": str(exc)[:160],
            }
            anomalies.append(anomaly)
            checks.append({**anomaly, "status": "unavailable"})
    return {"checks": checks, "anomalies": anomalies, "checked": len(checks)}
