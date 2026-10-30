import os
import time
from types import SimpleNamespace
from unittest.mock import patch
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .arbitrage import _amount_from_quote, _apply_state_drift_guard, candidate_routes, evaluate_candidate, quote_discovery_opportunity
from .pangea_bridge import next_bridge_state


class ArbitrageDecisionTests(SimpleTestCase):
    def setUp(self):
        self.launch = {"slug": "demo", "name": "Demo", "symbol": "D", "image": None}
        self.buy = {"chain": "base", "token": "0xbuy", "priceUsd": "1", "quoteSymbol": "ETH", "depthUsd": "1000"}
        self.sell = {"chain": "bsc", "token": "0xsell", "priceUsd": "1.5", "quoteSymbol": "BNB", "depthUsd": "1000"}

    @override_settings(
        CONI_ARBITRAGE_NOTIONAL_USD="10",
        CONI_ARBITRAGE_MAX_NOTIONAL_USD="25",
        CONI_ARBITRAGE_MIN_NET_BPS=100,
        CONI_ARBITRAGE_MIN_PROFIT_USD="0.05",
        CONI_ARBITRAGE_RISK_BUFFER_BPS=75,
        CONI_TRADING_FEE_BPS=25,
        CONI_ARBITRAGE_MAX_BRIDGE_SECONDS=120,
    )
    def test_exact_quotes_are_required_and_costed(self):
        trade_quotes = {
            "buy": {"details": {"amountOut": "10000000000"}},
            "sell": {"details": {"amountOut": "25000000000000000"}},
            "_coniQuotedAt": int(time.time()),
        }
        bridge_quote = {"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "20000000", "nativeFee": "1000000", "seconds": 12}], "_coniQuotedAt": int(time.time())}
        row = evaluate_candidate(launch=self.launch, buy_pool=self.buy, sell_pool=self.sell, rates={"ETH": "3000", "BNB": "600"}, trade_quotes=trade_quotes, bridge_quote=bridge_quote, require_quotes=True)
        self.assertTrue(row["executable"])
        self.assertGreater(Decimal(row["netProfitUsd"]), Decimal("0"))
        self.assertLess(Decimal(row["netProfitUsd"]), Decimal("10"))
        self.assertEqual(row["quoteState"], "fresh")
        self.assertGreaterEqual(row["confidenceBps"], 8500)

    def test_native_quote_output_is_converted_from_base_units_before_profit_check(self):
        trade_quotes = {
            "buy": {"details": {"amountOut": "10000000000"}},
            "sell": {"details": {"amountOut": "5000000000000000"}},
            "_coniQuotedAt": int(time.time()),
        }
        bridge_quote = {"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "0", "nativeFee": "0", "seconds": 12}], "_coniQuotedAt": int(time.time())}
        row = evaluate_candidate(
            launch=self.launch,
            buy_pool=self.buy,
            sell_pool=self.sell,
            rates={"ETH": "3000", "BNB": "600"},
            trade_quotes=trade_quotes,
            bridge_quote=bridge_quote,
            require_quotes=True,
        )
        self.assertFalse(row["executable"])
        self.assertIn("net_profit_below_minimum", row["reasons"])
        self.assertEqual(Decimal(row["executableGrossProfitUsd"]), Decimal("-7.00000000"))

    def test_missing_freshness_evidence_is_not_high_confidence(self):
        row = evaluate_candidate(
            launch=self.launch,
            buy_pool=self.buy,
            sell_pool=self.sell,
            rates={"ETH": "3000", "BNB": "600"},
            trade_quotes={"buy": {"details": {"amountOut": "10000000000"}}, "sell": {"details": {"amountOut": "25000000000000000"}}},
            bridge_quote={"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "0", "nativeFee": "0", "seconds": 12}]},
            require_quotes=True,
        )
        self.assertFalse(row["executable"])
        self.assertIn("quote_freshness_unknown", row["reasons"])
        self.assertIn("confidence_below_policy", row["reasons"])

    def test_high_price_impact_is_rejected_even_when_displayed_spread_is_large(self):
        now = int(time.time())
        row = evaluate_candidate(
            launch=self.launch,
            buy_pool=self.buy,
            sell_pool=self.sell,
            rates={"ETH": "3000", "BNB": "600"},
            trade_quotes={
                "buy": {"details": {"amountOut": "10000000000", "priceImpactBps": "150"}},
                "sell": {"details": {"amountOut": "25000000000000000"}},
                "_coniQuotedAt": now,
            },
            bridge_quote={"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "0", "nativeFee": "0", "seconds": 12}], "_coniQuotedAt": now},
            require_quotes=True,
        )
        self.assertFalse(row["executable"])
        self.assertIn("price_impact_above_policy", row["reasons"])

    @override_settings(CONI_ARBITRAGE_MAX_PRICE_MOVE_BPS=200, CONI_ARBITRAGE_MAX_MARKET_CAP_MOVE_BPS=2000)
    def test_state_drift_guard_rejects_price_and_market_cap_move(self):
        row = {"reasons": [], "decision": "take", "executable": True, "confidenceBps": 9500, "confidenceLabel": "high"}
        result = _apply_state_drift_guard(
            row,
            launch={"marketCapUsd": "130000"},
            buy_pool={"priceUsd": "1.03"},
            sell_pool={"priceUsd": "1.50"},
            baseline={"buyPriceUsd": "1", "sellPriceUsd": "1.50", "marketCapUsd": "100000"},
        )
        self.assertFalse(result["executable"])
        self.assertIn("buy_price_moved_too_far", result["reasons"])
        self.assertIn("market_cap_moved_too_far", result["reasons"])

    @override_settings(CONI_ARBITRAGE_REQUIRE_MARKET_CAP_DATA=True)
    def test_state_drift_guard_rejects_missing_market_cap_evidence_when_required(self):
        row = {"reasons": [], "decision": "take", "executable": True, "confidenceBps": 9500, "confidenceLabel": "high"}
        result = _apply_state_drift_guard(
            row,
            launch={},
            buy_pool={"priceUsd": "1"},
            sell_pool={"priceUsd": "1.50"},
            baseline={"buyPriceUsd": "1", "sellPriceUsd": "1.50"},
        )
        self.assertFalse(result["executable"])
        self.assertIn("market_cap_evidence_missing", result["reasons"])

    def test_market_cap_uses_pangea_cap_supply_with_conservative_route_price(self):
        row = {"reasons": [], "decision": "take", "executable": True, "confidenceBps": 9500, "confidenceLabel": "high"}
        result = _apply_state_drift_guard(
            row,
            launch={"capSupply": "5000000000"},
            buy_pool={"priceUsd": "0.000001"},
            sell_pool={"priceUsd": "0.000002"},
            baseline={"buyPriceUsd": "0.000001", "sellPriceUsd": "0.000002", "marketCapUsd": "5000"},
        )
        self.assertEqual(Decimal(result["marketCapUsd"]), Decimal("5000"))

    def test_displayed_gap_without_quotes_is_not_executable(self):
        row = evaluate_candidate(launch=self.launch, buy_pool=self.buy, sell_pool=self.sell, rates={}, require_quotes=True)
        self.assertFalse(row["executable"])
        self.assertIn("fresh_quotes_required", row["reasons"])

    def test_reads_current_pangea_trade_amount_shape(self):
        quote = {
            "details": {
                "currencyOut": {
                    "amount": "948789076157303",
                    "amountUsd": "9.760867",
                },
                "route": {
                    "destination": {
                        "outputCurrency": {"amount": "939301185395730"},
                    },
                },
            },
        }
        self.assertEqual(_amount_from_quote(quote, "buy"), Decimal("948789076157303"))

    def test_bridge_state_requires_destination_confirmation(self):
        self.assertEqual(next_bridge_state("quoted", "source-submitted"), "source-submitted")
        self.assertEqual(next_bridge_state("in-flight", "destination-confirmed"), "destination-confirmed")
        with self.assertRaises(ValueError):
            next_bridge_state("quoted", "destination-confirmed")

    def test_candidate_routes_covers_each_profitable_cross_chain_direction(self):
        data = {"launches": [{
            "slug": "demo",
            "pools": [
                {"chain": "base", "token": "0xb", "priceUsd": "1"},
                {"chain": "bsc", "token": "0xc", "priceUsd": "1.4"},
                {"chain": "ethereum", "token": "0xd", "priceUsd": "1.2"},
            ],
        }]}
        routes = candidate_routes(data, max_candidates=20)
        self.assertEqual(len(routes), 3)
        self.assertEqual([(row[1]["chain"], row[2]["chain"]) for row in routes], [
            ("base", "bsc"), ("base", "ethereum"), ("ethereum", "bsc"),
        ])

    def test_candidate_routes_covers_all_supported_chains_and_arbitrary_launches(self):
        chains = ("ethereum", "base", "robinhood", "bsc", "solana")
        launch = {
            "slug": "not-zgap",
            "symbol": "ANY",
            "pools": [
                {"chain": chain, "token": f"token-{chain}", "priceUsd": str(index + 1)}
                for index, chain in enumerate(chains)
            ],
        }
        second_launch = {
            "slug": "new-pangea-launch",
            "symbol": "NEW",
            "pools": [
                {"chain": "solana", "token": "new-sol", "priceUsd": "2"},
                {"chain": "robinhood", "token": "new-rh", "priceUsd": "3"},
            ],
        }

        routes = candidate_routes({"launches": [launch, second_launch]})

        # Five ascending pools produce every one of the ten profitable
        # buy-cheap/sell-dear directions. The unrelated second launch must
        # also be scanned, proving the feed is not hard-coded to ZGAP.
        self.assertEqual(len(routes), 11)
        self.assertEqual({row[0]["slug"] for row in routes}, {"not-zgap", "new-pangea-launch"})
        self.assertEqual(
            {(row[1]["chain"], row[2]["chain"]) for row in routes if row[0]["slug"] == "not-zgap"},
            {(buy, sell) for buy_index, buy in enumerate(chains) for sell in chains[buy_index + 1:]},
        )
        self.assertTrue(all(row[1]["chain"] != row[2]["chain"] for row in routes))

    @override_settings(CONI_ARBITRAGE_MAX_CANDIDATES=0)
    def test_candidate_routes_has_no_hidden_default_cap(self):
        pools = [{"chain": "base", "token": f"0xb{i}", "priceUsd": "1"} for i in range(2)]
        pools += [{"chain": "bsc", "token": f"0xc{i}", "priceUsd": "2"} for i in range(260)]
        routes = candidate_routes({"launches": [{"slug": "many", "pools": pools}]})
        self.assertGreater(len(routes), 500)

    @patch("core.arbitrage._quote_route")
    def test_discovery_quote_reloads_the_exact_route_tokens(self, quote_route):
        opportunity = SimpleNamespace(
            slug="demo",
            buy_chain="base",
            sell_chain="bsc",
            buy_token="0xbuy",
            sell_token="0xsell",
        )
        data = {
            "rates": {"ETH": "3000", "BNB": "600"},
            "launches": [{"slug": "demo", "symbol": "D", "pools": [self.buy, self.sell]}],
        }
        quote_route.return_value = (
            {
                "buy": {"details": {"amountOut": "10000000000"}},
                "sell": {"details": {"amountOut": "5000000000000000"}},
                "_coniQuotedAt": int(time.time()),
            },
            {"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "20000000", "nativeFee": "1000000", "seconds": 12}], "_coniQuotedAt": int(time.time())},
        )
        row = quote_discovery_opportunity(opportunity, data=data)
        self.assertEqual(row["quoteState"], "fresh")
        self.assertTrue(row["quoteHash"])
        self.assertEqual(row["quoteArtifact"]["trade"]["buy"]["details"]["amountOut"], "10000000000")
        quote_route.assert_called_once()
