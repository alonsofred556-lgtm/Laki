"""Durable, tamper-evident audit chain for the private custody service.

The signer never stores private keys here. It stores only bounded decision
metadata, intent digests, and adapter results. Every row commits the hash of
the previous row so an operator can export and independently verify the
sequence without trusting application logs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import psycopg
from psycopg.types.json import Jsonb


GENESIS_HASH = "0" * 64


class AuditStoreError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def event_hash(previous_hash, payload, key):
    """Return a keyed hash bound to both the event and its predecessor."""

    message = _canonical({"previousHash": previous_hash, "payload": payload}).encode("utf-8")
    return hmac.new(key.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_rows(rows, key):
    """Verify rows returned in ascending sequence order."""

    previous = GENESIS_HASH
    checked = 0
    for sequence, previous_hash, stored_hash, payload in rows:
        if previous_hash != previous:
            return {"valid": False, "checked": checked, "failedSequence": sequence, "reason": "previous hash mismatch"}
        expected = event_hash(previous_hash, payload, key)
        if not hmac.compare_digest(expected, stored_hash):
            return {"valid": False, "checked": checked, "failedSequence": sequence, "reason": "event hash mismatch"}
        previous = stored_hash
        checked += 1
    return {"valid": True, "checked": checked, "lastHash": previous}


class AuditStore:
    """PostgreSQL-backed append-only audit chain.

    The service account should be restricted by deployment policy to the audit
    schema/table and should not be granted UPDATE or DELETE in production.
    Schema creation is retained for first boot and local tests.
    """

    def __init__(self, dsn, key):
        if not dsn or not str(dsn).lower().startswith(("postgres://", "postgresql://")):
            raise ValueError("SIGNER_AUDIT_DATABASE_URL must be PostgreSQL")
        if not isinstance(key, str) or len(key) < 32:
            raise ValueError("SIGNER_AUDIT_HMAC_KEY must contain at least 32 characters")
        self.dsn = str(dsn)
        self.key = key
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            with psycopg.connect(self.dsn) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signer_audit_events (
                        sequence BIGSERIAL PRIMARY KEY,
                        previous_hash CHAR(64) NOT NULL,
                        event_hash CHAR(64) NOT NULL UNIQUE,
                        payload JSONB NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute("CREATE INDEX IF NOT EXISTS signer_audit_events_created ON signer_audit_events (created_at)")
        except psycopg.Error as exc:
            raise AuditStoreError("signer audit store is unavailable") from exc

    def append(self, action, result, *, digest=None, idempotency_key=None, reason=None, metadata=None):
        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "at": now,
            "service": "coni-signer",
            "action": str(action)[:96],
            "result": str(result)[:32],
            "intentDigest": str(digest)[:128] if digest else None,
            "idempotencyKey": str(idempotency_key)[:128] if idempotency_key else None,
            "reason": str(reason)[:255] if reason else None,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        try:
            with psycopg.connect(self.dsn) as connection:
                # Serialize tail selection so concurrent requests cannot point
                # at the same predecessor.
                connection.execute("SELECT pg_advisory_xact_lock(739104219)")
                row = connection.execute("SELECT event_hash FROM signer_audit_events ORDER BY sequence DESC LIMIT 1").fetchone()
                previous_hash = row[0] if row else GENESIS_HASH
                current_hash = event_hash(previous_hash, payload, self.key)
                connection.execute(
                    "INSERT INTO signer_audit_events (previous_hash, event_hash, payload) VALUES (%s, %s, %s)",
                    (previous_hash, current_hash, Jsonb(payload)),
                )
        except psycopg.Error as exc:
            raise AuditStoreError("signer audit append failed") from exc
        return {"eventHash": current_hash, "previousHash": previous_hash, "payload": payload}

    def verify(self):
        try:
            with psycopg.connect(self.dsn) as connection:
                rows = connection.execute(
                    "SELECT sequence, previous_hash, event_hash, payload FROM signer_audit_events ORDER BY sequence ASC"
                ).fetchall()
        except psycopg.Error as exc:
            raise AuditStoreError("signer audit verification failed") from exc
        return verify_rows(rows, self.key)
