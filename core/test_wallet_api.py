import json
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import TestCase

django.setup()

from .models import BotWallet, WalletIdentity
from .bot_wallets import ensure_bot_wallets


class BotWalletApiOwnershipTests(TestCase):
    def setUp(self):
        self.owner = WalletIdentity.objects.create(chain="base", address="0x" + "1" * 40)
        self.foreign = WalletIdentity.objects.create(chain="base", address="0x" + "3" * 40)
        ensure_bot_wallets(self.owner, mode="external-bot")
        self.foreign_wallet = BotWallet.objects.create(
            owner=self.foreign,
            chain="base",
            address="0x" + "4" * 40,
            mode="external-bot",
            status="active",
            signer_key_ref="hsm/base/foreign-test",
        )
        session = self.client.session
        session["wallet_identity_ids"] = [str(self.owner.id)]
        session["wallet_identity_id"] = str(self.owner.id)
        session.save()

    def test_bot_wallet_read_rejects_wallet_from_another_authenticated_owner(self):
        response = self.client.get(f"/api/bot-wallet?walletId={self.foreign_wallet.id}")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(json.loads(response.content)["error"], "authenticated wallet session required")

    def test_bot_wallet_transfer_rejects_wallet_from_another_authenticated_owner(self):
        response = self.client.post(
            "/api/bot-wallet/transfer",
            data={
                "botWalletId": str(self.foreign_wallet.id),
                "direction": "withdrawal",
                "asset": "ETH",
                "amountBaseUnits": "1",
                "destination": self.owner.address,
                "idempotencyKey": "foreign-wallet-api-test-1",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.foreign_wallet.transfers.exists())

    def test_each_identity_gets_one_bot_wallet_per_supported_chain_and_mode(self):
        self.assertEqual(set(BotWallet.objects.filter(owner=self.owner, mode="external-bot").values_list("chain", flat=True)), {"ethereum", "base", "robinhood", "bsc", "solana"})
        ensure_bot_wallets(self.owner, mode="internal-bot")
        self.assertEqual(BotWallet.objects.filter(owner=self.owner).count(), 10)
        self.assertEqual(BotWallet.objects.filter(owner=self.owner, mode="internal-bot").values("chain").count(), 5)
        self.assertEqual(BotWallet.objects.filter(owner=self.owner, mode="external-bot", chain="base").first().source_address, self.owner.address)
        self.assertEqual(BotWallet.objects.filter(owner=self.owner, mode="external-bot", chain="solana").first().source_address, "")
