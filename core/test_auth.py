import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .views import wallet_address_matches


class WalletAuthenticationTests(SimpleTestCase):
    def test_evm_address_matching_is_case_insensitive(self):
        address = "0xAbCd000000000000000000000000000000000001"
        self.assertTrue(wallet_address_matches("base", address.lower(), address))

    def test_solana_address_matching_is_case_sensitive(self):
        address = "Abcdefghijkmnpqrstuvwxyz123456789"
        self.assertTrue(wallet_address_matches("solana", address, address))
        self.assertFalse(wallet_address_matches("solana", address.swapcase(), address))

    def test_non_string_addresses_never_match(self):
        self.assertFalse(wallet_address_matches("base", None, "0x1111111111111111111111111111111111111111"))
