import json
import os
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .onchain_reconciliation import _evm_balance, _solana_balance, reconcile_bot_wallets


class OnchainReconciliationTests(SimpleTestCase):
    @patch("core.onchain_reconciliation.rpc_result", return_value="0x2a")
    def test_evm_native_balance_is_read_in_base_units(self, rpc):
        balance = _evm_balance("base", "0x" + "1" * 40, "ETH")
        self.assertEqual(balance, 42)
        rpc.assert_called_once_with("base", "eth_getBalance", ["0x" + "1" * 40, "latest"])

    @patch("core.onchain_reconciliation.rpc_result", return_value="0x2a")
    def test_evm_token_balance_uses_bound_balance_of_call(self, rpc):
        with override_settings(CONI_TOKEN_REGISTRY=json.dumps({"ZGAP": {"base": {"address": "0x" + "2" * 40, "decimals": 9}}})):
            balance = _evm_balance("base", "0x" + "1" * 40, "ZGAP")
        self.assertEqual(balance, 42)
        self.assertEqual(rpc.call_args.args[1], "eth_call")
        self.assertEqual(rpc.call_args.args[2][0]["data"], "0x70a08231" + "0" * 24 + "1" * 40)

    @patch("core.onchain_reconciliation.rpc_result", return_value={"value": 99})
    def test_solana_native_balance_uses_finalized_commitment(self, rpc):
        self.assertEqual(_solana_balance("So11111111111111111111111111111111111111112", "SOL"), 99)
        self.assertEqual(rpc.call_args.args[1], "getBalance")
        self.assertEqual(rpc.call_args.args[2][1]["commitment"], "finalized")

    @override_settings(CONI_EXPECTED_CHAIN_IDS=json.dumps({"base": "0x2105"}))
    @patch("core.onchain_reconciliation.ledger_balance", side_effect=[10, 10])
    @patch("core.onchain_reconciliation.LedgerAccount.objects")
    @patch("core.onchain_reconciliation._assets_for_chain", return_value=["ETH"])
    @patch("core.onchain_reconciliation.rpc_result", side_effect=["0x2105", "0x2a"])
    def test_mismatch_is_reported_without_mutating_ledger(self, rpc, assets, ledger_manager, balance):
        ledger_manager.filter.return_value.first.return_value = SimpleNamespace()
        wallet = SimpleNamespace(id="wallet-1", chain="base", address="0x" + "1" * 40, owner=SimpleNamespace(id="owner-1"))
        report = reconcile_bot_wallets(wallets=[wallet])
        self.assertEqual(report["checked"], 1)
        self.assertEqual(report["anomalies"][0]["type"], "bot-wallet-chain-ledger-mismatch")
        self.assertEqual(report["anomalies"][0]["observedBaseUnits"], "42")
        self.assertEqual(report["anomalies"][0]["ledgerBaseUnits"], "20")
        self.assertEqual(balance.call_count, 2)
