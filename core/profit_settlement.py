"""Realized-profit settlement in verified USDG on Robinhood Chain."""

from decimal import Decimal, InvalidOperation, ROUND_DOWN

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .audit import record_event
from .funding import account_for
from .models import Execution, LedgerEntry, ProfitSettlement, ProfitSettlementEvent, WalletIdentity


SETTLEMENT_EVENTS = {"submitted", "confirmed", "failed"}
_SETTLEMENT_TRANSITIONS = {
    "pending-approval": {"submitted": "submitted", "failed": "failed"},
    "submitted": {"confirmed": "confirmed", "failed": "failed"},
}


def next_settlement_state(current, event):
    if event not in SETTLEMENT_EVENTS:
        raise ValueError("unsupported settlement event")
    try:
        return _SETTLEMENT_TRANSITIONS[current][event]
    except KeyError as exc:
        raise ValueError(f"invalid settlement transition {current}->{event}") from exc


def _decimal(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def prepare_usdg_settlement(*, execution_id, user_identity_id, realized_profit_usd, usdg_price_usd, recipient):
    """Create a payout instruction only after a settled execution is reconciled.

    The USDG token address, decimals, and price are explicit inputs/configuration;
    this function never guesses them and never broadcasts a transaction.
    """
    execution = Execution.objects.get(id=execution_id)
    identity = WalletIdentity.objects.get(id=user_identity_id)
    realized = _decimal(realized_profit_usd)
    price = _decimal(usdg_price_usd)
    if execution.state != "settled":
        raise ValueError("profit must be settled before USDG payout preparation")
    if not settings.CONI_PROFIT_SETTLEMENT_ENABLED:
        raise ValueError("USDG settlement is disabled")
    if not settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS or settings.CONI_PROFIT_SETTLEMENT_DECIMALS is None:
        raise ValueError("verified USDG token address and decimals are required")
    if settings.CONI_PROFIT_SETTLEMENT_CHAIN != "robinhood" or identity.chain != "robinhood":
        raise ValueError("USDG settlement requires the user's Robinhood Chain wallet identity")
    if realized <= 0 or price <= 0 or not recipient or recipient.lower() != identity.address.lower():
        raise ValueError("invalid realized profit, USDG price, or recipient")
    amount = (realized / price * (Decimal("10") ** settings.CONI_PROFIT_SETTLEMENT_DECIMALS)).to_integral_value(rounding=ROUND_DOWN)
    if amount <= 0:
        raise ValueError("realized profit is below one USDG base unit")
    settlement = ProfitSettlement.objects.filter(execution=execution).first()
    if settlement and settlement.state in {"submitted", "confirmed"}:
        raise ValueError("USDG settlement is already in progress or complete")
    defaults = {
        "user_identity": identity,
        "realized_profit_usd": realized,
        "settlement_asset": settings.CONI_PROFIT_SETTLEMENT_ASSET,
        "settlement_chain": settings.CONI_PROFIT_SETTLEMENT_CHAIN,
        "settlement_token": settings.CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS,
        "settlement_price_usd": price,
        "amount_base_units": amount,
        "recipient": identity.address,
        "state": "pending-approval",
    }
    if settlement:
        for key, value in defaults.items():
            setattr(settlement, key, value)
        settlement.save()
    else:
        settlement = ProfitSettlement.objects.create(execution=execution, **defaults)
    return settlement


@transaction.atomic
def record_usdg_settlement_event(*, settlement_id, event, idempotency_key, tx_hash="", metadata=None):
    if event not in SETTLEMENT_EVENTS:
        raise ValueError("unsupported settlement event")
    settlement = ProfitSettlement.objects.select_for_update().select_related("user_identity").get(id=settlement_id)
    existing = ProfitSettlementEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.settlement_id != settlement.id:
            raise ValueError("idempotency key belongs to another settlement")
        return settlement
    next_state = next_settlement_state(settlement.state, event)
    proof = metadata if isinstance(metadata, dict) else {}
    if event == "confirmed":
        if str(proof.get("recipient", "")).lower() != settlement.recipient.lower() or str(proof.get("token", "")).lower() != settlement.settlement_token.lower() or str(proof.get("chain", "")) != settlement.settlement_chain or str(proof.get("amountBaseUnits", "")) != str(settlement.amount_base_units):
            raise ValueError("USDG settlement proof does not match the payout instruction")
    try:
        with transaction.atomic():
            ProfitSettlementEvent.objects.create(
                idempotency_key=idempotency_key,
                settlement=settlement,
                event=event,
                tx_hash=tx_hash or "",
                metadata=proof,
            )
    except IntegrityError:
        existing = ProfitSettlementEvent.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.settlement_id == settlement.id:
            return settlement
        raise ValueError("idempotency key belongs to another settlement")
    if event == "confirmed":
        treasury = account_for(None, settlement.settlement_asset, settlement.settlement_chain, "onchain-outflow")
        user_account = account_for(settlement.user_identity, settlement.settlement_asset, settlement.settlement_chain, "available")
        LedgerEntry.objects.get_or_create(
            reference_type="profit-settlement",
            reference_id=str(settlement.id),
            debit_account=treasury,
            credit_account=user_account,
            defaults={"asset": settlement.settlement_asset, "amount_base_units": settlement.amount_base_units},
        )
        settlement.settled_at = timezone.now()
    settlement.state = next_state
    if tx_hash:
        settlement.tx_hash = tx_hash
    settlement.save(update_fields=["state", "tx_hash", "settled_at"])
    record_event("profit_settlement.event", actor_type="signer", subject_type="profit_settlement", subject_id=settlement.id, metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None})
    return settlement
