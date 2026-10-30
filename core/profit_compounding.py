"""Ledger-aware profit allocation for bot compounding.

This module decides how confirmed profit is split. It does not claim that a
split is funded until the private signer proves the reinvestment transaction.
"""

from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.db import IntegrityError, transaction
from django.utils import timezone

from .assets import asset_decimals, supported_asset
from .assets import native_asset
from .bot_controls import global_bot_control
from .audit import record_event
from .bot_wallets import addresses_match
from .funding import account_for
from .models import Execution, LedgerEntry, ProfitAllocation, ProfitCompoundEvent, UserBotControl


COMPOUND_EVENTS = {"submitted", "confirmed", "failed"}
_COMPOUND_TRANSITIONS = {
    "pending-approval": {"submitted": "submitted", "failed": "failed"},
    "submitted": {"confirmed": "confirmed", "failed": "failed"},
}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def effective_compound_bps(identity):
    global_control = global_bot_control()
    user_control, _ = UserBotControl.objects.get_or_create(owner=identity)
    return max(0, min(10_000, int(global_control.compound_profit_bps), int(user_control.compound_profit_bps)))


def split_realized_profit(realized_profit_usd, compound_bps):
    """Return the durable PNL split for one settled execution.

    A route is expected to be profitable, but an on-chain execution can still
    settle at zero or a loss after price movement, gas, or bridge slippage.
    Those outcomes must remain visible in the user's PNL history without
    creating a negative compounding or payout instruction.
    """
    realized = _decimal(realized_profit_usd)
    if realized <= 0:
        return {
            "realized": realized,
            "compound": Decimal("0"),
            "payout": Decimal("0"),
            "compoundState": "not-applicable",
        }
    bounded_bps = max(0, min(10_000, int(compound_bps)))
    compound = (realized * Decimal(bounded_bps) / Decimal("10000")).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    payout = realized - compound
    return {
        "realized": realized,
        "compound": compound,
        "payout": payout,
        "compoundState": "not-applicable" if compound <= 0 else "pending-approval",
    }


@transaction.atomic
def allocate_confirmed_profit(execution_id):
    """Create one idempotent reinvestment/payout split after settlement."""
    execution = Execution.objects.select_for_update().select_related("user_identity").get(id=execution_id)
    if execution.state != "settled" or not execution.user_identity:
        raise ValueError("profit can only be allocated after a settled execution")
    realized = _decimal(execution.realized_profit_usd)
    compound_bps = effective_compound_bps(execution.user_identity)
    split = split_realized_profit(realized, compound_bps)
    allocation, _ = ProfitAllocation.objects.get_or_create(
        execution=execution,
        defaults={
            "user_identity": execution.user_identity,
            "realized_profit_usd": split["realized"],
            "compound_profit_usd": split["compound"],
            "payout_profit_usd": split["payout"],
            "compound_bps": max(0, min(10_000, int(compound_bps))),
            "compound_state": split["compoundState"],
        },
    )
    return allocation


