import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .planner_funding import native_reserve_base_units, required_native_units, reserves_configured


class PlannerFundingTests(SimpleTestCase):
    @override_settings(CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS='{"base":"1000","bsc":"2000"}')
    def test_buy_requirement_includes_trade_and_bridge_native_amounts(self):
        decision = {"buyAmountBaseUnits": "5000", "bridgeRoute": {"nativeFee": "30", "setupFee": "20"}}
        self.assertEqual(required_native_units(decision, "base", role="buy"), 6050)
        self.assertEqual(required_native_units(decision, "base", role="sell"), 1000)

    @override_settings(CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS="{}")
    def test_missing_reserve_disables_planning(self):
        self.assertIsNone(native_reserve_base_units("base"))
        self.assertFalse(reserves_configured(("base", "bsc")))

    @override_settings(CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS='{"base":"1000"}')
    def test_invalid_quote_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            required_native_units({"buyAmountBaseUnits": "0", "bridgeRoute": {}}, "base", role="buy")
