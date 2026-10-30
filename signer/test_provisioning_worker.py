import unittest
from types import SimpleNamespace
from unittest.mock import patch

from .provisioning_worker import _session_nonce, run_once


class FakeClient:
    def __init__(self, request):
        self.request = request
        self.attested = []

    def pull_provision_requests(self, *, limit):
        self.limit = limit
        return [self.request]

    def attest_bot_wallet(self, **kwargs):
        self.attested.append(kwargs)
        return {"state": "active"}


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def provision(self, payload, nonce):
        self.calls.append((payload, nonce))
        return {
            "provisionRequestId": payload["provisionRequestId"],
            "botWalletId": payload["botWalletId"],
            "chain": payload["chain"],
            "address": "0x1111111111111111111111111111111111111111",
            "signerKeyRef": "mpc/key/base-1",
            "attestationHash": "a" * 64,
            "sessionNonce": nonce,
        }


class ProvisioningWorkerTests(unittest.TestCase):
    def _request(self):
        return SimpleNamespace(
            provision_request_id="00000000-0000-4000-8000-000000000001",
            bot_wallet_id="00000000-0000-4000-8000-000000000002",
            owner_id="00000000-0000-4000-8000-000000000003",
            chain="base",
            mode="internal-bot",
            source_address=None,
            attempt=1,
        )

    def test_nonce_is_stable_for_idempotent_retry(self):
        with patch.dict("os.environ", {"CONI_SIGNER_PROVISION_SESSION_KEY": "k" * 32}, clear=False):
            self.assertEqual(_session_nonce("request-1"), _session_nonce("request-1"))
            self.assertNotEqual(_session_nonce("request-1"), _session_nonce("request-2"))

    def test_worker_attests_only_provider_result(self):
        client = FakeClient(self._request())
        adapter = FakeAdapter()
        with patch.dict("os.environ", {
            "SIGNER_PROVISIONING_ENABLED": "true",
            "CONI_SIGNER_PROVISION_SESSION_KEY": "k" * 32,
        }, clear=False):
            result = run_once(client=client, adapter=adapter, limit=1)
        self.assertEqual(result, [{"provisionRequestId": self._request().provision_request_id, "state": "attested"}])
        self.assertEqual(len(client.attested), 1)
        self.assertEqual(client.attested[0]["chain"], "base")

    def test_worker_is_disabled_by_default(self):
        with patch.dict("os.environ", {"SIGNER_PROVISIONING_ENABLED": "false"}, clear=False):
            with self.assertRaises(RuntimeError):
                run_once(client=FakeClient(self._request()), adapter=FakeAdapter())


if __name__ == "__main__":
    unittest.main()
