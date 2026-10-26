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

    def test_total_value_is_capped_across_all_steps(self):
        policy = SignerPolicy(
            mode="live",
            audit_approved=True,
            allowed_recipients={"base": [
                "0x1111111111111111111111111111111111111111",
                "0x2222222222222222222222222222222222222222",
            ]},
            max_value_base_units=1000,
            max_total_value_base_units=1000,
        )
        expires = int(time.time()) + 30
        with self.assertRaises(PolicyError):
            policy.validate(intent(expiresAt=expires, steps=[
                {"chain": "base", "to": "0x1111111111111111111111111111111111111111", "valueBaseUnits": "600", "calldataHash": "c" * 64, "expiresAt": expires},
                {"chain": "base", "to": "0x2222222222222222222222222222222222222222", "valueBaseUnits": "500", "calldataHash": "d" * 64, "expiresAt": expires},
            ]))

    def test_solana_allowlist_is_case_sensitive(self):
        address = "Abcdefghijkmnpqrstuvwxyz123456789"
        policy = SignerPolicy(
            mode="live",
            audit_approved=True,
            allowed_recipients={"solana": [address]},
            max_value_base_units=1000,
        )
        expires = int(time.time()) + 30
        policy.validate(intent(expiresAt=expires, steps=[{
            "chain": "solana",
            "to": address,
            "valueBaseUnits": "100",
            "calldataHash": "c" * 64,
            "expiresAt": expires,
        }]))
        with self.assertRaises(PolicyError):
            policy.validate(intent(expiresAt=expires, steps=[{
                "chain": "solana",
                "to": address.swapcase(),
                "valueBaseUnits": "100",
                "calldataHash": "c" * 64,
                "expiresAt": expires,
            }]))


if __name__ == "__main__":
    unittest.main()
