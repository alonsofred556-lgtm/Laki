import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .assets import browser_funding_supported


class BrowserFundingCapabilityTests(SimpleTestCase):
    @override_settings(CONI_TOKEN_REGISTRY=json.dumps({
        "LEGACY": {"solana": {"address": "MintLegacy", "decimals": 9}},
        "TOKEN2022": {"solana": {"address": "Mint2022", "decimals": 9, "programId": "TokenzQdY2022"}},
    }))
    def test_only_explicitly_unsupported_solana_programs_are_hidden(self):
        self.assertTrue(browser_funding_supported("LEGACY", "solana"))
        self.assertFalse(browser_funding_supported("TOKEN2022", "solana"))
        self.assertTrue(browser_funding_supported("SOL", "solana"))

    def test_evm_assets_are_not_limited_by_solana_program_support(self):
        self.assertTrue(browser_funding_supported("ETH", "base"))
