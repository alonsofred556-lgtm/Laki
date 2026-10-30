import json
import os
from decimal import Decimal

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import TestCase, override_settings

django.setup()

from .bot_wallets import bot_wallet_summary, create_bot_transfer
from .fees import accrue_fee, claim_fee_events, fee_amount_base_units, record_fee_event
from .funding import account_for, credit_confirmed_deposit, ledger_balance, reserve_withdrawal
from .models import BotWallet, DepositObservation, Execution, FundingIntent, LedgerEntry, WalletIdentity, WithdrawalRequest


BASE = "0x" + "1" * 40
BOT = "0x" + "2" * 40
FEE = "0x" + "3" * 40


@override_settings(
    CONI_TRADING_FEE_BPS=10,
    CONI_BOT_FUNDING_FEE_BPS=10,
    CONI_BOT_WITHDRAWAL_FEE_BPS=10,
    CONI_WITHDRAWAL_FEE_BPS=10,
    CONI_FEE_RECIPIENTS=json.dumps({"ETH": {"base": FEE}}),
    CONI_SIGNER_FEE_KEY_REFS=json.dumps({"base": "hsm:fee:base"}),
    CONI_BOT_WITHDRAWAL_LIMITS_REQUIRED=False,
)
class FeeLifecycleTests(TestCase):
    def setUp(self):
        self.identity = WalletIdentity.objects.create(chain="base", address=BASE)

    def test_fee_is_exactly_ten_basis_points_in_integer_base_units(self):
        amount, bps = fee_amount_base_units("10000", "funding")
        self.assertEqual(amount, Decimal("10"))
        self.assertEqual(bps, 10)

    def test_funding_credits_net_amount_and_queues_fee_transfer(self):
        wallet = BotWallet.objects.create(
            owner=self.identity,
            chain="base",
            address=BOT,
            status="active",
            signer_key_ref="hsm:bot:base",
        )
        intent = FundingIntent.objects.create(
            wallet_identity=self.identity,
            bot_wallet=wallet,
            asset="ETH",
            chain="base",
            amount_base_units=10000,
            destination=BOT,
            status="pending-verification",
            expires_at="2030-01-01T00:00:00Z",
        )
        observation = DepositObservation.objects.create(
            event_key="base:fee-test:0",
            funding_intent=intent,
            wallet_identity=self.identity,
            asset="ETH",
            chain="base",
            sender=BASE,
            destination=BOT,
            tx_hash="0x" + "a" * 64,
            amount_base_units=10000,
            status="confirmed",
        )
        credit_confirmed_deposit(observation.id)
        available = account_for(self.identity, "ETH", "base", "bot-available", bot_wallet=wallet)
        self.assertEqual(ledger_balance(available), Decimal("9990"))
        fee = self.identity.feeevent_set.get(kind="funding")
        self.assertEqual(fee.amount_base_units, Decimal("10"))
        self.assertEqual(fee.recipient, FEE)

    def test_normal_withdrawal_reserves_requested_amount_plus_fee(self):
        available = account_for(self.identity, "ETH", "base", "available")
        treasury = account_for(None, "ETH", "base", "onchain-receivable")
        LedgerEntry.objects.create(
            reference_type="fee-test-user-funding", reference_id="1", debit_account=treasury,
            credit_account=available, asset="ETH", amount_base_units=20000,
        )
        withdrawal = WithdrawalRequest.objects.create(
            idempotency_key="fee-test-withdrawal-1", wallet_identity=self.identity, asset="ETH",
            chain="base", amount_base_units=10000, fee_amount_base_units=10,
            total_reserved_base_units=10010, destination=BASE, status="pending-approval",
            nonce="fee-test-withdrawal-nonce-1", expires_at="2030-01-01T00:00:00Z",
        )
        fee = accrue_fee(
            kind="withdrawal", source_id=str(withdrawal.id), asset="ETH", chain="base",
            gross_amount_base_units=10000, user_identity=self.identity,
        )
        reserve_withdrawal(withdrawal.id)
        self.assertEqual(ledger_balance(available), Decimal("9990"))
        self.assertEqual(withdrawal.total_reserved_base_units, Decimal("10010"))
        self.assertEqual(fee.amount_base_units, Decimal("10"))

    def test_bot_withdrawal_reserves_requested_amount_plus_fee(self):
        wallet = BotWallet.objects.create(
            owner=self.identity,
            chain="base",
            address=BOT,
            status="active",
            signer_key_ref="hsm:bot:base",
        )
        available = account_for(self.identity, "ETH", "base", "bot-available", bot_wallet=wallet)
        treasury = account_for(None, "ETH", "base", "onchain-receivable")
        LedgerEntry.objects.create(
            reference_type="fee-test-funding",
            reference_id="1",
            debit_account=treasury,
            credit_account=available,
            asset="ETH",
            amount_base_units=20000,
        )
        transfer = create_bot_transfer(
            identity=self.identity,
            wallet_id=wallet.id,
            direction="withdrawal",
            asset="ETH",
            amount_base_units="10000",
            destination=BASE,
            idempotency_key="fee-test-transfer-1",
        )
        self.assertEqual(transfer.fee_amount_base_units, Decimal("10"))
        self.assertEqual(transfer.total_reserved_base_units, Decimal("10010"))
        self.assertEqual(ledger_balance(available), Decimal("9990"))

    def test_fee_queue_is_leased_and_confirmed_idempotently(self):
        execution = Execution.objects.create(user_identity=self.identity, slug="fee-trade", state="settled")
        fee = accrue_fee(
            kind="trade",
            source_id=str(execution.id),
            asset="ETH",
            chain="base",
            gross_amount_base_units=10000,
            user_identity=self.identity,
            execution_id=execution.id,
        )
        queued = claim_fee_events(limit=1)
        self.assertEqual([item.id for item in queued], [fee.id])
        self.assertEqual(queued[0].status, "leased")
        proof = {"verified": True, "chain": "base", "asset": "ETH", "amountBaseUnits": "10", "recipient": FEE}
        record_fee_event(fee_id=fee.id, event="submitted", idempotency_key="fee-callback-submitted", tx_hash="0x" + "b" * 64, metadata=proof)
        record_fee_event(fee_id=fee.id, event="confirmed", idempotency_key="fee-callback-confirmed", tx_hash="0x" + "c" * 64, metadata={**proof, "finality": "confirmed"})
        fee.refresh_from_db()
        self.assertEqual(fee.status, "confirmed")
        self.assertEqual(fee.tx_hash, "0x" + "c" * 64)

    def test_bot_wallet_history_exposes_settled_trade_fee(self):
        wallet = BotWallet.objects.create(
            owner=self.identity, chain="base", address=BOT, status="active",
            signer_key_ref="hsm:bot:base",
        )
        execution = Execution.objects.create(
            user_identity=self.identity, bot_wallet=wallet, slug="history-fee-trade", state="settled"
        )
        fee = accrue_fee(
            kind="trade", source_id=str(execution.id), asset="ETH", chain="base",
            gross_amount_base_units=10000, user_identity=self.identity, execution_id=execution.id,
        )
        summary = bot_wallet_summary(self.identity, selected_wallet_id=str(wallet.id))
        item = next(row for row in summary["history"] if row["id"] == str(execution.id))
        self.assertEqual(item["fee"]["amountBaseUnits"], "10")
        self.assertEqual(item["fee"]["feeBps"], 10)
        self.assertEqual(item["fee"]["state"], fee.status)
