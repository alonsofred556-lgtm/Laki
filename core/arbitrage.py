"""Quote-aware, paper-first arbitrage decision engine for Coni.

The engine implements the publicly described ZeroGap pattern without copying
private code: detect a cross-chain gap, close it by buying cheap/bridging/selling
dear, then allocate only realized and reconciled profit.  A displayed pool
spread is never treated as executable profit.
"""

import hashlib
import json
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.utils import timezone
from datetime import timedelta

from .audit import record_event
from .models import ArbitrageOpportunity
from .pangea import PangeaError, market_snapshot, request_json
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


def _market_cap_usd(launch, *pools):
    """Read Pangea's capSupply or an explicit USD market-cap field.

    Pangea's `/pools` contract publishes `capSupply` as the authoritative
    supply used for market cap, while the live pool price is chain-specific.
    Use the lowest available route price for a conservative risk comparison;
    never infer supply from reserves or a frontend default.
    """
    sources = (launch,) + pools
    keys = ("marketCapUsd", "market_cap_usd", "marketCap", "market_cap")
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in keys:
            value = dec(source.get(key))
            if value > ZERO:
                return value
    cap_supply = dec(launch.get("capSupply")) if isinstance(launch, dict) else ZERO
    if cap_supply <= ZERO and isinstance(launch, dict) and isinstance(launch.get("supply"), dict):
        cap_supply = dec(launch["supply"].get("capSupply") or launch["supply"].get("total"))
    prices = [_pool_price(pool) for pool in pools if isinstance(pool, dict) and _pool_price(pool) > ZERO]
    if cap_supply > ZERO and prices:
        return cap_supply * min(prices)
    return ZERO


def _relative_move_bps(current, baseline):
    current, baseline = dec(current), dec(baseline)
    if current <= ZERO or baseline <= ZERO:
        return None
    return int((abs(current - baseline) / baseline * Decimal("10000")).to_integral_value(rounding=ROUND_DOWN))


def _apply_state_drift_guard(row, *, launch, buy_pool, sell_pool, baseline):
    """Reject a quote if the market moved materially since discovery.

    A discovery opportunity is only a hint. This guard compares the exact
    route prices and explicit market-cap data captured during discovery with
    the fresh snapshot used to build the user's quote.
    """
    if not isinstance(baseline, dict):
        return row

    reasons = list(row.get("reasons") or [])
    price_moves = {}
    for side, pool, key in (("buy", buy_pool, "buyPriceUsd"), ("sell", sell_pool, "sellPriceUsd")):
        current = _pool_price(pool)
        previous = dec(baseline.get(key))
        move = _relative_move_bps(current, previous)
        price_moves[side] = move
        if move is not None and move > int(getattr(settings, "CONI_ARBITRAGE_MAX_PRICE_MOVE_BPS", 200)):
            reasons.append(f"{side}_price_moved_too_far")

    current_market_cap = _market_cap_usd(launch, buy_pool, sell_pool)
    previous_market_cap = dec(baseline.get("marketCapUsd"))
    market_cap_move = _relative_move_bps(current_market_cap, previous_market_cap)
    if previous_market_cap > ZERO and current_market_cap <= ZERO:
        reasons.append("market_cap_missing_at_execution")
    elif market_cap_move is not None and market_cap_move > int(getattr(settings, "CONI_ARBITRAGE_MAX_MARKET_CAP_MOVE_BPS", 2000)):
        reasons.append("market_cap_moved_too_far")
    elif getattr(settings, "CONI_ARBITRAGE_REQUIRE_MARKET_CAP_DATA", False) and (previous_market_cap <= ZERO or current_market_cap <= ZERO):
        reasons.append("market_cap_evidence_missing")

    row["buyPriceUsd"] = str(_pool_price(buy_pool))
    row["sellPriceUsd"] = str(_pool_price(sell_pool))
    row["marketCapUsd"] = str(current_market_cap) if current_market_cap > ZERO else None
    row["stateDrift"] = {"buyPriceMoveBps": price_moves["buy"], "sellPriceMoveBps": price_moves["sell"], "marketCapMoveBps": market_cap_move}
    if reasons != list(row.get("reasons") or []):
        row["reasons"] = sorted(set(reasons))
        row["decision"] = "avoid"
        row["executable"] = False
        row["confidenceBps"] = 0
        row["confidenceLabel"] = "insufficient"
        row["analysisVersion"] = "v3-state-revalidation"
    return row


