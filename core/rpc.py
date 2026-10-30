"""Private, allowlisted JSON-RPC forwarding for Coni wallets and workers.

This module is deliberately a relay, not a signer.  It keeps node URLs and
provider credentials server-side, forwards only explicitly supported methods,
and accepts transaction payloads only after they have already been signed by
the user's wallet or by the separately controlled signer service.
"""

import base64
import json
import re
from urllib.parse import urlparse

import requests
from django.conf import settings


class RpcError(Exception):
    """A provider or validation error that is safe to expose to callers."""


EVM_METHODS = frozenset({
    "eth_chainId",
    "eth_blockNumber",
    "eth_getBalance",
    "eth_getTransactionCount",
    "eth_getTransactionByHash",
    "eth_getTransactionReceipt",
    "eth_getCode",
    "eth_getBlockByNumber",
    "eth_getBlockByHash",
    "eth_getLogs",
    "eth_call",
    "eth_estimateGas",
    "eth_gasPrice",
    "eth_maxPriorityFeePerGas",
    "eth_feeHistory",
    "net_version",
    "web3_clientVersion",
    "eth_sendRawTransaction",
})

SOLANA_METHODS = frozenset({
    "getAccountInfo",
    "getBalance",
    "getBlockHeight",
    "getEpochInfo",
    "getFeeForMessage",
    "getLatestBlockhash",
    "getMinimumBalanceForRentExemption",
    "getSignatureStatuses",
    "getSlot",
    "getTokenAccountBalance",
    "getTokenAccountsByOwner",
    "getTokenSupply",
    "getTransaction",
    "getVersion",
    "getProgramAccounts",
    "simulateTransaction",
    "sendTransaction",
})

_EVM_RAW_TX_RE = re.compile(r"^0x[0-9a-fA-F]+$")
_SOLANA_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_MAX_PARAMS_BYTES = 256 * 1024
_MAX_RAW_TX_BYTES = 192 * 1024


def _url(value):
    parsed = urlparse(str(value).strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return parsed.geturl()


def rpc_endpoints(chain):
    """Return configured failover endpoints without exposing them to clients."""
    try:
        configured = json.loads(getattr(settings, "CONI_RPC_URLS", "{}") or "{}")
    except json.JSONDecodeError as exc:
        raise RpcError("CONI_RPC_URLS is not valid JSON") from exc
    if not isinstance(configured, dict):
        raise RpcError("CONI_RPC_URLS must be a JSON object")

    values = configured.get(chain, [])
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise RpcError(f"RPC configuration for {chain} must be a string or list")

    if chain == "solana":
        solana_url = getattr(settings, "CONI_SOLANA_RPC_URL", "")
        values = ([solana_url] if solana_url else []) + values

    endpoints = []
    for value in values:
        parsed = _url(value)
        if parsed and parsed not in endpoints:
            endpoints.append(parsed)
    if not endpoints:
        raise RpcError(f"no RPC configured for {chain}")
    return endpoints


def allowed_methods(chain):
    if chain == "solana":
        return SOLANA_METHODS
    return EVM_METHODS


def _validate_raw_transaction(chain, method, params):
    if method == "eth_sendRawTransaction":
        if chain == "solana" or len(params) != 1 or not isinstance(params[0], str):
            raise RpcError("eth_sendRawTransaction requires one signed EVM transaction")
        raw = params[0]
        if not _EVM_RAW_TX_RE.fullmatch(raw) or len(raw) < 10 or len(raw) % 2 or len(raw) > _MAX_RAW_TX_BYTES * 2 + 2:
            raise RpcError("invalid signed EVM transaction")
        return
    if method == "sendTransaction":
        if chain != "solana" or not params or not isinstance(params[0], str):
            raise RpcError("sendTransaction requires one signed Solana transaction")
        raw = params[0]
        if len(raw) > _MAX_RAW_TX_BYTES * 2 or not _SOLANA_BASE64_RE.fullmatch(raw):
            raise RpcError("invalid signed Solana transaction")
        try:
            base64.b64decode(raw, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise RpcError("invalid signed Solana transaction encoding") from exc
        if len(params) > 1 and not isinstance(params[1], dict):
            raise RpcError("invalid Solana send options")


def validate_call(chain, method, params):
    if chain not in {"ethereum", "base", "robinhood", "bsc", "solana"}:
        raise RpcError("unsupported RPC chain")
    if not isinstance(method, str) or method not in allowed_methods(chain):
        raise RpcError("RPC method is not enabled")
    if not isinstance(params, list):
        raise RpcError("RPC params must be an array")
    try:
        if len(json.dumps(params, separators=(",", ":"))) > _MAX_PARAMS_BYTES:
            raise RpcError("RPC params are too large")
    except (TypeError, ValueError) as exc:
        raise RpcError("RPC params are not valid JSON") from exc
    _validate_raw_transaction(chain, method, params)


def _request(endpoint, method, params, request_id=1):
    try:
        response = requests.post(
            endpoint,
            json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            timeout=(3.05, 20),
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise RpcError("chain RPC provider unavailable") from exc
    if not isinstance(payload, dict):
        raise RpcError("chain RPC provider returned invalid JSON")
    return payload


def forward(chain, method, params, request_id=1):
    """Forward one validated call, failing over only on transport failures."""
    validate_call(chain, method, params)
    last_error = None
    for endpoint in rpc_endpoints(chain):
        try:
            payload = _request(endpoint, method, params, request_id)
            if payload.get("error"):
                # An upstream method error is deterministic; retrying it against
                # another node can hide malformed client input.
                return {"jsonrpc": "2.0", "id": payload.get("id", request_id), "error": {"code": -32000, "message": "RPC provider rejected the request"}}
            return {"jsonrpc": "2.0", "id": payload.get("id", request_id), "result": payload.get("result")}
        except RpcError as exc:
            last_error = exc
    raise last_error or RpcError("chain RPC provider unavailable")


def result(chain, method, params):
    payload = forward(chain, method, params)
    if payload.get("error"):
        raise RpcError("chain RPC provider returned an error")
    return payload.get("result")


def configured_health():
    health = {}
    for chain in ("ethereum", "base", "robinhood", "bsc", "solana"):
        try:
            endpoints = rpc_endpoints(chain)
            health[chain] = {"configured": True, "endpointCount": len(endpoints)}
        except RpcError:
            health[chain] = {"configured": False, "endpointCount": 0}
    return health
