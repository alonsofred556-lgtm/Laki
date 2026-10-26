import hashlib
import hmac
import json
import unittest
from unittest.mock import Mock

from .django_client import DjangoSignerClient, DjangoSignerClientError


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self.payload


class DjangoSignerClientTests(unittest.TestCase):
    def test_pull_request_is_hmac_signed_without_private_material(self):
        secret = "s" * 32
        opener = Mock(return_value=FakeResponse({"requests": []}))
        client = DjangoSignerClient("https://coni.internal", secret, clock=lambda: 1700000000, opener=opener)

        self.assertEqual(client.pull_provision_requests(limit=3), [])
        request = opener.call_args.args[0]
        expected = hmac.new(secret.encode(), b"1700000000.", hashlib.sha256).hexdigest()
        self.assertEqual(request.get_header("X-coni-signer-timestamp"), "1700000000")
        self.assertEqual(request.get_header("X-coni-signer-signature"), expected)
        self.assertEqual(request.full_url, "https://coni.internal/api/internal/signer/bot-wallet-provision-requests?limit=3")

    def test_attestation_serializes_only_public_fields(self):
        opener = Mock(return_value=FakeResponse({"ok": True, "state": "active"}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, clock=lambda: 1700000000, opener=opener)

        result = client.attest_bot_wallet(
            provision_request_id="00000000-0000-4000-8000-000000000001",
            bot_wallet_id="00000000-0000-4000-8000-000000000002",
            chain="base",
            address="0x1111111111111111111111111111111111111111",
            signer_key_ref="hsm:key:wallet-1",
            attestation_hash="a" * 64,
        )

        self.assertEqual(result["state"], "active")
        request = opener.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(set(body), {"provisionRequestId", "botWalletId", "chain", "address", "signerKeyRef", "attestationHash"})
        self.assertNotIn("privateKey", body)
        self.assertNotIn("seed", body)

    def test_invalid_provider_response_fails_closed(self):
        opener = Mock(return_value=FakeResponse({"requests": [{"chain": "base", "privateKey": "secret"}]}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)

        with self.assertRaises(DjangoSignerClientError):
            client.pull_provision_requests()

    def test_django_error_object_fails_closed(self):
        opener = Mock(return_value=FakeResponse({"error": "unauthorized"}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)

        with self.assertRaises(DjangoSignerClientError):
            client.pull_provision_requests()

    def test_invalid_attestation_is_rejected_before_network(self):
        opener = Mock(return_value=FakeResponse({"ok": True}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)

        with self.assertRaises(ValueError):
            client.attest_bot_wallet(
                provision_request_id="00000000-0000-4000-8000-000000000001",
                bot_wallet_id="00000000-0000-4000-8000-000000000002",
                chain="base",
                address="not-an-address",
                signer_key_ref="hsm:key:wallet-1",
                attestation_hash="a" * 64,
            )
        opener.assert_not_called()


if __name__ == "__main__":
    unittest.main()
