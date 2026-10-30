import json
import os
from unittest.mock import patch

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "coni_backend.settings")

import django
from django.test import SimpleTestCase, override_settings

django.setup()

from .rpc import RpcError, forward, rpc_endpoints, validate_call


class RpcGatewayTests(SimpleTestCase):
    @override_settings(
        CONI_RPC_URLS=json.dumps({"base": ["https://base-a.internal", "https://base-b.internal"]}),
        CONI_SOLANA_RPC_URL="https://solana-a.internal",
    )
    def test_endpoints_support_failover_lists_and_solana(self):
        self.assertEqual(rpc_endpoints("base"), ["https://base-a.internal", "https://base-b.internal"])
        self.assertEqual(rpc_endpoints("solana"), ["https://solana-a.internal"])

    @override_settings(CONI_RPC_URLS='{"base":["https://base-a.internal","https://base-b.internal"]}')
    @patch("core.rpc._request")
    def test_transport_failure_fails_over_without_exposing_endpoint(self, request):
        request.side_effect = [RpcError("chain RPC provider unavailable"), {"jsonrpc": "2.0", "id": 7, "result": "0x2105"}]
        self.assertEqual(forward("base", "eth_chainId", [], 7)["result"], "0x2105")
        request.assert_called()

    @override_settings(CONI_RPC_URLS='{"base":"https://base-a.internal"}')
    @patch("core.rpc._request", return_value={"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad params"}})
    def test_provider_errors_are_normalized(self, request):
        payload = forward("base", "eth_chainId", [])
        self.assertEqual(payload["error"]["code"], -32000)
        self.assertNotIn("bad params", payload["error"]["message"])

    def test_unknown_methods_and_unsigned_transactions_are_rejected(self):
        with self.assertRaises(RpcError):
            validate_call("base", "personal_importRawKey", [])
        with self.assertRaises(RpcError):
            validate_call("base", "eth_sendRawTransaction", ["0x1234"])
        with self.assertRaises(RpcError):
            validate_call("solana", "sendTransaction", ["not-a-transaction"])
