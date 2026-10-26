"""Private signer HTTP boundary; deploy separately from Django."""

import hmac
import json
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .audit import record
from .policy import PolicyError, SignerPolicy


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
    steps: list[Step] = Field(min_length=1, max_length=8)


app = FastAPI(title="Coni audited signer boundary", docs_url=None, redoc_url=None)


def _authorized(value):
    configured = os.environ.get("SIGNER_SHARED_SECRET", "")
    return bool(configured and value and hmac.compare_digest(value.removeprefix("Bearer "), configured))


@app.get("/health")
def health():
    return {"ok": True, "service": "coni-signer", "adapter": "hsm-or-mpc-required", "mode": os.environ.get("CONI_EXECUTION_MODE", "disabled")}


@app.post("/v1/sign-intent")
def sign_intent(payload: SignIntent, authorization: str | None = Header(default=None)):
    if not _authorized(authorization):
        raise HTTPException(status_code=401, detail="signer authorization required")
    try:
        allowlist = json.loads(os.environ.get("SIGNER_ALLOWED_RECIPIENTS", "{}"))
        if not isinstance(allowlist, dict):
            raise ValueError
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="signer allowlist is invalid") from exc
    policy = SignerPolicy(mode=os.environ.get("CONI_EXECUTION_MODE", "disabled"), audit_approved=os.environ.get("CONI_SIGNER_AUDIT_APPROVED", "false").lower() == "true", allowed_recipients=allowlist, max_value_base_units=os.environ.get("SIGNER_MAX_VALUE_BASE_UNITS", "0"))
    try:
        validated = policy.validate(payload.model_dump())
    except (PolicyError, ValueError, TypeError) as exc:
        record("sign-intent", "rejected", idempotency_key=payload.idempotencyKey, reason=str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # No key adapter is included. A real deployment must inject an audited HSM/MPC adapter here.
    record("sign-intent", "adapter-required", digest=validated["digest"], idempotency_key=payload.idempotencyKey)
    return {"accepted": False, "state": "signer-adapter-required", "intentDigest": validated["digest"]}
