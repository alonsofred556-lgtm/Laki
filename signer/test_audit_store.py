import unittest

from .audit_store import GENESIS_HASH, event_hash, verify_rows


class AuditStoreTests(unittest.TestCase):
    def test_chain_verifies_in_sequence(self):
        key = "k" * 32
        first = {"at": "2026-10-27T00:00:00+00:00", "action": "sign-intent", "result": "accepted"}
        first_hash = event_hash(GENESIS_HASH, first, key)
        second = {"at": "2026-10-27T00:00:01+00:00", "action": "sign-intent", "result": "signed"}
        second_hash = event_hash(first_hash, second, key)
        result = verify_rows([(1, GENESIS_HASH, first_hash, first), (2, first_hash, second_hash, second)], key)
        self.assertTrue(result["valid"])
        self.assertEqual(result["checked"], 2)
        self.assertEqual(result["lastHash"], second_hash)

    def test_tampered_payload_is_rejected(self):
        key = "k" * 32
        payload = {"action": "sign-intent", "result": "accepted"}
        digest = event_hash(GENESIS_HASH, payload, key)
        result = verify_rows([(1, GENESIS_HASH, digest, {"action": "sign-intent", "result": "rejected"})], key)
        self.assertFalse(result["valid"])
        self.assertEqual(result["failedSequence"], 1)

    def test_missing_predecessor_is_rejected(self):
        key = "k" * 32
        payload = {"action": "sign-intent", "result": "accepted"}
        digest = event_hash("f" * 64, payload, key)
        result = verify_rows([(2, "f" * 64, digest, payload)], key)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "previous hash mismatch")


if __name__ == "__main__":
    unittest.main()
