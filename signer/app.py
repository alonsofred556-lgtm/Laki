"""Private signer HTTP boundary; deploy separately from Django."""

import hmac
import json
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .audit import record, record_durable, verify_durable
from .adapter import AdapterError, configured_adapter
from .custody import readiness
from .policy import PolicyError, SignerPolicy
from .store import IntentStore, IntentStoreError
from .audit_store import AuditStoreError


class Step(BaseModel):
    chain: str
    to: str
    valueBaseUnits: str = Field(pattern=r"^\d{1,78}$")
    calldataHash: str = Field(pattern=r"^(0x)?[a-fA-F0-9]{64}$")
    expiresAt: int


class SignIntent(BaseModel):
    idempotencyKey: str
    quoteHash: str
    simulationHash: str
    expiresAt: int
    approvalRefs: list[str] = Field(default_factory=list, max_length=4)
    approvals: list[dict] = Field(default_factory=list, max_length=4)
    steps: list[Step] = Field(min_length=1, max_length=8)


app = FastAPI(title="Coni audited signer boundary", docs_url=None, redoc_url=None)


def _authorized(value):
    configured = os.environ.get("SIGNER_SHARED_SECRET", "")
    return bool(len(configured) >= 32 and value and hmac.compare_digest(value.removeprefix("Bearer "), configured))


@app.get("/health")
def health():
    configured = bool(os.environ.get("SIGNER_ADAPTER_URL", "").strip())
    mode = os.environ.get("CONI_EXECUTION_MODE", "disabled")
    store_configured = bool(os.environ.get("SIGNER_DATABASE_URL", "").strip())
    custody = readiness()
    audit_store_configured = bool(os.environ.get("SIGNER_AUDIT_DATABASE_URL", "").strip()) and len(os.environ.get("SIGNER_AUDIT_HMAC_KEY", "")) >= 32
    audit = {"configured": audit_store_configured, "verified": False}
    if audit_store_configured:
        try:
            verification = verify_durable()
            audit.update({"verified": bool(verification.get("valid")), "checked": verification.get("checked", 0), "lastHash": verification.get("lastHash")})
        except (AuditStoreError, ValueError) as exc:
            audit["error"] = str(exc)
    live_ready = custody.ready and audit.get("verified", False)
    return {"ok": mode != "live" or live_ready, "service": "coni-signer", "adapter": "hsm-mpc-mtls" if configured else "unconfigured", "store": store_configured, "auditStore": audit, "mode": mode, "custody": custody.public()}


@app.get("/v1/audit/verify")
def audit_verify(authorization: str | None = Header(default=None)):
    """Verify the private append-only audit chain; never expose its events."""

    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="signer authorization required")
    try:
        return {"service": "coni-signer", "audit": verify_durable()}
    except (AuditStoreError, ValueError) as exc:
        record("audit-verify", "failed", reason=str(exc))
        raise HTTPException(status_code=503, detail="audit store unavailable") from exc


@app.post("/v1/sign-intent")
def sign_intent(payload: SignIntent, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="signer authorization required")
    try:
        readiness().require_live()
    except PolicyError as exc:
        record("sign-intent", "rejected", idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="custody service is not live-ready") from exc
    try:
        allowlist = json.loads(os.environ.get("SIGNER_ALLOWED_RECIPIENTS", "{}"))
        if not isinstance(allowlist, dict):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="signer allowlist is invalid") from exc
    try:
        approver_keys = json.loads(os.environ.get("SIGNER_APPROVER_KEYS", "{}") or "{}")
        if not isinstance(approver_keys, dict):
            raise ValueError("approver key configuration is invalid")
        policy = SignerPolicy(
            mode=os.environ.get("CONI_EXECUTION_MODE", "disabled"),
            audit_approved=os.environ.get("CONI_SIGNER_AUDIT_APPROVED", "false").lower() == "true",
            allowed_recipients=allowlist,
            max_value_base_units=os.environ.get("SIGNER_MAX_VALUE_BASE_UNITS", "0"),
            max_total_value_base_units=os.environ.get("SIGNER_MAX_TOTAL_VALUE_BASE_UNITS")
            or os.environ.get("SIGNER_MAX_VALUE_BASE_UNITS", "0"),
            required_approvals=os.environ.get("SIGNER_REQUIRED_APPROVALS", "0"),
            approver_keys=approver_keys,
        )
        validated = policy.validate(payload.model_dump())
    except (PolicyError, ValueError, TypeError) as exc:
        record("sign-intent", "rejected", idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        store = IntentStore(os.environ.get("SIGNER_DATABASE_URL", ""))
        existing = store.reserve(payload.idempotencyKey, validated["digest"])
        if existing["state"] == "signed" and isinstance(existing.get("response"), dict):
            return existing["response"]
        if existing["state"] == "pending" and not existing.get("new"):
            # A concurrent request must not cause a second HSM/MPC operation.
            raise HTTPException(status_code=409, detail="signing intent is already in progress")
        if existing["state"] not in {"failed", "pending"}:
            raise HTTPException(status_code=409, detail="signing intent is not retryable")
    except HTTPException:
        raise
    except (IntentStoreError, ValueError) as exc:
        record("sign-intent", "store-unavailable", digest=validated["digest"], idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="signer intent store unavailable") from exc
    try:
        record_durable(
            "sign-intent",
            "accepted",
            digest=validated["digest"],
            idempotency_key=payload.idempotencyKey,
            metadata={"approvalRefs": validated["approvalRefs"], "stepCount": len(validated["steps"])},
        )
    except (AuditStoreError, ValueError) as exc:
        try:
            store.mark_failed(payload.idempotencyKey, validated["digest"])
        except IntentStoreError:
            pass
        record("sign-intent", "audit-unavailable", digest=validated["digest"], idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="signer audit store unavailable") from exc
    try:
        result = configured_adapter().sign(validated)
    except AdapterError as exc:
        try:
            store.mark_failed(payload.idempotencyKey, validated["digest"])
        except IntentStoreError:
            pass
        record("sign-intent", "adapter-unavailable", digest=validated["digest"], idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="HSM/MPC signer adapter unavailable") from exc
    response = {
        "accepted": True,
        "state": "signed",
        "intentDigest": validated["digest"],
        "signature": result["signature"],
        "signerKeyRef": result["signerKeyRef"],
        "attestation": result["attestation"],
        "adapter": result["adapter"],
    }
    try:
        store.mark_signed(payload.idempotencyKey, validated["digest"], response)
    except IntentStoreError as exc:
        record("sign-intent", "store-finalize-failed", digest=validated["digest"], idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="signer result could not be durably recorded") from exc
    try:
        record_durable("sign-intent", "signed", digest=validated["digest"], idempotency_key=payload.idempotencyKey, metadata={"adapter": result["adapter"]})
    except (AuditStoreError, ValueError) as exc:
        # The signed result is already durably idempotent.  Refuse to return a
        # success response until the caller can retry and obtain that same
        # recorded response, while preserving the no-second-sign guarantee.
        record("sign-intent", "post-sign-audit-unavailable", digest=validated["digest"], idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=503, detail="signer audit store unavailable") from exc
    return response
