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
    evm_address_re = re.compile(r"^0x[a-fA-F0-9]{40}$")
    solana_address_re = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

    def __init__(self, *, mode="disabled", audit_approved=False, max_expiry_seconds=90, allowed_recipients=None, max_value_base_units=0, max_total_value_base_units=None, required_approvals=0):
        self.mode = mode
        self.audit_approved = audit_approved
        self.max_expiry_seconds = max_expiry_seconds
        normalized = {}
        for chain, values in (allowed_recipients or {}).items():
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, (list, tuple, set)):
                raise ValueError("signer allowlist values must be address lists")
            chain_name = str(chain)
            normalized[chain_name] = {
                str(item) if chain_name == "solana" else str(item).lower()
                for item in values
            }
        self.allowed_recipients = normalized
        self.max_value_base_units = int(max_value_base_units or 0)
        self.max_total_value_base_units = int(
            max_total_value_base_units
            if max_total_value_base_units is not None
            else max_value_base_units or 0
        )
        self.required_approvals = int(required_approvals or 0)
        if self.required_approvals < 0 or self.required_approvals > 4:
            raise ValueError("required approvals must be between 0 and 4")

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
        if self.max_value_base_units <= 0:
            raise PolicyError("maximum transaction value is not configured")
        if self.max_total_value_base_units <= 0:
            raise PolicyError("maximum total transaction value is not configured")
        approval_refs = payload.get("approvalRefs", [])
        if self.required_approvals:
            if not isinstance(approval_refs, list) or len(approval_refs) != self.required_approvals:
                raise PolicyError("required approval references are missing")
            if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9._:/-]{8,128}", item) for item in approval_refs) or len(set(approval_refs)) != len(approval_refs):
                raise PolicyError("approval references are invalid")
        elif approval_refs not in (None, []) and not isinstance(approval_refs, list):
            raise PolicyError("approval references must be a list")
        expires_at = int(payload.get("expiresAt", 0))
        now = int(datetime.now(timezone.utc).timestamp())
        if expires_at <= now or expires_at > now + self.max_expiry_seconds:
            raise PolicyError("intent expiry is invalid")
        steps = payload.get("steps")
        if not isinstance(steps, list) or not steps or len(steps) > 8:
            raise PolicyError("structured transaction steps are required")
        normalized = []
        total_value = 0
        for step in steps:
            if not isinstance(step, dict) or set(step) - {"chain", "to", "valueBaseUnits", "calldataHash", "expiresAt"}:
                raise PolicyError("unexpected transaction fields")
            chain, recipient = str(step.get("chain", "")), str(step.get("to", ""))
            address_pattern = self.solana_address_re if chain == "solana" else self.evm_address_re
            if chain not in self.chains or not address_pattern.fullmatch(recipient):
                raise PolicyError("invalid chain or recipient")
            recipient_key = recipient if chain == "solana" else recipient.lower()
            if recipient_key not in self.allowed_recipients.get(chain, set()):
                raise PolicyError("recipient is not allowlisted")
            if not re.fullmatch(r"\d{1,78}", str(step.get("valueBaseUnits", ""))):
                raise PolicyError("invalid transaction value")
            if self.max_value_base_units and int(step["valueBaseUnits"]) > self.max_value_base_units:
                raise PolicyError("transaction value exceeds signer policy")
            total_value += int(step["valueBaseUnits"])
            if not self.hash_re.fullmatch(str(step.get("calldataHash", ""))):
                raise PolicyError("calldata must be represented by a hash")
            if int(step.get("expiresAt", 0)) != expires_at:
                raise PolicyError("step expiry mismatch")
            normalized.append(SignStep(chain, recipient, str(step["valueBaseUnits"]), str(step["calldataHash"]), expires_at))
        if total_value > self.max_total_value_base_units:
            raise PolicyError("total transaction value exceeds signer policy")
        canonical_approvals = [str(item) for item in approval_refs] if isinstance(approval_refs, list) else []
        digest = hashlib.sha256(_canonical({"idempotencyKey": payload["idempotencyKey"], "quoteHash": quote_hash, "simulationHash": simulation_hash, "expiresAt": expires_at, "approvalRefs": canonical_approvals, "steps": [step.__dict__ for step in normalized]}).encode()).hexdigest()
        return {"digest": digest, "quoteHash": quote_hash, "simulationHash": simulation_hash, "idempotencyKey": payload["idempotencyKey"], "approvalRefs": canonical_approvals, "steps": normalized}

    def sign(self, payload, adapter=None):
        intent = self.validate(payload)
        if adapter is None:
            adapter = SignerAdapter()
        return adapter.sign(intent)