def _pool_fee_bps(pool):
    configured = dec(pool.get("feeBps") or pool.get("tradingFeeBps"))
    # Pangea's pools response does not always include the launch's fixed fee.
    # Until an exact fee is supplied, price the worst documented fee rather
    # than silently understating costs.
    return configured if configured > ZERO else dec(getattr(settings, "CONI_DEFAULT_LAUNCH_FEE_BPS", 500))


def _token_decimals(pool):
    """Return the token's base-unit scale from Pangea, with chain fallback.

    Bridge token fees are returned in base units.  A token-specific value is
    preferred; Pangea's launch API uses nine decimals for launched tokens when
    the pool feed omits the token scale.
    """
    for key in ("tokenDecimals", "decimals", "baseDecimals", "assetDecimals"):
        value = pool.get(key)
        if value not in (None, ""):
            try:
                parsed = int(value)
                if 0 <= parsed <= 36:
                    return parsed
            except (TypeError, ValueError):
                pass
    return 9


def _native_output_usd(quote, chain, rates, pool):
    """Convert a native-asset quote output from base units to USD."""
    base_units = _amount_from_quote(quote, "sell")
    if base_units <= ZERO:
        return ZERO
    native_amount = base_units / (Decimal("10") ** NATIVE_DECIMALS.get(chain, 18))
    return native_amount * _native_rate(pool, rates)


def _quote_age_seconds(quote, now=None):
    """Read Coni's local quote timestamp and return its age in seconds."""
    if not isinstance(quote, dict) or quote.get("_coniQuotedAt") in (None, ""):
        return None
    stamp = dec(quote.get("_coniQuotedAt"))
    if stamp > Decimal("100000000000"):
        stamp /= Decimal("1000")
    if stamp <= ZERO:
        return None
    age = dec(now if now is not None else time.time()) - stamp
    return age


def _quote_has_error(quote):
    return isinstance(quote, dict) and (quote.get("error") or quote.get("success") is False or quote.get("status") == "error")


def _quote_impact_bps(quote):
    if not isinstance(quote, dict):
        return ZERO
    values = _find_values(quote, {"priceimpactbps", "impactbps"})
    for key in ("priceimpactbps", "impactbps"):
        value = dec(values.get(key))
        if value >= ZERO:
            return value
    values = _find_values(quote, {"priceimpact", "priceimpactpct"})
    for key in ("priceimpact", "priceimpactpct"):
        value = dec(values.get(key))
        if value >= ZERO:
            return value * (Decimal("100") if value <= Decimal("100") else Decimal("1"))
    return ZERO


def _pool_depth(pool):
    for key in ("depthUsd", "liquidityUsd", "tvlUsd", "depth", "liquidity"):
        value = dec(pool.get(key))
        if value > ZERO:
            return value
    return ZERO


def _explicit_quote_fee_usd(quote):
    """Read only explicitly named USD fee fields; never infer from arbitrary amounts."""
    if not isinstance(quote, dict):
        return ZERO
    direct_keys = ("totalFeeUsd", "totalFeesUsd", "networkFeeUsd", "gasFeeUsd", "routeFeeUsd")
    for key in direct_keys:
        value = dec(quote.get(key))
        if value >= ZERO and quote.get(key) not in (None, ""):
            return value
    details = quote.get("details") if isinstance(quote.get("details"), dict) else {}
    for key in direct_keys:
        value = dec(details.get(key))
        if value >= ZERO and details.get(key) not in (None, ""):
            return value
    return ZERO


