import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .execution import (
    _quote_hash,
    execution_event_requires_bridge_guid,
    execution_event_requires_tx_hash,
    next_execution_state,
    signer_wallets_ready,
)
from .bot_wallets import bot_transfer_event_requires_tx_hash, next_bot_transfer_state
from .profit_settlement import next_settlement_state, settlement_event_requires_tx_hash


class ExecutionLifecycleTests(SimpleTestCase):
    def test_signer_handoff_requires_both_active_attested_wallets(self):
        wallet = type("Wallet", (), {"status": "active", "address": "0xabc", "signer_key_ref": "hsm/key-1"})()
        execution = type("Execution", (), {"bot_wallet": wallet, "sell_bot_wallet": wallet})()
        self.assertTrue(signer_wallets_ready(execution))
        wallet.signer_key_ref = ""
        self.assertFalse(signer_wallets_ready(execution))

    def test_signer_handoff_rejects_revoked_wallet(self):
        wallet = type("Wallet", (), {"status": "revoked", "address": "0xabc", "signer_key_ref": "hsm/key-1"})()
        execution = type("Execution", (), {"bot_wallet": wallet, "sell_bot_wallet": wallet})()
        self.assertFalse(signer_wallets_ready(execution))

    def test_happy_path_is_strictly_ordered(self):
        state = "planned"
        for event, expected in (
            ("signer-requested", "awaiting-signer"),
            ("buy-submitted", "buy-submitted"),
            ("buy-confirmed", "buy-confirmed"),
            ("bridge-source-submitted", "bridge-source-submitted"),
            ("bridge-source-confirmed", "bridge-source-confirmed"),
            ("bridge-in-flight", "bridge-in-flight"),
            ("bridge-destination-confirmed", "bridge-destination-confirmed"),
            ("sell-submitted", "sell-submitted"),
            ("sell-confirmed", "sell-confirmed"),
            ("settled", "settled"),
        ):
            state = next_execution_state(state, event)
            self.assertEqual(state, expected)

    def test_out_of_order_destination_confirmation_is_rejected(self):
        with self.assertRaises(ValueError):
            next_execution_state("buy-submitted", "bridge-destination-confirmed")

    def test_terminal_execution_cannot_be_failed_again(self):
        with self.assertRaises(ValueError):
            next_execution_state("settled", "failed")

    def test_refund_is_only_valid_after_bridge_progress(self):
        self.assertEqual(next_execution_state("bridge-in-flight", "refunded"), "refunded")
        with self.assertRaises(ValueError):
            next_execution_state("planned", "refunded")

    def test_execution_callbacks_require_transaction_proof(self):
        self.assertTrue(execution_event_requires_tx_hash("buy-submitted"))
        self.assertTrue(execution_event_requires_tx_hash("sell-confirmed"))
        self.assertFalse(execution_event_requires_tx_hash("bridge-in-flight"))
        self.assertTrue(execution_event_requires_bridge_guid("bridge-in-flight"))
        self.assertFalse(execution_event_requires_bridge_guid("bridge-source-confirmed"))

    def test_quote_hash_is_canonical_for_nested_artifact(self):
        artifact = {"trade": {"buy": {"amount": "10"}}, "bridge": {"route": "canonical"}}
        equivalent = {"bridge": {"route": "canonical"}, "trade": {"buy": {"amount": "10"}}}
        self.assertEqual(_quote_hash(artifact), _quote_hash(equivalent))

    def test_bot_transfer_callbacks_are_strictly_ordered(self):
        self.assertEqual(next_bot_transfer_state("pending-approval", "submitted"), "submitted")
        self.assertEqual(next_bot_transfer_state("submitted", "confirmed"), "confirmed")
        with self.assertRaises(ValueError):
            next_bot_transfer_state("confirmed", "failed")

    def test_bot_transfer_proof_is_required_for_submission_and_confirmation(self):
        self.assertTrue(bot_transfer_event_requires_tx_hash("submitted"))
        self.assertTrue(bot_transfer_event_requires_tx_hash("confirmed"))
        self.assertFalse(bot_transfer_event_requires_tx_hash("failed"))

    def test_profit_settlement_requires_submitted_before_confirmation(self):
        self.assertEqual(next_settlement_state("pending-approval", "submitted"), "submitted")
        self.assertEqual(next_settlement_state("submitted", "confirmed"), "confirmed")
        with self.assertRaises(ValueError):
            next_settlement_state("pending-approval", "confirmed")

    def test_profit_settlement_proof_is_required_for_submission_and_confirmation(self):
        self.assertTrue(settlement_event_requires_tx_hash("submitted"))
        self.assertTrue(settlement_event_requires_tx_hash("confirmed"))
        self.assertFalse(settlement_event_requires_tx_hash("failed"))
