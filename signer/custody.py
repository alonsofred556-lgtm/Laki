"""Fail-closed readiness checks for Coni's private custody boundary.

This module does not create keys and does not make an audit claim. It verifies
that the operator has supplied the configuration and evidence references that
must exist before the separate HSM/MPC custody system can be enabled.
"""

from __future__ import annotations

import json
import os
import base64
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .policy import PolicyError


_EVIDENCE_ENV = (
    "SIGNER_CUSTODY_POLICY_ID",
    "SIGNER_KEY_CEREMONY_ID",
    "SIGNER_RECOVERY_DRILL_ID",
    "SIGNER_SECURITY_REVIEW_ID",
    "SIGNER_APPROVAL_POLICY_ID",
)


def _configured(value):
    text = str(value or "").strip()
    return bool(text) and not any(marker in text.lower() for marker in ("replace", "example", "<", ">"))


def _positive_int(value):
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _allowlist_configured(value):
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(isinstance(addresses, list) and addresses for addresses in payload.values())


def _approver_keys_configured(value, required):
    try:
        payload = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or len(payload) < required:
        return False
    decoded = []
    for encoded in payload.values():
        if not isinstance(encoded, str):
            return False
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return False
        if len(raw) != 32:
            return False
        decoded.append(raw)
    return len(set(decoded)) >= required


@dataclass(frozen=True)
class CustodyReadiness:
    ready: bool
    checks: dict[str, bool]
    missing: tuple[str, ...]

    def public(self):
        return {"ready": self.ready, "checks": self.checks, "missing": list(self.missing)}

    def require_live(self):
        if not self.ready:
            raise PolicyError("custody service is not live-ready: " + ", ".join(self.missing))


def readiness(environ=None):
    env = environ or os.environ
    adapter_url = urlparse(str(env.get("SIGNER_ADAPTER_URL", "")).strip())
    ca_file = str(env.get("SIGNER_ADAPTER_CA_FILE", "")).strip()
    client_cert = str(env.get("SIGNER_ADAPTER_CLIENT_CERT", "")).strip()
    client_key = str(env.get("SIGNER_ADAPTER_CLIENT_KEY", "")).strip()
    django_api = urlparse(str(env.get("SIGNER_DJANGO_API_URL", "")).strip())
    shared_secret = str(env.get("SIGNER_SHARED_SECRET", ""))
    provision_secret = str(env.get("CONI_SIGNER_PROVISION_SECRET", ""))
    event_secret = str(env.get("CONI_SIGNER_EVENT_SECRET", ""))
    required_approvals = int(env.get("SIGNER_REQUIRED_APPROVALS", "0") or 0) if str(env.get("SIGNER_REQUIRED_APPROVALS", "0")).isdigit() else 0
    checks = {
        "liveMode": env.get("CONI_EXECUTION_MODE", "disabled") == "live",
        "auditApprovalFlag": str(env.get("CONI_SIGNER_AUDIT_APPROVED", "false")).lower() == "true",
        "privateHttpsAdapter": adapter_url.scheme == "https" and bool(adapter_url.netloc),
        "adapterCaMounted": bool(ca_file) and Path(ca_file).is_file(),
        "adapterClientCertificateMounted": bool(client_cert) and Path(client_cert).is_file(),
        "adapterClientKeyMounted": bool(client_key) and Path(client_key).is_file(),
        "privateDjangoApi": django_api.scheme == "https" and bool(django_api.netloc),
        "provisionCallbackSecret": len(provision_secret) >= 32,
        "eventCallbackSecret": len(event_secret) >= 32,
        "callbackSecretsDistinct": len({shared_secret, provision_secret, event_secret}) == 3 and all(len(value) >= 32 for value in (shared_secret, provision_secret, event_secret)),
        "postgresStore": str(env.get("SIGNER_DATABASE_URL", "")).lower().startswith(("postgres://", "postgresql://")),
        "auditPostgresStore": str(env.get("SIGNER_AUDIT_DATABASE_URL", "")).lower().startswith(("postgres://", "postgresql://")),
        "auditHmacKey": len(str(env.get("SIGNER_AUDIT_HMAC_KEY", ""))) >= 32,
        "sharedSecret": len(shared_secret) >= 32,
        "recipientAllowlist": _allowlist_configured(env.get("SIGNER_ALLOWED_RECIPIENTS")),
        "perTransactionLimit": _positive_int(env.get("SIGNER_MAX_VALUE_BASE_UNITS")),
        "aggregateTransactionLimit": _positive_int(env.get("SIGNER_MAX_TOTAL_VALUE_BASE_UNITS")),
        "twoPersonApproval": required_approvals >= 2 and _approver_keys_configured(env.get("SIGNER_APPROVER_KEYS"), required_approvals),
        "immutableAuditSink": _configured(env.get("SIGNER_AUDIT_SINK_ID")),
    }
    for key in _EVIDENCE_ENV:
        checks[key] = _configured(env.get(key))
    missing = tuple(key for key, passed in checks.items() if not passed)
    return CustodyReadiness(ready=not missing, checks=checks, missing=missing)