def _confidence(*, buy_pool, sell_pool, trade_quotes, bridge_quote, notional, net_bps, bridge_seconds, token_out_before_bridge, arrived_amount, reasons):
    """Score execution evidence, not a guaranteed probability of profit."""
    score = 10_000
    confidence_reasons = []
    max_age = int(getattr(settings, "CONI_ARBITRAGE_MAX_QUOTE_AGE_SECONDS", 10))
    ages = [_quote_age_seconds(trade_quotes), _quote_age_seconds(bridge_quote)]
    if any(age is None for age in ages):
        score = 0
        confidence_reasons.append("quote_freshness_unknown")
    else:
        for age in ages:
            if age < Decimal("-2"):
                score = 0
                confidence_reasons.append("quote_clock_skew")
            elif age > max_age:
                score = 0
                confidence_reasons.append("quote_too_old")
            else:
                score -= int((age / Decimal(max_age or 1) * Decimal("500")).to_integral_value(rounding=ROUND_DOWN))
    if _quote_has_error(trade_quotes.get("buy") if isinstance(trade_quotes, dict) else None) or _quote_has_error(trade_quotes.get("sell") if isinstance(trade_quotes, dict) else None):
        score = 0
        confidence_reasons.append("trade_quote_error")
    buy_depth, sell_depth = _pool_depth(buy_pool), _pool_depth(sell_pool)
    min_multiple = dec(getattr(settings, "CONI_ARBITRAGE_MIN_DEPTH_MULTIPLE", 3))
    if buy_depth <= ZERO or sell_depth <= ZERO:
        score = 0
        confidence_reasons.append("pool_depth_unknown")
    else:
        for depth in (buy_depth, sell_depth):
            multiple = depth / notional if notional > ZERO else ZERO
            if multiple < min_multiple:
                score = min(score, int((multiple / min_multiple * Decimal("6000")).to_integral_value(rounding=ROUND_DOWN)))
                confidence_reasons.append("pool_depth_thin")
            else:
                score -= min(500, int((Decimal("500") / multiple).to_integral_value(rounding=ROUND_DOWN)))
    max_impact = dec(getattr(settings, "CONI_ARBITRAGE_MAX_PRICE_IMPACT_BPS", 100))
    for quote in ((trade_quotes or {}).get("buy"), (trade_quotes or {}).get("sell")):
        impact = _quote_impact_bps(quote)
        if impact > max_impact:
            score = 0
            confidence_reasons.append("price_impact_above_policy")
    if token_out_before_bridge <= ZERO or arrived_amount <= ZERO:
        score = 0
        confidence_reasons.append("bridge_retention_unknown")
    else:
        retention_bps = arrived_amount / token_out_before_bridge * Decimal("10000")
        min_retention = dec(getattr(settings, "CONI_ARBITRAGE_MIN_BRIDGE_RETENTION_BPS", 9900))
        if retention_bps < min_retention:
            score = 0
            confidence_reasons.append("bridge_retention_below_policy")
    if bridge_seconds is None:
        score = 0
        confidence_reasons.append("bridge_latency_unknown")
    elif bridge_seconds > int(getattr(settings, "CONI_ARBITRAGE_MAX_BRIDGE_SECONDS", 120)):
        score = 0
        confidence_reasons.append("bridge_too_slow")
    min_bps = int(getattr(settings, "CONI_ARBITRAGE_MIN_NET_BPS", 100))
    if net_bps < min_bps * 2:
        score = min(score, 7000)
        confidence_reasons.append("profit_buffer_is_thin")
    return max(0, min(10_000, score)), confidence_reasons


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
    token_out_before_bridge = ZERO
    arrived_amount = ZERO
    quote_state = "not-requested"
    if trade_quotes:
        quote_state = "fresh"
        token_out = _amount_from_quote(trade_quotes.get("buy", {}), "buy")
        token_out_before_bridge = token_out
        sell_proceeds = _native_output_usd(trade_quotes.get("sell", {}), sell_pool.get("chain"), rates, sell_pool)
        trade_cost = notional * (_pool_fee_bps(buy_pool) + _pool_fee_bps(sell_pool)) / Decimal("10000")
        trade_cost += _explicit_quote_fee_usd(trade_quotes.get("buy", {})) + _explicit_quote_fee_usd(trade_quotes.get("sell", {}))
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
                arrived_amount = arrived
            token_fee_usd = dec(route.get("tokenFeeUsd"))
            bridge_token_fee = token_fee_usd if token_fee_usd > ZERO else dec(route.get("tokenFee")) * sell_price / (Decimal("10") ** _token_decimals(buy_pool))
            bridge_native_fee = dec(route.get("nativeFee")) + dec(route.get("setupFee"))
            bridge_cost = bridge_native_fee / (Decimal("10") ** NATIVE_DECIMALS.get(buy_pool.get("chain"), 18)) * _native_rate(buy_pool, rates)
            bridge_cost += dec(route.get("setupFeeUsd"))
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

    confidence_bps, confidence_reasons = _confidence(
        buy_pool=buy_pool,
        sell_pool=sell_pool,
        trade_quotes=trade_quotes,
        bridge_quote=bridge_quote,
        notional=notional,
        net_bps=net_bps,
        bridge_seconds=bridge_seconds,
        token_out_before_bridge=token_out_before_bridge,
        arrived_amount=arrived_amount,
        reasons=reasons,
    )
    reasons.extend(confidence_reasons)
    if confidence_bps < int(getattr(settings, "CONI_ARBITRAGE_MIN_CONFIDENCE_BPS", 8500)):
        reasons.append("confidence_below_policy")

    decision = "take" if not reasons else "avoid"
    route = _route(bridge_quote) if bridge_quote else None
    market_cap = _market_cap_usd(launch, buy_pool, sell_pool)
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
        "confidenceBps": confidence_bps,
        "confidenceLabel": "high" if confidence_bps >= int(getattr(settings, "CONI_ARBITRAGE_MIN_CONFIDENCE_BPS", 8500)) else "insufficient",
        "analysisVersion": "v2-confidence-gate",
        "notionalUsd": str(notional),
        "bridgeSeconds": bridge_seconds,
        "bridgeRoute": {"id": route.get("id"), "amountOut": route.get("amountOut"), "nativeFee": route.get("nativeFee"), "tokenFee": route.get("tokenFee"), "setupFee": route.get("setupFee"), "seconds": route.get("seconds"), "callHash": _hash(route.get("call")) if route and route.get("call") else None, "state": "quoted"} if route else None,
        "buyAmountBaseUnits": str((trade_quotes or {}).get("_coniBuyAmountBaseUnits") or "0"),
        "sellAmountBaseUnits": str((trade_quotes or {}).get("_coniSellAmountBaseUnits") or "0"),
        "quoteState": quote_state,
        "buyPriceUsd": str(buy_price),
        "sellPriceUsd": str(sell_price),
        "marketCapUsd": str(market_cap) if market_cap > ZERO else None,
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
    quoted_at = int(time.time())
    token_out = _amount_from_quote(buy_quote, "buy")
    if token_out <= ZERO:
        return {"buy": buy_quote}, None
    bridge_result = quote_bridge(slug=launch["slug"], source_chain=buy_pool["chain"], destination_chain=sell_pool["chain"], amount_base_units=int(token_out), source_wallet=buy_wallet, destination_wallet=sell_wallet, solana_wallet=buy_wallet if buy_pool["chain"] == "solana" else sell_wallet)
    bridge_quote = dict(bridge_result["quote"])
    bridge_quote["_coniQuotedAt"] = int(time.time())
    if bridge_result.get("solanaTransaction"):
        bridge_quote["_coniSolanaTransaction"] = bridge_result["solanaTransaction"]
    sell_amount = int(bridge_result["amountOut"])
    sell_body = {"slug": launch["slug"], "side": "sell", "chain": sell_pool["chain"], "amount": str(sell_amount), "user": sell_wallet, "recipient": sell_wallet, "slippageBps": int(getattr(settings, "CONI_ARBITRAGE_SLIPPAGE_BPS", 100))}
    if sell_pool["chain"] == "solana":
        sell_body["solanaUser"] = sell_wallet
        sell_body.pop("user", None)
    sell_quote = request_json("/trade/quote", method="POST", payload=sell_body)
    return {"buy": buy_quote, "sell": sell_quote, "_coniBuyAmountBaseUnits": str(native_units), "_coniSellAmountBaseUnits": str(sell_amount), "_coniQuotedAt": quoted_at}, bridge_quote


def fresh_user_quotes(opportunity, *, buy_wallet, sell_wallet, market_data=None):
    """Rebuild a route for one user's active bot-wallet addresses.

    The scanner's persisted opportunity is a discovery snapshot only. This
    function fetches a new pool snapshot and new Pangea trade/bridge quotes so
    the signer never receives a quote created for another wallet or an expired
    market state.
    """
    data = market_data or request_json("/pools")
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
    row = _apply_state_drift_guard(
        row,
        launch=launch,
        buy_pool=buy_pool,
        sell_pool=sell_pool,
        baseline=opportunity.decision,
    )
    if not row["executable"]:
        raise PangeaError("fresh user quote no longer meets the market-state policy")
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
    data = market_snapshot()
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
