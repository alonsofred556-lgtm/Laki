import json
import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .asset_attestation import verify_asset_registry


class AssetAttestationTests(SimpleTestCase):
    @override_settings(
        CONI_TOKEN_REGISTRY=json.dumps({"ZGAP": {"base": {"address": "0x" + "1" * 40, "decimals": 9}, "solana": {"address": "Abcdefghijkmnpqrstuvwxyz123456789", "decimals": 9}}}),
        CONI_EXPECTED_CHAIN_IDS=json.dumps({"base": "0x2105"}),
    )
    @patch("core.asset_attestation.rpc_result")
    def test_evm_and_solana_decimals_must_match_registry(self, rpc):
        def response(chain, method, params):
            if chain == "base" and method == "eth_chainId":
                return "0x2105"
            if chain == "base" and method == "eth_call":
                return "0x9"
            if chain == "solana" and method == "getAccountInfo":
                return {"value": {"data": {"parsed": {"info": {"decimals": 9}}}}}
            raise AssertionError((chain, method, params))

        rpc.side_effect = response
        report = verify_asset_registry()
        self.assertTrue(report["ready"])
        self.assertEqual({item["status"] for item in report["assets"]}, {"verified"})

    @override_settings(
        CONI_TOKEN_REGISTRY=json.dumps({"ZGAP": {"base": {"address": "0x" + "1" * 40, "decimals": 9}}}),
        CONI_EXPECTED_CHAIN_IDS=json.dumps({"base": "0x2105"}),
    )
    @patch("core.asset_attestation.rpc_result", side_effect=["0x2105", "0x8"])
    def test_decimal_mismatch_is_not_ready(self, rpc):
        report = verify_asset_registry()
        self.assertFalse(report["ready"])
        self.assertEqual(report["assets"][0]["status"], "decimal-mismatch")

    @override_settings(
        CONI_TOKEN_REGISTRY=json.dumps({"ZGAP": {"base": {"address": "0x" + "1" * 40, "decimals": None}}}),
        CONI_EXPECTED_CHAIN_IDS=json.dumps({"base": "0x2105"}),
    )
    @patch("core.asset_attestation.rpc_result", return_value="0x2105")
    def test_missing_server_decimals_stays_gated(self, rpc):
        report = verify_asset_registry()
        self.assertFalse(report["ready"])
        self.assertEqual(report["assets"][0]["status"], "missing-configured-decimals")
        rpc.assert_called_once()
