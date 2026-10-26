import os
from types import SimpleNamespace

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase

django.setup()

from .reconciliation import bot_transfer_ledger_anomalies, profit_settlement_ledger_anomalies


class ReconciliationInvariantTests(SimpleTestCase):
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
