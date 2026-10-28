"""Structured signer audit records; ship stdout to an immutable log sink."""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from .audit_store import AuditStore, AuditStoreError

logger = logging.getLogger("coni.signer.audit")


def record(action, result, *, digest=None, idempotency_key=None, reason=None):
    payload = {
        "at": datetime.now(timezone.utc).isoformat(),
        "service": "coni-signer",
        "action": action,
        "result": result,
        "intentDigest": digest,
        "idempotencyKey": idempotency_key,
        "reason": reason,
    }
    payload["recordHash"] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def record_durable(action, result, *, digest=None, idempotency_key=None, reason=None, metadata=None):
    """Write a custody decision to the private chained audit store.

    This is separate from ``record`` because stdout logging is useful for
    operations but is not a sufficient signing control.  Callers in live mode
    must propagate ``AuditStoreError`` and refuse to sign when this append
    cannot be committed.
    """

    store = AuditStore(
        os.environ.get("SIGNER_AUDIT_DATABASE_URL", ""),
        os.environ.get("SIGNER_AUDIT_HMAC_KEY", ""),
    )
    result_payload = store.append(
        action,
        result,
        digest=digest,
        idempotency_key=idempotency_key,
        reason=reason,
        metadata=metadata,
    )
    record(action, result, digest=digest, idempotency_key=idempotency_key, reason=reason)
    return result_payload


def verify_durable():
    store = AuditStore(
        os.environ.get("SIGNER_AUDIT_DATABASE_URL", ""),
        os.environ.get("SIGNER_AUDIT_HMAC_KEY", ""),
    )
    return store.verify()
