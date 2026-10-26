import os
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .arbitrage import _amount_from_quote, evaluate_candidate
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
            "sell": {"details": {"amountOut": "5000000000000000"}},
        }
        bridge_quote = {"routes": [{"id": "canonical", "amountOut": "9980000000", "tokenFee": "20000000", "nativeFee": "1000000", "seconds": 12}]}
        row = evaluate_candidate(launch=self.launch, buy_pool=self.buy, sell_pool=self.sell, rates={"ETH": "3000", "BNB": "600"}, trade_quotes=trade_quotes, bridge_quote=bridge_quote, require_quotes=True)
        self.assertTrue(row["executable"])
        self.assertGreater(Decimal(row["netProfitUsd"]), Decimal("0"))
        self.assertEqual(row["quoteState"], "fresh")

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
