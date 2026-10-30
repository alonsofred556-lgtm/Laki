import os
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import TestCase, override_settings

django.setup()

from .bot_wallets import create_bot_transfer
from .funding import account_for, ledger_balance
from .models import BotWallet, LedgerEntry, UserBotControl, WalletIdentity


@override_settings(
    CONI_BOT_WITHDRAWAL_LIMITS='{"base:ETH":{"perTransferBaseUnits":"100","dailyBaseUnits":"250"}}',
    CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED=True,
)
class BotWalletTransferLedgerTests(TestCase):
    def setUp(self):
        self.identity = WalletIdentity.objects.create(
            chain="base",
            address="0x" + "1" * 40,
        )
        UserBotControl.objects.create(owner=self.identity, wallet_mode="external-bot")
        self.wallet = BotWallet.objects.create(
            owner=self.identity,
            chain="base",
            address="0x" + "2" * 40,
            mode="external-bot",
            custody="audited-signer",
            status="active",
            source_address=self.identity.address,
            signer_key_ref="hsm/base/bot-test",
        )
        available = account_for(self.identity, "ETH", "base", "bot-available", bot_wallet=self.wallet)
        treasury = account_for(None, "ETH", "base", "onchain-receivable")
        LedgerEntry.objects.create(
            reference_type="test-deposit",
            reference_id="bot-withdrawal-ledger-test",
            debit_account=treasury,
            credit_account=available,
            asset="ETH",
            amount_base_units=Decimal("100"),
        )

    def test_new_withdrawal_reserves_bot_balance(self):
        transfer = create_bot_transfer(
            identity=self.identity,
            wallet_id=self.wallet.id,
            direction="withdrawal",
            asset="ETH",
            amount_base_units="40",
            destination=self.identity.address,
            idempotency_key="bot-withdrawal-ledger-test-1",
        )

        available = account_for(self.identity, "ETH", "base", "bot-available", bot_wallet=self.wallet)
        reserved = account_for(self.identity, "ETH", "base", "bot-withdrawal-reserved", bot_wallet=self.wallet)
        self.assertEqual(ledger_balance(available), Decimal("60"))
        self.assertEqual(ledger_balance(reserved), Decimal("40"))
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference_type="bot-withdrawal-reservation",
                reference_id=str(transfer.id),
            ).count(),
            1,
        )

    def test_withdrawal_cannot_reserve_more_than_bot_balance(self):
        with self.assertRaises(ValueError):
            create_bot_transfer(
                identity=self.identity,
                wallet_id=self.wallet.id,
                direction="withdrawal",
                asset="ETH",
                amount_base_units="101",
                destination=self.identity.address,
                idempotency_key="bot-withdrawal-ledger-test-2",
            )

    def test_active_label_without_signer_reference_cannot_create_transfer(self):
        self.wallet.signer_key_ref = ""
        self.wallet.save(update_fields=["signer_key_ref", "updated_at"])

        with self.assertRaisesMessage(ValueError, "bot wallet is awaiting audited signer provisioning"):
            create_bot_transfer(
                identity=self.identity,
                wallet_id=self.wallet.id,
                direction="withdrawal",
                asset="ETH",
                amount_base_units="1",
                destination=self.identity.address,
                idempotency_key="bot-withdrawal-ledger-test-no-signer-ref",
            )

    def test_repeating_same_idempotency_key_does_not_double_reserve(self):
        first = create_bot_transfer(
            identity=self.identity,
            wallet_id=self.wallet.id,
            direction="withdrawal",
            asset="ETH",
            amount_base_units="40",
            destination=self.identity.address,
            idempotency_key="bot-withdrawal-ledger-test-3",
        )
        second = create_bot_transfer(
            identity=self.identity,
            wallet_id=self.wallet.id,
            direction="withdrawal",
            asset="ETH",
            amount_base_units="40",
            destination=self.identity.address,
            idempotency_key="bot-withdrawal-ledger-test-3",
        )

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            LedgerEntry.objects.filter(
                reference_type="bot-withdrawal-reservation",
                reference_id=str(first.id),
            ).count(),
            1,
        )

    def test_bot_transfer_rejects_amounts_wider_than_ledger_precision(self):
        with self.assertRaisesMessage(ValueError, "invalid bot transfer"):
            create_bot_transfer(
                identity=self.identity,
                wallet_id=self.wallet.id,
                direction="withdrawal",
                asset="ETH",
                amount_base_units="1" + "0" * 78,
                destination=self.identity.address,
                idempotency_key="bot-withdrawal-ledger-test-wide",
            )
