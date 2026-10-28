import unittest
from unittest.mock import patch

from signer.store import IntentStore, IntentStoreError


class _Result:
    def __init__(self, rowcount=0, row=None):
        self.rowcount = rowcount
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    def __init__(self):
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=()):
        sql = " ".join(str(statement).split()).upper()
        if sql.startswith("CREATE TABLE"):
            return _Result()
        if sql.startswith("INSERT INTO"):
            if self.row is not None:
                return _Result(0)
            self.row = (params[1], "pending", None)
            return _Result(1)
        if sql.startswith("SELECT"):
            return _Result(1, self.row)
        if sql.startswith("UPDATE SIGNER_INTENTS SET STATE = 'PENDING'"):
            if self.row and self.row[1] == "failed":
                self.row = (self.row[0], "pending", None)
            return _Result(1)
        if sql.startswith("UPDATE SIGNER_INTENTS SET STATE = 'SIGNED'"):
            self.row = (self.row[0], "signed", params[0])
            return _Result(1)
        if sql.startswith("UPDATE SIGNER_INTENTS SET STATE = 'FAILED'"):
            self.row = (self.row[0], "failed", None)
            return _Result(1)
        raise AssertionError(f"unexpected SQL: {statement}")


class IntentStoreTests(unittest.TestCase):
    def setUp(self):
        self.database = _FakeConnection()
        self.connect = patch("signer.store.psycopg.connect", return_value=self.database)
        self.connect.start()
        self.addCleanup(self.connect.stop)

    def test_new_reservation_is_marked_new(self):
        result = IntentStore("postgresql://db/test").reserve("intent-1234", "a" * 64)
        self.assertTrue(result["new"])
        self.assertEqual(result["state"], "pending")

    def test_existing_signed_reservation_is_replayable(self):
        store = IntentStore("postgresql://db/test")
        store.reserve("intent-1234", "a" * 64)
        self.database.row = ("a" * 64, "signed", {"state": "signed"})
        result = store.reserve("intent-1234", "a" * 64)
        self.assertFalse(result["new"])
        self.assertEqual(result["response"], {"state": "signed"})

    def test_failed_reservation_can_retry(self):
        store = IntentStore("postgresql://db/test")
        store.reserve("intent-1234", "a" * 64)
        self.database.row = ("a" * 64, "failed", None)
        result = store.reserve("intent-1234", "a" * 64)
        self.assertTrue(result["new"])
        self.assertEqual(result["state"], "pending")

    def test_key_cannot_be_reused_for_another_digest(self):
        store = IntentStore("postgresql://db/test")
        store.reserve("intent-1234", "a" * 64)
        with self.assertRaises(IntentStoreError):
            store.reserve("intent-1234", "b" * 64)


if __name__ == "__main__":
    unittest.main()
