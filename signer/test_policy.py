import time
import unittest

from .policy import PolicyError, SignerPolicy


def intent(**overrides):
    payload = {
        "idempotencyKey": "policy-test-001",
        "quoteHash": "a" * 64,
        "simulationHash": "b" * 64,
        "expiresAt": int(time.time()) + 30,
        "steps": [{
            "chain": "base",
            "to": "0x1111111111111111111111111111111111111111",
            "valueBaseUnits": "100",
            "calldataHash": "c" * 64,
            "expiresAt": int(time.time()) + 30,
        }],
    }
    payload.update(overrides)
    payload["steps"][0]["expiresAt"] = payload["expiresAt"]
    return payload


class SignerPolicyTests(unittest.TestCase):
    def test_live_policy_accepts_string_allowlist_and_canonicalizes(self):
        policy = SignerPolicy(
            mode="live",
            audit_approved=True,
            allowed_recipients={"base": "0x1111111111111111111111111111111111111111"},
            max_value_base_units="1000",
        )
        validated = policy.validate(intent())
        self.assertEqual(len(validated["digest"]), 64)

    def test_live_policy_rejects_unbounded_maximum(self):
        policy = SignerPolicy(
            mode="live",
            audit_approved=True,
            allowed_recipients={"base": ["0x1111111111111111111111111111111111111111"]},
            max_value_base_units=0,
        )
        with self.assertRaises(PolicyError):
            policy.validate(intent())

    def test_evm_step_cannot_use_a_solana_address(self):
        policy = SignerPolicy(
            mode="live",
            audit_approved=True,
            allowed_recipients={"base": ["11111111111111111111111111111111"]},
            max_value_base_units=1000,
        )
        with self.assertRaises(PolicyError):
            policy.validate(intent(steps=[{
                "chain": "base",
                "to": "11111111111111111111111111111111",
                "valueBaseUnits": "100",
                "calldataHash": "c" * 64,
                "expiresAt": int(time.time()) + 30,
            }]))


if __name__ == "__main__":
    unittest.main()
