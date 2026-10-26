import os
from types import SimpleNamespace

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .bot_wallets import bot_transfer_chain, chain_accessible, select_bot_wallet


class BotWalletAccessTests(SimpleTestCase):
    def test_summary_prefers_persisted_internal_mode(self):
        wallets = [
            SimpleNamespace(id="external", chain="base", mode="external-bot"),
            SimpleNamespace(id="internal", chain="base", mode="internal-bot"),
        ]
        self.assertEqual(select_bot_wallet(wallets, chain="base", mode="internal-bot").id, "internal")

    def test_summary_falls_back_explicitly_when_mode_is_not_provisioned(self):
        wallets = [SimpleNamespace(id="external", chain="base", mode="external-bot")]
        self.assertEqual(select_bot_wallet(wallets, chain="base", mode="internal-bot").mode, "external-bot")

    def test_transfer_uses_selected_bot_wallet_chain(self):
        wallet = SimpleNamespace(chain="robinhood")
        self.assertEqual(bot_transfer_chain(wallet), "robinhood")

    def test_transfer_requires_a_bot_wallet_chain(self):
        with self.assertRaises(ValueError):
            bot_transfer_chain(SimpleNamespace(chain=""))

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
