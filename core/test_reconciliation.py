import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .reconciliation import bot_transfer_ledger_anomalies, can_release_expired_withdrawal, profit_settlement_ledger_anomalies, release_expired_withdrawals


class ReconciliationInvariantTests(SimpleTestCase):
    def test_expiry_release_requires_exactly_one_reservation(self):
        self.assertTrue(can_release_expired_withdrawal(status="pending-approval", reservation_count=1))
        self.assertFalse(can_release_expired_withdrawal(status="pending-approval", reservation_count=0))
        self.assertFalse(can_release_expired_withdrawal(status="pending-approval", reservation_count=2))
        self.assertFalse(can_release_expired_withdrawal(status="submitted", reservation_count=1))

    @patch("core.reconciliation.record_event")
    @patch("core.reconciliation.release_withdrawal")
    @patch("core.reconciliation.LedgerEntry.objects")
    @patch("core.reconciliation.WithdrawalRequest.objects")
    def test_expiry_pass_releases_only_pending_approval_requests(self, manager, ledger_manager, release, record):
        manager.filter.return_value.only.return_value = [SimpleNamespace(id="withdrawal-1")]
        ledger_manager.filter.return_value.count.return_value = 1
        release.return_value = SimpleNamespace(status="released")
        self.assertEqual(release_expired_withdrawals(), 1)
        manager.filter.assert_called_once()
        self.assertEqual(release.call_args.args, ("withdrawal-1", "expired"))
        record.assert_called_once()

    def test_confirmed_bot_withdrawal_requires_one_settlement(self):
        transfer = SimpleNamespace(id="transfer-1", direction="withdrawal", state="confirmed")
        anomalies = bot_transfer_ledger_anomalies(transfer, reservation_count=1, settlement_count=0, release_count=0)
        self.assertEqual(anomalies[0]["type"], "confirmed-bot-withdrawal-ledger-mismatch")

    def test_failed_bot_withdrawal_requires_release(self):
        transfer = SimpleNamespace(id="transfer-2", direction="withdrawal", state="failed")
        anomalies = bot_transfer_ledger_anomalies(transfer, reservation_count=1, settlement_count=0, release_count=0)
        self.assertEqual(anomalies[0]["type"], "failed-bot-withdrawal-ledger-mismatch")

    def test_confirmed_profit_settlement_requires_one_entry(self):
        settlement = SimpleNamespace(id="settlement-1", state="confirmed")
        self.assertTrue(profit_settlement_ledger_anomalies(settlement, settlement_entry_count=0))
        self.assertFalse(profit_settlement_ledger_anomalies(settlement, settlement_entry_count=1))

    def test_pending_profit_settlement_must_not_have_ledger_entry(self):
        settlement = SimpleNamespace(id="settlement-2", state="pending-approval")
        self.assertTrue(profit_settlement_ledger_anomalies(settlement, settlement_entry_count=1))
