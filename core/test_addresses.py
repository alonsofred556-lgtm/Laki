import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .bot_wallets import addresses_match
from .deposit_discovery import _address_key


class ChainAddressComparisonTests(SimpleTestCase):
    def test_evm_addresses_are_case_insensitive(self):
        address = "0xAbCDEF0123456789abcdef0123456789ABCDEF01"
        self.assertTrue(addresses_match("base", address.lower(), address))

    def test_solana_addresses_are_case_sensitive(self):
        address = "5KtxmGhFfy6QaoU3Qtu2ouweqq1qfXfNZHg3CJH7y6ea"
        self.assertTrue(addresses_match("solana", address, address))
        self.assertFalse(addresses_match("solana", address.lower(), address))
        self.assertEqual(_address_key("solana", address), address)

