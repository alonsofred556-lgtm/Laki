import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import TestCase

django.setup()

from .bot_wallets import accessible_bot_chains, claim_provision_requests, ensure_bot_wallets
from .models import BotWallet, BotWalletProvisionRequest, WalletIdentity


class BotWalletLifecycleTests(TestCase):
    def test_evm_identity_gets_one_pending_bot_wallet_and_request_per_supported_chain(self):
        identity = WalletIdentity.objects.create(
            chain="base", address="0x" + "1" * 40
        )

        wallets = ensure_bot_wallets(identity, mode="internal-bot")

        self.assertEqual({item.chain for item in wallets}, {"ethereum", "base", "robinhood", "bsc", "solana"})
        self.assertTrue(all(item.mode == "internal-bot" for item in wallets))
        self.assertTrue(all(item.status == "awaiting-signer-provision" for item in wallets))
        self.assertTrue(all(item.address == "" and item.signer_key_ref == "" for item in wallets))
        self.assertEqual(BotWalletProvisionRequest.objects.count(), 5)
        self.assertEqual({item.bot_wallet.chain for item in claim_provision_requests(limit=10)}, {"ethereum", "base", "robinhood", "bsc", "solana"})

    def test_solana_identity_keeps_all_records_but_exposes_only_solana(self):
        identity = WalletIdentity.objects.create(
            chain="solana", address="Abcdefghijkmnpqrstuvwxyz123456789"
        )

        wallets = ensure_bot_wallets(identity, mode="external-bot")

        self.assertEqual({item.chain for item in wallets}, {"ethereum", "base", "robinhood", "bsc", "solana"})
        self.assertEqual(accessible_bot_chains(identity), ("solana",))
        self.assertEqual(BotWallet.objects.filter(owner=identity, chain="solana").count(), 1)
        self.assertEqual(BotWalletProvisionRequest.objects.count(), 5)

    def test_custody_modes_have_separate_user_scoped_records(self):
        identity = WalletIdentity.objects.create(
            chain="ethereum", address="0x" + "2" * 40
        )

        ensure_bot_wallets(identity, mode="external-bot")
        ensure_bot_wallets(identity, mode="internal-bot")

        self.assertEqual(BotWallet.objects.filter(owner=identity).count(), 10)
        self.assertEqual(BotWalletProvisionRequest.objects.filter(bot_wallet__owner=identity).count(), 10)
        self.assertEqual(set(BotWallet.objects.filter(owner=identity).values_list("mode", flat=True)), {"external-bot", "internal-bot"})
