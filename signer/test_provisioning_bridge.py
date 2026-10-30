import unittest

from .provisioning_bridge import (
    DisabledProvisioningProvider,
    ProvisioningBridge,
    ProvisioningError,
    ProvisionedWallet,
)


def request_payload(**overrides):
    value = {
        "request_id": "req-1",
        "owner_id": "owner-1",
        "chain": "base",
        "mode": "internal",
        "idempotency_key": "idem-1",
        "request_nonce": "nonce-1",
    }
    value.update(overrides)
    return value


class Provider:
    def __init__(self, **overrides):
        self.overrides = overrides

    def provision(self, request):
        value = {
            "request_id": request.request_id,
            "chain": request.chain,
            "address": "0x1111111111111111111111111111111111111111",
            "signer_key_ref": "mpc/base/key-1",
            "attestation": "attestation-1234567890",
            "request_nonce": request.request_nonce,
        }
        value.update(self.overrides)
        return ProvisionedWallet.from_mapping(value)


class ProvisioningBridgeTests(unittest.TestCase):
    def test_disabled_provider_fails_closed(self):
        with self.assertRaises(ProvisioningError):
            DisabledProvisioningProvider().provision(None)

    def test_mismatch_never_attests(self):
        calls = []
        bridge = ProvisioningBridge(
            Provider(request_nonce="wrong"),
            lambda: [request_payload()],
            lambda **kwargs: calls.append(kwargs),
        )
        with self.assertRaises(ProvisioningError):
            bridge.run_once()
        self.assertEqual(calls, [])

    def test_valid_provider_result_is_attested(self):
        calls = []
        bridge = ProvisioningBridge(
            Provider(),
            lambda: [request_payload()],
            lambda **kwargs: calls.append(kwargs) or {"accepted": True},
        )
        result = bridge.run_once()
        self.assertEqual(result[0]["request_id"], "req-1")
        self.assertEqual(calls[0]["address"], "0x1111111111111111111111111111111111111111")

    def test_private_material_is_rejected(self):
        with self.assertRaises(ProvisioningError):
            ProvisionedWallet.from_mapping(
                {
                    "request_id": "req-1",
                    "chain": "base",
                    "address": "0x1111111111111111111111111111111111111111",
                    "signer_key_ref": "mpc/base/key-1",
                    "attestation": "attestation-1234567890",
                    "request_nonce": "nonce-1",
                    "private_key": "must-never-cross-boundary",
                }
            )


if __name__ == "__main__":
    unittest.main()
