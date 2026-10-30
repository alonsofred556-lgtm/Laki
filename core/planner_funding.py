"""Conservative native-balance requirements for signer-bound route planning."""

from __future__ import annotations

import json

from django.conf import settings


def native_reserve_base_units(chain):
    """Return the operator-supplied native reserve for one chain.

    The reserve is deliberately required rather than inferred. It must cover
    gas and any expected bridge-native fee for one active route, in the chain's
    native base units. A missing or invalid reserve disables planning.
    """

    try:
        configured = json.loads(getattr(settings, "CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS", "{}") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(configured, dict):
        return None
    value = configured.get(str(chain))
    if isinstance(value, bool) or not str(value or "").isdigit():
        return None
    amount = int(value)
    return amount if amount > 0 else None


def _positive_base_units(value, field):
    if isinstance(value, bool) or not str(value or "").isdigit():
        raise ValueError(f"{field} is missing or invalid")
    amount = int(value)
    if amount <= 0:
        raise ValueError(f"{field} must be positive")
    return amount


def required_native_units(decision, chain, *, role):
    """Return the minimum native balance needed before a route can be planned."""

    reserve = native_reserve_base_units(chain)
    if reserve is None:
        return None
    if role == "sell":
        return reserve
    if role != "buy":
        raise ValueError("funding role is invalid")
    required = reserve + _positive_base_units(decision.get("buyAmountBaseUnits"), "buyAmountBaseUnits")
    route = decision.get("bridgeRoute")
    if not isinstance(route, dict):
        raise ValueError("bridge route is missing")
    for field in ("nativeFee", "setupFee"):
        value = route.get(field, "0")
        if isinstance(value, bool) or not str(value or "0").isdigit():
            raise ValueError(f"{field} is invalid")
        required += int(value)
    return required


def reserves_configured(chains):
    """Return whether every requested chain has an explicit positive reserve."""

    return all(native_reserve_base_units(chain) is not None for chain in chains)
