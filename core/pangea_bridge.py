"""Pangea LayerZero bridge quote boundary.

This module only prices and validates the unsigned bridge step. It never signs,
broadcasts, or treats a source-chain send as a completed destination transfer.
"""

import hashlib
import json

from .pangea import PangeaError, request_json


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def canonical_route(quote):
    routes = quote.get("routes") if isinstance(quote, dict) else None
    if isinstance(routes, dict):
        routes = list(routes.values())
    if not isinstance(routes, list):
        return None
    for route in routes:
        if isinstance(route, dict) and route.get("id") == "canonical" and not route.get("unavailable"):
            return route
    return None


def quote_bridge(*, slug, source_chain, destination_chain, amount_base_units, source_wallet, destination_wallet, solana_wallet=None):
    if source_chain == destination_chain:
        raise PangeaError("bridge source and destination must differ")
    payload = {
        "slug": slug,
        "from": source_chain,
        "to": destination_chain,
        "amount": str(amount_base_units),
        "user": destination_wallet if source_chain == "solana" else source_wallet,
        "recipient": destination_wallet,
    }
    if source_chain == "solana":
        payload["solanaUser"] = source_wallet
    elif destination_chain == "solana" and solana_wallet:
        payload["solanaUser"] = solana_wallet
    quote = request_json("/bridge/quote", method="POST", payload=payload)
    route = canonical_route(quote)
    if not route:
        raise PangeaError("Pangea canonical bridge route is unavailable")
    if not str(route.get("amountOut") or "").isdigit() or int(route["amountOut"]) <= 0:
        raise PangeaError("Pangea bridge quote has no positive destination amount")
    call = route.get("call")
    result = {
        "quote": quote,
        "route": route,
        "callHash": _hash(call) if call else None,
        "unsignedCallPresent": bool(call),
        "source": source_chain,
        "destination": destination_chain,
        "amountIn": str(amount_base_units),
        "amountOut": str(route["amountOut"]),
        "state": "quoted",
    }
    if source_chain == "solana":
        built = request_json("/bridge/solana-tx", method="POST", payload={"slug": slug, "to": destination_chain, "amount": str(amount_base_units), "sender": source_wallet, "recipient": destination_wallet})
        if built.get("stage") not in {"send", "together", "setup", "waiting"} or not built.get("transaction") and built.get("stage") not in {"waiting", "setup"}:
            raise PangeaError("Pangea did not return an unsigned Solana bridge transaction")
        result["solanaTransaction"] = {"stage": built.get("stage"), "transaction": built.get("transaction"), "blockhash": built.get("blockhash"), "lastValidBlockHeight": built.get("lastValidBlockHeight"), "totalFee": built.get("totalFee"), "received": built.get("received")}
        result["unsignedCallPresent"] = bool(built.get("transaction"))
    return result


BRIDGE_STATES = ("quoted", "source-submitted", "source-confirmed", "in-flight", "destination-confirmed", "settled", "failed", "refunded")


def next_bridge_state(current, event):
    transitions = {
        "quoted": {"source-submitted": "source-submitted", "failed": "failed"},
        "source-submitted": {"source-confirmed": "source-confirmed", "failed": "failed"},
        "source-confirmed": {"in-flight": "in-flight", "failed": "failed"},
        "in-flight": {"destination-confirmed": "destination-confirmed", "refunded": "refunded", "failed": "failed"},
        "destination-confirmed": {"settled": "settled", "failed": "failed"},
    }
    try:
        return transitions[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid bridge transition {current}->{event}") from exc