def next_compound_state(current, event):
    if event not in COMPOUND_EVENTS:
        raise ValueError("unsupported compound event")
    try:
        return _COMPOUND_TRANSITIONS[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid compound transition {current}->{event}") from exc


def compound_intent_payload(allocation):
    """Return the bounded, public transaction facts for the private signer."""
    allocation = ProfitAllocation.objects.select_related(
        "execution", "execution__sell_bot_wallet", "user_identity"
    ).get(id=allocation.id)
    wallet = allocation.execution.sell_bot_wallet
    if allocation.compound_state not in {"pending-approval", "submitted"}:
        raise ValueError("compound allocation is not awaiting signer action")
    if allocation.compound_profit_usd <= 0 or not wallet or wallet.status != "active" or not wallet.address or not wallet.signer_key_ref:
        raise ValueError("compound allocation does not have an active attested bot wallet")
    asset = native_asset(wallet.chain)
    if not asset:
        raise ValueError("compound destination chain has no configured native asset")
    return {
        "allocationId": str(allocation.id),
        "executionId": str(allocation.execution_id),
        "state": allocation.compound_state,
        "compoundProfitUsd": str(allocation.compound_profit_usd),
        "payoutProfitUsd": str(allocation.payout_profit_usd),
        "compoundBps": allocation.compound_bps,
        "chain": wallet.chain,
        "asset": asset,
        "recipient": wallet.address,
        "signerKeyRef": wallet.signer_key_ref,
    }


def _positive_integer(value):
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 and str(parsed) == str(value).lstrip("0") else 0


@transaction.atomic
def record_compound_event(*, allocation_id, event, idempotency_key, tx_hash="", metadata=None):
    """Confirm a real reinvestment and credit the exact bot-wallet ledger.

    The signer must prove the chain, asset, recipient, and integer base-unit
    amount. A later deposit observation with the same transaction is treated
    as already credited to prevent double counting.
    """
    if event not in COMPOUND_EVENTS or not idempotency_key:
        raise ValueError("invalid compound event")
    allocation = ProfitAllocation.objects.select_for_update().select_related(
        "execution", "execution__sell_bot_wallet", "user_identity"
    ).get(id=allocation_id)
    if allocation.compound_state == "not-applicable":
        raise ValueError("compound allocation is disabled for this execution")
    existing = ProfitCompoundEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.allocation_id != allocation.id:
            raise ValueError("idempotency key belongs to another compound allocation")
        if existing.event != event or (tx_hash and existing.tx_hash != tx_hash):
            raise ValueError("idempotency key does not match the original compound event")
        return allocation
    next_state = next_compound_state(allocation.compound_state, event)
    proof = metadata if isinstance(metadata, dict) else {}
    wallet = allocation.execution.sell_bot_wallet
    if event in {"submitted", "confirmed"} and not str(tx_hash or "").strip():
        raise ValueError("compound proof requires transaction hash")
    if event == "confirmed":
        chain = str(proof.get("chain") or "")
        asset = str(proof.get("asset") or "").upper()
        recipient = str(proof.get("recipient") or "")
        amount = _positive_integer(proof.get("amountBaseUnits"))
        if not wallet or chain != wallet.chain or not addresses_match(chain, recipient, wallet.address):
            raise ValueError("compound proof recipient does not match the execution bot wallet")
        if asset != native_asset(chain):
            raise ValueError("compound proof must use the destination chain native asset")
        if not supported_asset(asset, chain) or asset_decimals(asset, chain) is None or amount <= 0:
            raise ValueError("compound proof asset or amount is invalid")
        amount_usd = _decimal(proof.get("amountUsd"))
        if amount_usd.quantize(Decimal("0.00000001")) != allocation.compound_profit_usd:
            raise ValueError("compound proof USD value does not match the allocation")
    try:
        ProfitCompoundEvent.objects.create(
            idempotency_key=idempotency_key,
            allocation=allocation,
            event=event,
            tx_hash=tx_hash or "",
            metadata=proof,
        )
    except IntegrityError:
        existing = ProfitCompoundEvent.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.allocation_id == allocation.id:
            return allocation
        raise ValueError("idempotency key belongs to another compound allocation")
    allocation.compound_state = next_state
    if tx_hash:
        allocation.compound_tx_hash = tx_hash
    allocation.compound_proof = proof
    if event == "confirmed":
        available = account_for(allocation.user_identity, asset, chain, "bot-available", bot_wallet=wallet)
        source = account_for(None, asset, chain, "onchain-receivable")
        LedgerEntry.objects.get_or_create(
            reference_type="profit-compound",
            reference_id=str(allocation.id),
            debit_account=source,
            credit_account=available,
            defaults={"asset": asset, "amount_base_units": amount},
        )
        allocation.confirmed_at = timezone.now()
    allocation.save(update_fields=["compound_state", "compound_tx_hash", "compound_proof", "confirmed_at"])
    record_event("profit_compound.event", actor_type="signer", subject_type="profit_allocation", subject_id=allocation.id, metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None})
    return allocation
