import os
from types import SimpleNamespace

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .bot_wallets import chain_accessible


class BotWalletAccessTests(SimpleTestCase):
    def test_evm_identity_can_select_each_evm_bot_network(self):
        identity = SimpleNamespace(chain="base")
        for chain in ("ethereum", "base", "robinhood", "bsc"):
            with self.subTest(chain=chain):
                self.assertTrue(chain_accessible(identity, chain))

    def test_solana_identity_cannot_select_evm_bot_network(self):
        identity = SimpleNamespace(chain="solana")
        self.assertTrue(chain_accessible(identity, "solana"))
        self.assertFalse(chain_accessible(identity, "base"))

    def test_evm_identity_cannot_select_solana_bot_network(self):
        identity = SimpleNamespace(chain="ethereum")
        self.assertFalse(chain_accessible(identity, "solana"))
