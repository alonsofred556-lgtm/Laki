import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .profit_settlement import settlement_identity_can_receive
from .management.commands.queue_profit_settlements import Command


class ProfitSettlementIdentityTests(SimpleTestCase):
    def test_any_supported_evm_identity_can_receive_on_robinhood(self):
        for chain in ("ethereum", "base", "robinhood", "bsc"):
            with self.subTest(chain=chain):
                self.assertTrue(settlement_identity_can_receive(chain, "robinhood"))

    def test_solana_identity_cannot_receive_evm_usdg(self):
        self.assertFalse(settlement_identity_can_receive("solana", "robinhood"))

    def test_other_settlement_chain_is_not_implicitly_allowed(self):
        self.assertFalse(settlement_identity_can_receive("base", "ethereum"))


class ProfitSettlementQueueTests(SimpleTestCase):
    @override_settings(CONI_PROFIT_SETTLEMENT_ENABLED=False)
    def test_queue_is_inert_when_settlement_is_disabled(self):
        self.assertEqual(Command()._run_once(10)["reason"], "settlement-disabled")

    @override_settings(
        CONI_PROFIT_SETTLEMENT_ENABLED=True,
        CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS="",
        CONI_PROFIT_SETTLEMENT_DECIMALS=None,
        CONI_PROFIT_SETTLEMENT_PRICE_USD="1",
    )
    def test_queue_is_inert_without_verified_token_configuration(self):
        self.assertEqual(Command()._run_once(10)["reason"], "verified-usdg-config-or-price-missing")
