import json
import unittest
from unittest.mock import Mock

from .adapter import AdapterError, HsmMpcHttpAdapter


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class AdapterProvisioningTests(unittest.TestCase):
    def _request(self):
        return {
            "provisionRequestId": "00000000-0000-4000-8000-000000000001",
            "botWalletId": "00000000-0000-4000-8000-000000000002",
            "ownerId": "00000000-0000-4000-8000-000000000003",
            "chain": "base",
            "mode": "internal-bot",
            "sourceAddress": None,
            "attempt": 1,
            "sessionNonce": "N" * 43,
        }

    def test_provision_response_is_nonce_and_request_bound(self):
        request = self._request()
        opener = Mock(return_value=FakeResponse({
            "provisionRequestId": request["provisionRequestId"],
            "botWalletId": request["botWalletId"],
            "chain": "base",
            "address": "0x1111111111111111111111111111111111111111",
            "signerKeyRef": "mpc/key/base-1",
            "attestationHash": "a" * 64,
            "sessionNonce": request["sessionNonce"],
        }))
        adapter = HsmMpcHttpAdapter(
            url="https://adapter.internal",
            shared_secret="s" * 32,
            clock=lambda: 1700000000,
            opener=opener,
        )
        result = adapter.provision(request, request["sessionNonce"])
        self.assertEqual(result["chain"], "base")
        sent = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("privateKey", sent)
        self.assertIn("x-coni-adapter-signature", {key.lower(): value for key, value in opener.call_args.args[0].headers.items()})

    def test_private_provider_response_is_rejected(self):
        request = self._request()
        opener = Mock(return_value=FakeResponse({
            "provisionRequestId": request["provisionRequestId"],
            "botWalletId": request["botWalletId"],
            "chain": "base",
            "address": "0x1111111111111111111111111111111111111111",
            "signerKeyRef": "mpc/key/base-1",
            "attestationHash": "a" * 64,
            "sessionNonce": request["sessionNonce"],
            "privateKey": "never-returned",
        }))
        adapter = HsmMpcHttpAdapter(url="https://adapter.internal", opener=opener)
        with self.assertRaises(AdapterError):
            adapter.provision(request, request["sessionNonce"])

    def test_wrong_nonce_is_rejected_before_network(self):
        opener = Mock()
        adapter = HsmMpcHttpAdapter(url="https://adapter.internal", opener=opener)
        with self.assertRaises(AdapterError):
            adapter.provision(self._request(), "wrong-nonce")
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
