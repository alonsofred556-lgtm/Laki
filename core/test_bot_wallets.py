import os
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import RequestFactory, SimpleTestCase, override_settings

django.setup()

from .bot_wallets import accessible_bot_chains, bot_transfer_chain, bot_wallet_automation_status, bot_wallet_payload, chain_accessible, select_bot_wallet, withdrawal_limits
from .views import bot_wallet_view


class BotWalletAccessTests(SimpleTestCase):
    @override_settings(CONI_BOT_WITHDRAWAL_LIMITS='{"base:ETH":{"perTransferBaseUnits":"10","dailyBaseUnits":"25"}}')
    def test_withdrawal_limits_are_explicit_base_unit_caps(self):
        self.assertEqual(withdrawal_limits("ETH", "base"), {"perTransferBaseUnits": 10, "dailyBaseUnits": 25})
        self.assertIsNone(withdrawal_limits("SOL", "base"))

    @override_settings(CONI_BOT_WITHDRAWAL_LIMITS='{"base:ETH":{"perTransferBaseUnits":"25","dailyBaseUnits":"10"}}')
    def test_invalid_withdrawal_limit_order_is_rejected(self):
        self.assertIsNone(withdrawal_limits("ETH", "base"))

    def test_summary_prefers_persisted_internal_mode(self):
        wallets = [
            SimpleNamespace(id="external", chain="base", mode="external-bot"),
            SimpleNamespace(id="internal", chain="base", mode="internal-bot"),
        ]
        self.assertEqual(select_bot_wallet(wallets, chain="base", mode="internal-bot").id, "internal")

    def test_summary_does_not_cross_custody_modes_when_mode_is_not_provisioned(self):
        wallets = [SimpleNamespace(id="external", chain="base", mode="external-bot")]
        self.assertIsNone(select_bot_wallet(wallets, chain="base", mode="internal-bot"))

    def test_unprovisioned_wallet_is_not_labeled_as_audited(self):
        wallet = SimpleNamespace(
            id="pending", chain="base", address="", mode="external-bot",
            custody="audited-signer", status="awaiting-signer-provision",
            signer_key_ref="", source_address="0x" + "1" * 40,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.assertEqual(bot_wallet_payload(wallet)["custody"], "pending-attestation")

    @override_settings(CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS='{"base":"100"}')
    def test_active_funded_wallet_is_ready_for_planner_scan(self):
        wallet = SimpleNamespace(
            chain="base", address="0x" + "2" * 40, signer_key_ref="hsm/base/key",
            status="active",
        )
        result = bot_wallet_automation_status(
            wallet,
            user_control=SimpleNamespace(enabled=True, paused=False),
            global_control=SimpleNamespace(enabled=True, paused=False),
            native_balance="100",
        )
        self.assertEqual(result["state"], "ready-to-scan")
        self.assertTrue(result["plannerEligible"])
        self.assertFalse(result["autoStartAfterFunding"])

    @override_settings(CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS='{"base":"100"}')
    def test_attested_wallet_exposes_auto_start_after_funding_state(self):
        wallet = SimpleNamespace(
            chain="base", address="0x" + "2" * 40, signer_key_ref="hsm/base/key",
            status="active",
        )
        result = bot_wallet_automation_status(
            wallet,
            user_control=SimpleNamespace(enabled=True, paused=False),
            global_control=SimpleNamespace(enabled=True, paused=False),
            native_balance="0",
        )
        self.assertEqual(result["state"], "awaiting-funding")
        self.assertTrue(result["autoStartAfterFunding"])
        self.assertFalse(result["plannerEligible"])

    def test_selected_wallet_id_cannot_cross_mode_or_chain(self):
        wallets = [
            SimpleNamespace(id="external", chain="base", mode="external-bot"),
            SimpleNamespace(id="internal", chain="base", mode="internal-bot"),
        ]
        self.assertEqual(select_bot_wallet(wallets, chain="base", mode="external-bot", selected_wallet_id="internal").id, "external")
        self.assertIsNone(select_bot_wallet(wallets, chain="ethereum", mode="internal-bot", selected_wallet_id="internal"))

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

    def test_evm_identity_does_not_expose_solana_bot_wallet(self):
        self.assertNotIn("solana", accessible_bot_chains(SimpleNamespace(chain="base")))
        self.assertIn("base", accessible_bot_chains(SimpleNamespace(chain="base")))

    def test_solana_identity_only_exposes_solana_bot_wallet(self):
        self.assertEqual(accessible_bot_chains(SimpleNamespace(chain="solana")), ("solana",))

    def test_solana_identity_cannot_select_evm_bot_network(self):
        identity = SimpleNamespace(chain="solana")
        self.assertTrue(chain_accessible(identity, "solana"))
        self.assertFalse(chain_accessible(identity, "base"))

    def test_evm_identity_cannot_select_solana_bot_network(self):
        identity = SimpleNamespace(chain="ethereum")
        self.assertFalse(chain_accessible(identity, "solana"))

    def test_bot_wallet_endpoint_combines_signed_evm_and_solana_sessions(self):
        factory = RequestFactory()
        evm = SimpleNamespace(id="evm-id", chain="base", address="0x" + "1" * 40)
        solana = SimpleNamespace(id="sol-id", chain="solana", address="Abcdefghijkmnpqrstuvwxyz123456789")
        evm_summary = {"wallets": [{"id": "evm-wallet", "chain": "base", "mode": "external-bot"}]}
        solana_summary = {"wallets": [{"id": "sol-wallet", "chain": "solana", "mode": "external-bot"}]}
        with patch("core.views.require_session", return_value=evm), patch("core.views.session_identities", return_value=[evm, solana]), patch("core.views.bot_wallet_summary", side_effect=[evm_summary, solana_summary]):
            response = bot_wallet_view(factory.get("/api/bot-wallet"))
        payload = json.loads(response.content)
        self.assertEqual({item["chain"] for item in payload["wallets"]}, {"base", "solana"})
        self.assertEqual({item["walletIdentityId"] for item in payload["wallets"]}, {"evm-id", "sol-id"})
