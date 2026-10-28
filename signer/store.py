"""Durable signer-intent idempotency store.

Only intent digest, lifecycle state, and the adapter response are persisted.
Private keys and seed material are never part of this schema.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


class IntentStoreError(RuntimeError):
    pass


class IntentStore:
    def __init__(self, dsn):
        if not dsn or not str(dsn).lower().startswith(("postgres://", "postgresql://")):
            raise ValueError("SIGNER_DATABASE_URL must be PostgreSQL")
        self.dsn = str(dsn)
        self._ensure_schema()

    def _ensure_schema(self):
        try:
            with psycopg.connect(self.dsn) as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS signer_intents (
                        idempotency_key VARCHAR(128) PRIMARY KEY,
                        intent_digest CHAR(64) NOT NULL,
                        state VARCHAR(24) NOT NULL,
                        response JSONB,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                ).rowcount == 1
        except psycopg.Error as exc:
            raise IntentStoreError("signer intent store is unavailable") from exc

    def reserve(self, idempotency_key, intent_digest):
        try:
            with psycopg.connect(self.dsn) as connection:
                inserted = connection.execute(
                    """
                    INSERT INTO signer_intents (idempotency_key, intent_digest, state)
                    VALUES (%s, %s, 'pending')
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """,
                    (idempotency_key, intent_digest),
                ).rowcount == 1
                row = connection.execute(
                    "SELECT intent_digest, state, response FROM signer_intents WHERE idempotency_key = %s FOR UPDATE",
                    (idempotency_key,),
                ).fetchone()
                if not row:
                    raise IntentStoreError("signer intent reservation failed")
                existing_digest, state, response = row
                if existing_digest != intent_digest:
                    raise IntentStoreError("idempotency key is bound to another intent")
                if not inserted and state == "failed":
                    connection.execute(
                        "UPDATE signer_intents SET state = 'pending', response = NULL, updated_at = CURRENT_TIMESTAMP WHERE idempotency_key = %s AND state = 'failed'",
                        (idempotency_key,),
                    )
                    state, response, inserted = "pending", None, True
                return {"digest": existing_digest, "state": state, "response": response, "new": inserted}
        except IntentStoreError:
            raise
        except psycopg.Error as exc:
            raise IntentStoreError("signer intent store is unavailable") from exc

    def mark_signed(self, idempotency_key, intent_digest, response):
        try:
            with psycopg.connect(self.dsn) as connection:
                updated = connection.execute(
                    """
                    UPDATE signer_intents
                    SET state = 'signed', response = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE idempotency_key = %s AND intent_digest = %s AND state = 'pending'
                    """,
                    (Jsonb(response), idempotency_key, intent_digest),
                ).rowcount
                if updated != 1:
                    raise IntentStoreError("signer intent could not be finalized")
        except IntentStoreError:
            raise
        except psycopg.Error as exc:
            raise IntentStoreError("signer intent store is unavailable") from exc

    def mark_failed(self, idempotency_key, intent_digest):
        try:
            with psycopg.connect(self.dsn) as connection:
                connection.execute(
                    "UPDATE signer_intents SET state = 'failed', updated_at = CURRENT_TIMESTAMP WHERE idempotency_key = %s AND intent_digest = %s AND state = 'pending'",
                    (idempotency_key, intent_digest),
                )
        except psycopg.Error as exc:
            raise IntentStoreError("signer intent store is unavailable") from exc
