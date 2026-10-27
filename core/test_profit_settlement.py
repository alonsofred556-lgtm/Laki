import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .profit_settlement import settlement_identity_can_receive


class ProfitSettlementIdentityTests(SimpleTestCase):
    def test_any_supported_evm_identity_can_receive_on_robinhood(self):
        for chain in ("ethereum", "base", "robinhood", "bsc"):
            with self.subTest(chain=chain):
                self.assertTrue(settlement_identity_can_receive(chain, "robinhood"))

    def test_solana_identity_cannot_receive_evm_usdg(self):
        self.assertFalse(settlement_identity_can_receive("solana", "robinhood"))

    def test_other_settlement_chain_is_not_implicitly_allowed(self):
        self.assertFalse(settlement_identity_can_receive("base", "ethereum"))
