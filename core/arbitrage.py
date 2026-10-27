"""Quote-aware, paper-first arbitrage decision engine for Coni.

The engine implements the publicly described ZeroGap pattern without copying
private code: detect a cross-chain gap, close it by buying cheap/bridging/selling
dear, then allocate only realized and reconciled profit.  A displayed pool
spread is never treated as executable profit.
"""

import hashlib
import json
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .audit import record_event
from .models import ArbitrageOpportunity
from .pangea import PangeaError, request_json
from .pangea_bridge import quote_bridge


ZERO = Decimal("0")
NATIVE_DECIMALS = {"ethereum": 18, "base": 18, "robinhood": 18, "bsc": 18, "solana": 9}


def dec(value, default=ZERO):
    try:
        if value is None or value == "":
            return default
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _json(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return value if isinstance(value, dict) else {}


def _native_rate(pool, rates):
    quote_symbol = str(pool.get("quoteSymbol") or "").upper()
    rate_map = _json(rates)
    usd_rates = rate_map.get("usd", {}) if isinstance(rate_map.get("usd", {}), dict) else {}
    candidates = [
        pool.get("quotePriceUsd"),
        pool.get("nativePriceUsd"),
        pool.get("quoteUsd"),
        pool.get("nativeUsd"),
        rate_map.get(quote_symbol),
        rate_map.get(quote_symbol.lower()),
        usd_rates.get(quote_symbol),
        usd_rates.get(quote_symbol.lower()),
    ]
    return next((dec(item) for item in candidates if dec(item) > ZERO), ZERO)


def _find_values(value, wanted, found=None):
    if found is None:
        found = {}
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in wanted and item not in (None, ""):
                found.setdefault(str(key).lower(), item)
            _find_values(item, wanted, found)
    elif isinstance(value, list):
        for item in value[:100]:
            _find_values(item, wanted, found)
    return found


def _amount_from_quote(quote, side):
    """Extract an amount from Pangea's routed quote without trusting one venue schema."""
    if not isinstance(quote, dict):
        return ZERO
    # Pangea currently passes Relay/Jupiter details through at this stable path.
    # Keep the generic fallback below because the provider documents this shape
    # as subject to change.
    details = quote.get("details") if isinstance(quote.get("details"), dict) else {}
    for candidate in (
        details.get("currencyOut", {}).get("amount") if isinstance(details.get("currencyOut"), dict) else None,
        details.get("route", {}).get("destination", {}).get("outputCurrency", {}).get("amount") if isinstance(details.get("route"), dict) else None,
        details.get("route", {}).get("origin", {}).get("outputCurrency", {}).get("amount") if isinstance(details.get("route"), dict) else None,
    ):
        amount = dec(candidate)
        if amount > ZERO:
            return amount
    wanted = {"amountout", "amount_out", "outamount", "outputamount", "toamount", "receive", "tokenamount"} if side == "buy" else {"amountout", "amount_out", "outamount", "outputamount", "toamount", "receive", "quoteamount"}
    values = _find_values(quote, wanted)
    for key in ("amountout", "amount_out", "outamount", "outputamount", "toamount", "receive", "tokenamount", "quoteamount"):
        candidate = dec(values.get(key))
        if candidate > ZERO:
            return candidate
    return ZERO


def _route(bridge):
    routes = bridge.get("routes") if isinstance(bridge, dict) else None
    if not isinstance(routes, list):
        routes = list(routes.values()) if isinstance(routes, dict) else []
    for item in routes:
        if isinstance(item, dict) and item.get("id") == "canonical" and not item.get("unavailable"):
            return item
    return None


def _bot_wallet(chain):
    wallets = _json(getattr(settings, "CONI_BOT_WALLETS", "{}"))
    return str(wallets.get(chain) or "")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _pool_price(pool):
    return dec(pool.get("priceUsd"))


def _pool_fee_bps(pool):
    configured = dec(pool.get("feeBps") or pool.get("tradingFeeBps"))
    # Pangea's pools response does not always include the launch's fixed fee.
    # Until an exact fee is supplied, price the worst documented fee rather
    # than silently understating costs.
    return configured if configured > ZERO else dec(getattr(settings, "CONI_DEFAULT_LAUNCH_FEE_BPS", 500))


def evaluate_candidate(*, launch, buy_pool, sell_pool, rates=None, trade_quotes=None, bridge_quote=None, require_quotes=True):
    """Evaluate one buy/bridge/sell route using supplied data; pure and testable."""
    slug = str(launch.get("slug") or "")
    symbol = str(launch.get("symbol") or slug).upper()
    buy_price, sell_price = _pool_price(buy_pool), _pool_price(sell_pool)
    notional = min(dec(getattr(settings, "CONI_ARBITRAGE_NOTIONAL_USD", "10")), dec(getattr(settings, "CONI_ARBITRAGE_MAX_NOTIONAL_USD", "25")))
    reasons = []
    if not slug or buy_price <= ZERO or sell_price <= buy_price:
        reasons.append("no_positive_displayed_gap")
    if buy_pool.get("chain") == sell_pool.get("chain"):
        reasons.append("same_chain_route")
    if not buy_pool.get("token") or not sell_pool.get("token"):
        reasons.append("missing_token_identity")
    if notional <= ZERO:
        reasons.append("invalid_notional")

    displayed_gross = (sell_price / buy_price - 1) * notional if buy_price > ZERO else ZERO
    token_out = ZERO
    sell_proceeds = ZERO
    trade_cost = ZERO
    bridge_cost = ZERO
    bridge_token_fee = ZERO
    bridge_seconds = None
    quote_state = "not-requested"
    if trade_quotes:
        quote_state = "fresh"
        token_out = _amount_from_quote(trade_quotes.get("buy", {}), "buy")
        sell_proceeds_native = _amount_from_quote(trade_quotes.get("sell", {}), "sell")
        sell_proceeds = sell_proceeds_native * _native_rate(sell_pool, rates)
        trade_cost = notional * (_pool_fee_bps(buy_pool) + _pool_fee_bps(sell_pool)) / Decimal("10000")
        if token_out <= ZERO or sell_proceeds <= ZERO:
            reasons.append("quote_missing_executable_amount")
    elif require_quotes:
        reasons.append("fresh_quotes_required")
        quote_state = "missing"

    if bridge_quote is not None:
        route = _route(bridge_quote)
        if not route:
            reasons.append("canonical_bridge_unavailable")
        else:
            arrived = dec(route.get("amountOut"))
            if arrived <= ZERO:
                reasons.append("bridge_amount_missing")
            else:
                token_out = arrived if token_out <= ZERO else min(token_out, arrived)
            bridge_token_fee = dec(route.get("tokenFee")) * sell_price / (Decimal("10") ** 9)
            bridge_cost = dec(route.get("nativeFee")) / (Decimal("10") ** NATIVE_DECIMALS.get(buy_pool.get("chain"), 18)) * _native_rate(buy_pool, rates)
            bridge_seconds = int(dec(route.get("seconds"))) if dec(route.get("seconds")) > ZERO else None
            if bridge_seconds and bridge_seconds > int(getattr(settings, "CONI_ARBITRAGE_MAX_BRIDGE_SECONDS", 120)):
                reasons.append("bridge_too_slow")
    elif buy_pool.get("chain") != sell_pool.get("chain"):
        reasons.append("fresh_bridge_quote_required")

    if require_quotes and (not trade_quotes or bridge_quote is None):
        executable_gross = ZERO
    else:
        executable_gross = sell_proceeds - notional

    risk_reserve = notional * dec(getattr(settings, "CONI_ARBITRAGE_RISK_BUFFER_BPS", 75)) / Decimal("10000")
    total_cost = trade_cost + bridge_cost + bridge_token_fee + risk_reserve
    net_profit = executable_gross - total_cost
    net_bps = int((net_profit / notional * Decimal("10000")).to_integral_value(rounding=ROUND_DOWN)) if notional > ZERO else 0
    min_bps = int(getattr(settings, "CONI_ARBITRAGE_MIN_NET_BPS", 100))
    min_profit = dec(getattr(settings, "CONI_ARBITRAGE_MIN_PROFIT_USD", "0.05"))
    if net_bps < min_bps:
        reasons.append("net_profit_below_policy")
    if net_profit < min_profit:
        reasons.append("net_profit_below_minimum")
    if dec(buy_pool.get("depthUsd") or buy_pool.get("depth")) and dec(buy_pool.get("depthUsd") or buy_pool.get("depth")) < notional:
        reasons.append("buy_pool_depth_below_notional")
    if dec(sell_pool.get("depthUsd") or sell_pool.get("depth")) and dec(sell_pool.get("depthUsd") or sell_pool.get("depth")) < notional:
        reasons.append("sell_pool_depth_below_notional")

    decision = "take" if not reasons else "avoid"
    route = _route(bridge_quote) if bridge_quote else None
    return {
        "slug": slug,
        "name": launch.get("name"),
        "symbol": symbol,
        "image": launch.get("image"),
        "buyChain": buy_pool.get("chain"),
        "sellChain": sell_pool.get("chain"),
        "buyToken": buy_pool.get("token"),
        "sellToken": sell_pool.get("token"),
        "displayedGrossBps": int(((sell_price / buy_price - 1) * Decimal("10000")).to_integral_value(rounding=ROUND_DOWN)) if buy_price > ZERO else 0,
        "grossProfitUsd": str(displayed_gross.quantize(Decimal("0.00000001"))),
        "executableGrossProfitUsd": str(executable_gross.quantize(Decimal("0.00000001"))),
        "tradeCostUsd": str(trade_cost.quantize(Decimal("0.00000001"))),
        "bridgeCostUsd": str((bridge_cost + bridge_token_fee).quantize(Decimal("0.00000001"))),
        "riskReserveUsd": str(risk_reserve.quantize(Decimal("0.00000001"))),
        "totalCostUsd": str(total_cost.quantize(Decimal("0.00000001"))),
        "netProfitUsd": str(net_profit.quantize(Decimal("0.00000001"))),
        "netBps": net_bps,
        "notionalUsd": str(notional),
        "bridgeSeconds": bridge_seconds,
        "bridgeRoute": {"id": route.get("id"), "amountOut": route.get("amountOut"), "nativeFee": route.get("nativeFee"), "tokenFee": route.get("tokenFee"), "setupFee": route.get("setupFee"), "seconds": route.get("seconds"), "callHash": _hash(route.get("call")) if route and route.get("call") else None, "state": "quoted"} if route else None,
        "buyAmountBaseUnits": str((trade_quotes or {}).get("_coniBuyAmountBaseUnits") or "0"),
        "sellAmountBaseUnits": str((trade_quotes or {}).get("_coniSellAmountBaseUnits") or "0"),
        "quoteState": quote_state,
        "tokenOutBaseUnits": str(token_out),
        "decision": decision,
        "executable": decision == "take" and not reasons,
        "reasons": sorted(set(reasons)),
    }


def _quote_route(launch, buy_pool, sell_pool, rates, *, buy_wallet_override=None, sell_wallet_override=None):
    """Request exact Pangea quotes. Any missing bot wallet makes this paper-only."""
    buy_wallet = buy_wallet_override or _bot_wallet(buy_pool["chain"])
    sell_wallet = sell_wallet_override or _bot_wallet(sell_pool["chain"])
    buy_native_rate = _native_rate(buy_pool, rates)
    if not buy_wallet or not sell_wallet or buy_native_rate <= ZERO:
        return None, None
    notional = min(dec(getattr(settings, "CONI_ARBITRAGE_NOTIONAL_USD", "10")), dec(getattr(settings, "CONI_ARBITRAGE_MAX_NOTIONAL_USD", "25")))
    native_units = int((notional / buy_native_rate * (Decimal("10") ** NATIVE_DECIMALS.get(buy_pool["chain"], 18))).to_integral_value(rounding=ROUND_DOWN))
    buy_body = {"slug": launch["slug"], "side": "buy", "chain": buy_pool["chain"], "amount": str(native_units), "user": buy_wallet, "recipient": buy_wallet, "slippageBps": int(getattr(settings, "CONI_ARBITRAGE_SLIPPAGE_BPS", 100))}
    if buy_pool["chain"] == "solana":
        buy_body["solanaUser"] = buy_wallet
        buy_body.pop("user", None)
    buy_quote = request_json("/trade/quote", method="POST", payload=buy_body)
    token_out = _amount_from_quote(buy_quote, "buy")
    if token_out <= ZERO:
        return {"buy": buy_quote}, None
    bridge_result = quote_bridge(slug=launch["slug"], source_chain=buy_pool["chain"], destination_chain=sell_pool["chain"], amount_base_units=int(token_out), source_wallet=buy_wallet, destination_wallet=sell_wallet, solana_wallet=buy_wallet if buy_pool["chain"] == "solana" else sell_wallet)
    bridge_quote = dict(bridge_result["quote"])
    if bridge_result.get("solanaTransaction"):
        bridge_quote["_coniSolanaTransaction"] = bridge_result["solanaTransaction"]
    sell_amount = int(bridge_result["amountOut"])
    sell_body = {"slug": launch["slug"], "side": "sell", "chain": sell_pool["chain"], "amount": str(sell_amount), "user": sell_wallet, "recipient": sell_wallet, "slippageBps": int(getattr(settings, "CONI_ARBITRAGE_SLIPPAGE_BPS", 100))}
    if sell_pool["chain"] == "solana":
        sell_body["solanaUser"] = sell_wallet
        sell_body.pop("user", None)
    sell_quote = request_json("/trade/quote", method="POST", payload=sell_body)
    return {"buy": buy_quote, "sell": sell_quote, "_coniBuyAmountBaseUnits": str(native_units), "_coniSellAmountBaseUnits": str(sell_amount)}, bridge_quote


def fresh_user_quotes(opportunity, *, buy_wallet, sell_wallet):
    """Rebuild a route for one user's active bot-wallet addresses.

    The scanner's persisted opportunity is a discovery snapshot only. This
    function fetches a new pool snapshot and new Pangea trade/bridge quotes so
    the signer never receives a quote created for another wallet or an expired
    market state.
    """
    data = request_json("/pools")
    launch = next((item for item in data.get("launches", []) if item.get("slug") == opportunity.slug), None)
    if not launch:
        raise PangeaError("Pangea launch is no longer available")
    pools = [item for item in launch.get("pools", []) if isinstance(item, dict) and item.get("chain") and item.get("token") and _pool_price(item) > ZERO]

    def matches(pool, chain, token):
        return pool.get("chain") == chain and (not token or str(pool.get("token")).lower() == str(token).lower())

    buy_pool = next((item for item in pools if matches(item, opportunity.buy_chain, opportunity.buy_token)), None)
    sell_pool = next((item for item in pools if matches(item, opportunity.sell_chain, opportunity.sell_token)), None)
    if not buy_pool or not sell_pool:
        raise PangeaError("Pangea source or destination pool is no longer available")
    trade_quotes, bridge_quote = _quote_route(
        launch,
        buy_pool,
        sell_pool,
        data.get("rates", {}),
        buy_wallet_override=buy_wallet,
        sell_wallet_override=sell_wallet,
    )
    row = evaluate_candidate(
        launch=launch,
        buy_pool=buy_pool,
        sell_pool=sell_pool,
        rates=data.get("rates", {}),
        trade_quotes=trade_quotes,
        bridge_quote=bridge_quote,
        require_quotes=True,
    )
    row["quoteHash"] = _hash({"trade": trade_quotes, "bridge": bridge_quote})
    row["quoteArtifact"] = {"trade": trade_quotes, "bridge": bridge_quote}
    if not row["executable"]:
        raise PangeaError("fresh user quote no longer meets the net-profit policy")
    return row


def quote_discovery_opportunity(opportunity, *, data=None):
    """Refresh one queued discovery route with a current Pangea quote.

    Discovery quotes use the configured scanner wallets and are never treated
    as user execution authorization. The execution planner must still call
    ``fresh_user_quotes`` for the user's active bot-wallet addresses before a
    signer handoff.
    """
    data = data or request_json("/pools")
    launch = next((item for item in data.get("launches", []) if item.get("slug") == opportunity.slug), None)
    if not launch:
        raise PangeaError("Pangea launch is no longer available")
    pools = [
        item for item in launch.get("pools", [])
        if isinstance(item, dict)
        and item.get("chain")
        and item.get("token")
        and _pool_price(item) > ZERO
    ]

    def matches(pool, chain, token):
        return pool.get("chain") == chain and (not token or str(pool.get("token")).lower() == str(token).lower())

    buy_pool = next((item for item in pools if matches(item, opportunity.buy_chain, opportunity.buy_token)), None)
    sell_pool = next((item for item in pools if matches(item, opportunity.sell_chain, opportunity.sell_token)), None)
    if not buy_pool or not sell_pool:
        raise PangeaError("Pangea source or destination pool is no longer available")
    trade_quotes, bridge_quote = _quote_route(launch, buy_pool, sell_pool, data.get("rates", {}))
    row = evaluate_candidate(
        launch=launch,
        buy_pool=buy_pool,
        sell_pool=sell_pool,
        rates=data.get("rates", {}),
        trade_quotes=trade_quotes,
        bridge_quote=bridge_quote,
        require_quotes=True,
    )
    row["quoteHash"] = _hash({"trade": trade_quotes, "bridge": bridge_quote}) if trade_quotes or bridge_quote else None
    row["quoteArtifact"] = {"trade": trade_quotes, "bridge": bridge_quote} if trade_quotes or bridge_quote else None
    return row


def candidate_routes(data, *, max_candidates=None):
    """Enumerate every profitable cross-chain pool direction in the feed."""
    candidates = []
    for launch in data.get("launches", []):
        pools = [
            pool for pool in launch.get("pools", [])
            if isinstance(pool, dict)
            and _pool_price(pool) > ZERO
            and not pool.get("error")
            and pool.get("token")
        ]
        for buy in pools:
            for sell in pools:
                if buy is sell or buy.get("chain") == sell.get("chain") or _pool_price(sell) <= _pool_price(buy):
                    continue
                candidates.append((launch, buy, sell))
    candidates.sort(key=lambda item: (_pool_price(item[2]) / _pool_price(item[1]) - 1), reverse=True)
    if max_candidates is None:
        configured_cap = int(getattr(settings, "CONI_ARBITRAGE_MAX_CANDIDATES", 0))
        max_candidates = configured_cap if configured_cap > 0 else None
    return candidates if max_candidates is None else candidates[:max_candidates]


def scan_opportunities(*, persist=True):
    data = request_json("/pools")
    candidates = candidate_routes(data)
    result = []
    for position, (launch, buy, sell) in enumerate(candidates):
        trade_quotes = bridge_quote = None
        if getattr(settings, "CONI_ARBITRAGE_REQUIRE_QUOTES", True) and position < int(getattr(settings, "CONI_ARBITRAGE_MAX_QUOTE_REQUESTS", 20)):
            try:
                trade_quotes, bridge_quote = _quote_route(launch, buy, sell, data.get("rates", {}))
            except PangeaError:
                trade_quotes, bridge_quote = None, None
        row = evaluate_candidate(launch=launch, buy_pool=buy, sell_pool=sell, rates=data.get("rates", {}), trade_quotes=trade_quotes, bridge_quote=bridge_quote, require_quotes=getattr(settings, "CONI_ARBITRAGE_REQUIRE_QUOTES", True))
        row["quoteHash"] = _hash({"trade": trade_quotes, "bridge": bridge_quote}) if trade_quotes or bridge_quote else None
        if persist:
            dedupe = f"{row['slug']}:{row['buyChain']}:{row['sellChain']}:{row['quoteHash'] or data.get('readAt') or 'snapshot'}"
            ArbitrageOpportunity.objects.update_or_create(
                dedupe_key=dedupe,
                defaults={
                    "slug": row["slug"], "symbol": row["symbol"], "buy_chain": row["buyChain"], "sell_chain": row["sellChain"],
                    "buy_token": row["buyToken"], "sell_token": row["sellToken"], "notional_usd": dec(row["notionalUsd"]),
                    "gross_profit_usd": dec(row["executableGrossProfitUsd"]), "total_cost_usd": dec(row["totalCostUsd"]),
                    "net_profit_usd": dec(row["netProfitUsd"]), "net_bps": row["netBps"], "state": "approved-paper" if row["executable"] else "rejected",
                    "quote_hash": row["quoteHash"], "decision": row,
                    "expires_at": timezone.now() + timedelta(seconds=60),
                },
            )
        result.append(row)
    result.sort(key=lambda item: item["netBps"], reverse=True)
    if persist:
        record_event("arbitrage.scan", result="healthy", metadata={"count": len(result), "executable": sum(1 for item in result if item["executable"])})
    return result[:100]
