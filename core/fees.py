"""Deterministic fee policy and signer-gated fee-transfer queue.

Fees are calculated server-side from integer base units.  Django records the
fee intent and recipient, but never signs or broadcasts the fee transfer.
The private signer must pull a leased intent and return proof of submission or
confirmation before the event is marked settled.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from .audit import record_event
from .models import BotWalletTransfer, DepositObservation, Execution, FeeEvent, LedgerAccount, LedgerEntry, WithdrawalRequest


FEE_KINDS = {"trade", "funding", "withdrawal", "bot-withdrawal"}
FEE_EVENTS = {"submitted", "confirmed", "failed"}
FEE_LEASE_SECONDS = 120
_EVM_RECIPIENT = re.compile(r"^0x[a-fA-F0-9]{40}$")
_SOLANA_RECIPIENT = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


def _json_object(value):
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def fee_bps_for(kind: str) -> int:
    values = {
        "trade": getattr(settings, "CONI_TRADING_FEE_BPS", 0),
        "funding": getattr(settings, "CONI_BOT_FUNDING_FEE_BPS", 0),
        "withdrawal": getattr(settings, "CONI_WITHDRAWAL_FEE_BPS", 0),
        "bot-withdrawal": getattr(settings, "CONI_BOT_WITHDRAWAL_FEE_BPS", 0),
    }
    if kind not in values:
        raise ValueError("unsupported fee kind")
    try:
        value = int(values[kind])
    except (TypeError, ValueError) as exc:
        raise ValueError("fee basis points are invalid") from exc
    if value < 0 or value > 10_000:
        raise ValueError("fee basis points must be between 0 and 10000")
    return value


def fee_recipient_for(asset: str, chain: str) -> str:
    """Resolve a reviewed chain-specific fee wallet.

    Nested ``{"ETH":{"base":"0x..."}}`` is preferred.  Flat asset
    values remain supported for backwards compatibility, but production
    deployments should use a chain-specific recipient for every chain.
    """

    asset, chain = str(asset or "").upper(), str(chain or "")
    recipients = _json_object(getattr(settings, "CONI_FEE_RECIPIENTS", "{}"))
    configured = recipients.get(asset)
    if isinstance(configured, dict):
        return str(configured.get(chain) or "")
    if isinstance(configured, str):
        return configured
    for key in (f"{chain}:{asset}", f"{asset}:{chain}"):
        if recipients.get(key):
            return str(recipients[key])
    return ""


def fee_signer_key_ref(chain: str) -> str:
    refs = _json_object(getattr(settings, "CONI_SIGNER_FEE_KEY_REFS", "{}"))
    value = refs.get(str(chain))
    return str(value or "")


def _recipient_valid(chain: str, recipient: str) -> bool:
    if chain == "solana":
        return bool(_SOLANA_RECIPIENT.fullmatch(recipient))
    return bool(_EVM_RECIPIENT.fullmatch(recipient))


def fee_amount_base_units(gross_amount_base_units, kind: str) -> tuple[Decimal, int]:
    try:
        gross = Decimal(str(gross_amount_base_units))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("invalid fee amount") from exc
    if not gross.is_finite() or gross < 0 or gross != gross.to_integral_value():
        raise ValueError("fee amount must be a non-negative integer")
    bps = fee_bps_for(kind)
    amount = (gross * Decimal(bps) / Decimal(10_000)).to_integral_value(rounding=ROUND_DOWN)
    return amount, bps


def _fee_idempotency(kind: str, source_id: str, asset: str, chain: str) -> str:
    raw = f"{kind}|{source_id}|{str(asset).upper()}|{chain}"
    return "fee-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _source_ready(event: FeeEvent) -> bool:
    return {
        "funding": DepositObservation.objects.filter(event_key=event.source_id, status="credited").exists(),
        "trade": Execution.objects.filter(id=event.source_id, state="settled").exists(),
        "withdrawal": WithdrawalRequest.objects.filter(id=event.source_id, status="completed").exists(),
        "bot-withdrawal": BotWalletTransfer.objects.filter(id=event.source_id, state="confirmed").exists(),
    }.get(event.kind, False)


@transaction.atomic
def accrue_fee(*, kind: str, source_id: str, asset: str, chain: str, gross_amount_base_units,
               user_identity=None, execution_id=None) -> FeeEvent | None:
    """Create one idempotent fee-transfer intent, or return ``None`` at 0 fee."""

    if kind not in FEE_KINDS or not source_id:
        raise ValueError("invalid fee source")
    fee_amount, bps = fee_amount_base_units(gross_amount_base_units, kind)
    if fee_amount <= 0:
        return None
    recipient = fee_recipient_for(asset, chain)
    if not recipient or not _recipient_valid(chain, recipient):
        raise ValueError(f"fee recipient is not configured for {asset} on {chain}")
    key = _fee_idempotency(kind, str(source_id), asset, chain)
    existing = FeeEvent.objects.filter(idempotency_key=key).first()
    if existing:
        if (
            existing.kind != kind
            or existing.asset != str(asset).upper()
            or existing.chain != chain
            or existing.amount_base_units != fee_amount
            or existing.recipient != recipient
        ):
            raise ValueError("fee source is already bound to different fee terms")
        return existing
    try:
        event = FeeEvent.objects.create(
            user_identity=user_identity,
            execution_id=str(execution_id or "") or None,
            kind=kind,
            source_id=str(source_id),
            asset=str(asset).upper(),
            chain=chain,
            gross_amount_base_units=gross_amount_base_units,
            amount_base_units=fee_amount,
            fee_bps=bps,
            recipient=recipient,
            status="pending",
            idempotency_key=key,
        )
    except IntegrityError:
        event = FeeEvent.objects.get(idempotency_key=key)
    record_event(
        "fee.accrued",
        actor_type="system",
        actor_id=user_identity.id if user_identity else None,
        subject_type="fee",
        subject_id=event.id,
        metadata={"kind": kind, "sourceId": str(source_id), "asset": str(asset).upper(), "chain": chain,
                  "grossAmountBaseUnits": str(gross_amount_base_units), "amountBaseUnits": str(fee_amount),
                  "feeBps": bps, "recipient": recipient},
    )
    return event


def claim_fee_events(*, limit=20, chains=None):
    now = timezone.now()
    lease_until = now + timedelta(seconds=FEE_LEASE_SECONDS)
    with transaction.atomic():
        candidates = FeeEvent.objects.select_for_update().filter(
            Q(status="pending") | Q(status="leased", lease_until__lt=now),
        ).order_by("created_at")
        if chains:
            candidates = candidates.filter(chain__in=tuple(chains))
        rows = []
        for event in candidates:
            if len(rows) >= limit:
                break
            if _source_ready(event) and fee_signer_key_ref(event.chain):
                rows.append(event)
        for event in rows:
            event.status = "leased"
            event.attempt_count += 1
            event.lease_until = lease_until
            event.save(update_fields=["status", "attempt_count", "lease_until"])
        return rows


def fee_intent_payload(event: FeeEvent) -> dict:
    return {
        "feeId": str(event.id),
        "idempotencyKey": event.idempotency_key,
        "kind": event.kind,
        "sourceId": event.source_id,
        "executionId": event.execution_id,
        "userIdentityId": str(event.user_identity_id) if event.user_identity_id else None,
        "chain": event.chain,
        "asset": event.asset,
        "grossAmountBaseUnits": str(event.gross_amount_base_units),
        "amountBaseUnits": str(event.amount_base_units),
        "feeBps": event.fee_bps,
        "recipient": event.recipient,
        "signerKeyRef": fee_signer_key_ref(event.chain),
        "attempt": event.attempt_count,
        "leaseUntil": event.lease_until.isoformat() if event.lease_until else None,
    }


def record_fee_event(*, fee_id, event: str, idempotency_key: str, tx_hash="", metadata=None):
    """Record private-signer evidence for one fee transfer."""

    if event not in FEE_EVENTS:
        raise ValueError("unsupported fee event")
    if event in {"submitted", "confirmed"} and not str(tx_hash or "").strip():
        raise ValueError("fee transfer proof requires transaction hash")
    if event in {"submitted", "confirmed"} and (not isinstance(metadata, dict) or metadata.get("verified") is not True):
        raise ValueError("fee transfer proof is required")
    if event == "confirmed" and metadata.get("finality") not in {"confirmed", "finalized"}:
        raise ValueError("confirmed fee transfer requires finalized proof")
    with transaction.atomic():
        fee = FeeEvent.objects.select_for_update().get(id=fee_id)
        if event in {"submitted", "confirmed"} and not _source_ready(fee):
            raise ValueError("fee source lifecycle is not finalized")
        if event in {"submitted", "confirmed"}:
            expected = {
                "chain": fee.chain,
                "asset": fee.asset,
                "amountBaseUnits": str(fee.amount_base_units),
                "recipient": fee.recipient,
            }
            for key, value in expected.items():
                supplied = metadata.get(key)
                if key == "recipient" and fee.chain != "solana":
                    if not isinstance(supplied, str) or supplied.lower() != value.lower():
                        raise ValueError("fee transfer proof does not match the intent")
                elif str(supplied) != str(value):
                    raise ValueError("fee transfer proof does not match the intent")
        if fee.last_event_key == idempotency_key:
            if fee.last_event == event:
                return fee
            raise ValueError("fee event idempotency key does not match the original event")
        transitions = {
            "pending": {"submitted": "submitted", "failed": "failed"},
            "leased": {"submitted": "submitted", "failed": "failed"},
            "submitted": {"confirmed": "confirmed", "failed": "failed"},
        }
        if fee.status in {"confirmed", "failed"}:
            raise ValueError("fee transfer is already terminal")
        try:
            next_state = transitions[fee.status][event]
        except KeyError as exc:
            raise ValueError("invalid fee transfer transition") from exc
        fee.status = next_state
        fee.tx_hash = tx_hash or fee.tx_hash
        fee.last_event_key = idempotency_key
        fee.last_event = event
        fee.lease_until = None
        fee.save(update_fields=["status", "tx_hash", "last_event_key", "last_event", "lease_until"])
        if event == "confirmed":
            payable, _ = LedgerAccount.objects.get_or_create(
                owner=None,
                bot_wallet=None,
                asset=fee.asset,
                chain=fee.chain,
                kind="fee-payable",
            )
            outflow, _ = LedgerAccount.objects.get_or_create(
                owner=None,
                bot_wallet=None,
                asset=fee.asset,
                chain=fee.chain,
                kind="onchain-outflow",
            )
            LedgerEntry.objects.get_or_create(
                reference_type="fee-transfer",
                reference_id=str(fee.id),
                debit_account=payable,
                credit_account=outflow,
                defaults={"asset": fee.asset, "amount_base_units": fee.amount_base_units},
            )
    record_event("fee.signer_event", actor_type="signer", subject_type="fee", subject_id=fee.id,
                 metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None,
                           "metadata": metadata if isinstance(metadata, dict) else {}})
    return fee
