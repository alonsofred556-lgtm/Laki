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

    def test_pull_bot_transfer_intents_validates_public_intent_only(self):
        opener = Mock(return_value=FakeResponse({"transfers": [{
            "transferId": "00000000-0000-4000-8000-000000000001",
            "botWalletId": "00000000-0000-4000-8000-000000000002",
            "ownerId": "00000000-0000-4000-8000-000000000003",
            "chain": "base",
            "asset": "ETH",
            "direction": "withdrawal",
            "amountBaseUnits": "100",
            "destination": "0x1111111111111111111111111111111111111111",
            "signerKeyRef": "hsm:key:wallet-1",
            "attempt": 1,
            "leaseUntil": "2026-10-27T08:00:00+00:00",
        }]}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)
        result = client.pull_bot_transfer_intents(limit=1)
        self.assertEqual(result[0].amount_base_units, "100")
        self.assertEqual(result[0].direction, "withdrawal")
        body = json.loads(opener.call_args.args[0].data.decode("utf-8")) if opener.call_args.args[0].data else None
        self.assertIsNone(body)

    def test_bot_transfer_event_rejects_invalid_chain_transaction_before_network(self):
        opener = Mock(return_value=FakeResponse({"ok": True}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)
        with self.assertRaises(ValueError):
            client.record_bot_transfer_event(
                transfer_id="00000000-0000-4000-8000-000000000001",
                chain="base",
                event="confirmed",
                idempotency_key="bot-event-1",
                tx_hash="not-a-hash",
            )
        opener.assert_not_called()

    def test_pull_withdrawal_intents_validates_nonce_and_key_reference(self):
        opener = Mock(return_value=FakeResponse({"withdrawals": [{
            "withdrawalId": "00000000-0000-4000-8000-000000000001",
            "ownerId": "00000000-0000-4000-8000-000000000003",
            "chain": "base",
            "asset": "ETH",
            "amountBaseUnits": "100",
            "destination": "0x1111111111111111111111111111111111111111",
            "nonce": "withdrawal-nonce-1",
            "signerKeyRef": "hsm:key:withdrawal-1",
            "attempt": 1,
            "leaseUntil": "2026-10-27T08:00:00+00:00",
        }]}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)
        result = client.pull_withdrawal_intents(limit=1)
        self.assertEqual(result[0].nonce, "withdrawal-nonce-1")
        self.assertEqual(result[0].signer_key_ref, "hsm:key:withdrawal-1")

    def test_withdrawal_event_rejects_invalid_transaction_before_network(self):
        opener = Mock(return_value=FakeResponse({"ok": True}))
        client = DjangoSignerClient("https://coni.internal", "s" * 32, opener=opener)
        with self.assertRaises(ValueError):
            client.record_withdrawal_event(
                withdrawal_id="00000000-0000-4000-8000-000000000001",
                chain="base",
                event="confirmed",
                idempotency_key="withdrawal-event-1",
                tx_hash="invalid",
            )
        opener.assert_not_called()

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
