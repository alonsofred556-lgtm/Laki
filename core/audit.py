"""Append-only, non-secret audit events for funding and execution controls."""

import hashlib
import hmac
import json

from django.conf import settings

from .models import AuditEvent


def _safe(value):
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items() if str(key).lower() not in {"signature", "privatekey", "seed", "secret", "token"}}
    if isinstance(value, list):
        return [_safe(item) for item in value[:100]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def record_event(action, *, result="success", actor_type="system", actor_id=None, subject_type=None, subject_id=None, request_id=None, metadata=None):
    clean = _safe(metadata or {})
    encoded = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    audit_key = getattr(settings, "CONI_AUDIT_HASH_KEY", "").encode("utf-8")
    digest = hmac.new(audit_key, encoded.encode("utf-8"), hashlib.sha256).hexdigest() if audit_key else hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return AuditEvent.objects.create(
        action=action,
        actor_type=actor_type,
        actor_id=str(actor_id) if actor_id else None,
        subject_type=subject_type,
        subject_id=str(subject_id) if subject_id else None,
        result=result,
        request_id=request_id,
        metadata=clean,
        metadata_hash=digest,
    )
