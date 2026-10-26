"""Structured signer audit records; ship stdout to an immutable log sink."""

import hashlib
import json
import logging
from datetime import datetime, timezone


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
