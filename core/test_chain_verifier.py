import os
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from django.utils import timezone

from .chain_verifier import VerificationError, _verify_evm, observation_is_stale


class ChainVerifierTests(SimpleTestCase):
    @override_settings(CONI_DEPOSIT_MAX_AGE_MINUTES=30)
    def test_observation_age_window_rejects_old_discoveries(self):
        observation = SimpleNamespace(observed_at=timezone.now() - timedelta(minutes=31))
        self.assertTrue(observation_is_stale(observation))

    @override_settings(CONI_DEPOSIT_MAX_AGE_MINUTES=30)
    def test_observation_age_window_accepts_recent_discoveries(self):
        observation = SimpleNamespace(observed_at=timezone.now() - timedelta(minutes=29))
        self.assertFalse(observation_is_stale(observation))

    @override_settings(
        CONI_RPC_URLS='{"base":"https://rpc.example"}',
        CONI_EXPECTED_CHAIN_IDS='{"base":"0x2105"}',
        CONI_DEPOSIT_CONFIRMATIONS=2,
    )
    def test_evm_transfer_is_credited_after_finality(self):
        observation = SimpleNamespace(
            chain="base",
            tx_hash="0x" + "1" * 64,
            sender="0x" + "2" * 40,
            destination="0x" + "3" * 40,
            amount_base_units=Decimal("100"),
        )
        responses = iter([
            "0x2105",
            {"from": observation.sender, "to": observation.destination, "value": "0x64"},
            {"status": "0x1", "blockNumber": "0xa", "blockHash": "0x" + "4" * 64},
            "0xb",
        ])
        with patch("core.chain_verifier._rpc", side_effect=lambda *args: next(responses)) as rpc:
            with patch("core.chain_verifier._confirm_and_credit") as credit:
                self.assertEqual(_verify_evm(observation), "credited")
                credit.assert_called_once()
                self.assertEqual(rpc.call_count, 4)

    @override_settings(
        CONI_RPC_URLS='{"base":"https://rpc.example"}',
        CONI_EXPECTED_CHAIN_IDS='{"base":"0x2105"}',
    )
    def test_evm_sender_mismatch_is_rejected(self):
        observation = SimpleNamespace(
            chain="base",
            tx_hash="0x" + "1" * 64,
            sender="0x" + "2" * 40,
            destination="0x" + "3" * 40,
            amount_base_units=Decimal("100"),
        )
        responses = iter([
            "0x2105",
            {"from": "0x" + "5" * 40, "to": observation.destination, "value": "0x64"},
        ])
        with patch("core.chain_verifier._rpc", side_effect=lambda *args: next(responses)):
            with self.assertRaises(VerificationError):
                _verify_evm(observation)
