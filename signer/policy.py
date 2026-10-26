"""Signer policy boundary. This module deliberately has no software-key fallback."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class SignStep:
    chain: str
    to: str
    value_base_units: str
    calldata_hash: str
    expires_at: int


class SignerAdapter:
    """Implement with an HSM/MPC provider; never with an env private key."""

    name = "unconfigured"

    def sign(self, intent):
        raise PolicyError("no audited signer adapter is configured")


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class SignerPolicy:
    chains = {"ethereum", "base", "robinhood", "bsc", "solana"}
    address_re = re.compile(r"^(0x[a-fA-F0-9]{40}|[1-9A-HJ-NP-Za-km-z]{32,44})$")
    hash_re = re.compile(r"^(0x)?[a-fA-F0-9]{64}$")

    def __init__(self, *, mode="disabled", audit_approved=False, max_expiry_seconds=90, allowed_recipients=None, max_value_base_units=0):
        self.mode = mode
        self.audit_approved = audit_approved
        self.max_expiry_seconds = max_expiry_seconds
        self.allowed_recipients = {chain: {item.lower() for item in values} for chain, values in (allowed_recipients or {}).items()}
        self.max_value_base_units = int(max_value_base_units or 0)

    def validate(self, payload):
        if not isinstance(payload, dict):
            raise PolicyError("intent must be an object")
        if self.mode != "live" or not self.audit_approved:
            raise PolicyError("signer is disabled until the audited custody policy is approved")
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", str(payload.get("idempotencyKey", ""))):
            raise PolicyError("invalid idempotency key")
        quote_hash = str(payload.get("quoteHash", ""))
        simulation_hash = str(payload.get("simulationHash", ""))
        if not self.hash_re.fullmatch(quote_hash) or not self.hash_re.fullmatch(simulation_hash):
            raise PolicyError("quote and simulation hashes are required")
        if not self.allowed_recipients:
            raise PolicyError("recipient allowlist is not configured")
        expires_at = int(payload.get("expiresAt", 0))
        now = int(datetime.now(timezone.utc).timestamp())
        if expires_at <= now or expires_at > now + self.max_expiry_seconds:
            raise PolicyError("intent expiry is invalid")
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > 8:
            raise PolicyError("structured transaction steps are required")
        normalized = []
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"chain", "to", "valueBaseUnits", "calldataHash", "expiresAt"}:
                raise PolicyError("unexpected transaction fields")
            chain, recipient = str(step.get("chain", "")), str(step.get("to", ""))
            if chain not in self.chains or not self.address_re.fullmatch(recipient):
                raise PolicyError("invalid chain or recipient")
            if recipient.lower() not in self.allowed_recipients.get(chain, set()):
                raise PolicyError("recipient is not allowlisted")
            if not re.fullmatch(r"\d{1,78}", str(step.get("valueBaseUnits", ""))):
                raise PolicyError("invalid transaction value")
            if self.max_value_base_units and int(step["valueBaseUnits"]) > self.max_value_base_units:
                raise PolicyError("transaction value exceeds signer policy")
            if not self.hash_re.fullmatch(str(step.get("calldataHash", ""))):
                raise PolicyError("calldata must be represented by a hash")
            if int(step.get("expiresAt", 0)) != expires_at:
                raise PolicyError("step expiry mismatch")
            normalized.append(SignStep(chain, recipient, str(step["valueBaseUnits"]), str(step["calldataHash"]), expires_at))
        digest = hashlib.sha256(_canonical({"idempotencyKey": payload["idempotencyKey"], "quoteHash": quote_hash, "simulationHash": simulation_hash, "expiresAt": expires_at, "steps": [step.__dict__ for step in normalized]}).encode()).hexdigest()
        return {"digest": digest, "quoteHash": quote_hash, "simulationHash": simulation_hash, "idempotencyKey": payload["idempotencyKey"], "steps": normalized}

    def sign(self, payload, adapter=None):
        intent = self.validate(payload)
        if adapter is None:
            adapter = SignerAdapter()
        return adapter.sign(intent)
