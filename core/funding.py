from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from .audit import record_event
from .fees import accrue_fee
from .models import BotWallet, DepositObservation, FeeEvent, FundingIntent, LedgerAccount, LedgerEntry, WithdrawalEvent, WithdrawalRequest


class InsufficientBalance(Exception):
    pass


WITHDRAWAL_EVENTS = {"submitted", "confirmed", "failed"}
WITHDRAWAL_LEASE_SECONDS = 120


def next_withdrawal_state(state, event):
    transitions = {
        "leased": {"submitted": "broadcasting", "failed": "released"},
        "broadcasting": {"confirmed": "completed", "failed": "released"},
    }
    try:
        return transitions[state][event]
    except KeyError as exc:
        raise ValueError("withdrawal event is out of order") from exc


def validate_withdrawal_proof(withdrawal, event, metadata):
    """Require signer evidence to bind exactly to the approved request."""
    if event not in {"submitted", "confirmed"}:
        return
    if not isinstance(metadata, dict) or metadata.get("verified") is not True:
        raise ValueError("withdrawal proof is required")
    expected = {
        "chain": withdrawal.chain,
        "asset": withdrawal.asset,
        "amountBaseUnits": str(withdrawal.amount_base_units),
        "destination": withdrawal.destination,
        "nonce": withdrawal.nonce,
    }
    for key, value in expected.items():
        supplied = metadata.get(key)
        if key == "destination" and withdrawal.chain != "solana":
            if not isinstance(supplied, str) or supplied.lower() != value.lower():
                raise ValueError("withdrawal proof does not match the request")
        elif str(supplied) != str(value):
            raise ValueError("withdrawal proof does not match the request")
    if event == "confirmed" and metadata.get("finality") not in {"confirmed", "finalized"}:
        raise ValueError("confirmed withdrawal requires finalized proof")


def claim_withdrawal_requests(*, limit=20, chains=None):
    """Lease manually approved requests for the private signer service."""
    now = timezone.now()
    lease_until = now + timedelta(seconds=WITHDRAWAL_LEASE_SECONDS)
    claimed = []
    with transaction.atomic():
        candidates = WithdrawalRequest.objects.select_for_update().select_related("wallet_identity").filter(
            Q(status="approved") | Q(status="leased", lease_until__lt=now),
            expires_at__gt=now,
        )
        if chains:
            candidates = candidates.filter(chain__in=tuple(chains))
        candidates = candidates.order_by("requested_at")[:limit]
        for withdrawal in candidates:
            withdrawal.status = "leased"
            withdrawal.attempt_count += 1
            withdrawal.lease_until = lease_until
            withdrawal.save(update_fields=["status", "attempt_count", "lease_until"])
            claimed.append(withdrawal)
    return claimed


def _addresses_match(chain, supplied, expected):
    if not isinstance(supplied, str) or not isinstance(expected, str):
        return False
    return supplied == expected if chain == "solana" else supplied.lower() == expected.lower()


def account_for(identity, asset, chain, kind, *, bot_wallet=None):
    if bot_wallet is not None and bot_wallet.owner_id != getattr(identity, "id", None):
        raise ValueError("bot wallet owner mismatch")
    account, _ = LedgerAccount.objects.get_or_create(
        owner=identity,
        bot_wallet=bot_wallet,
        asset=asset,
        chain=chain,
        kind=kind,
    )
    return account


def ledger_balance(account):
    credits = LedgerEntry.objects.filter(credit_account=account).aggregate(total=Sum("amount_base_units"))["total"] or Decimal("0")
    debits = LedgerEntry.objects.filter(debit_account=account).aggregate(total=Sum("amount_base_units"))["total"] or Decimal("0")
    return credits - debits


@transaction.atomic
def credit_confirmed_deposit(observation_id):
    observation = DepositObservation.objects.select_for_update().select_related("wallet_identity", "funding_intent").get(id=observation_id)
    if observation.status == "credited":
        return observation
    if observation.status != "confirmed" or not observation.wallet_identity:
        raise ValueError("deposit is not ready for credit")
    if observation.funding_intent:
        intent = FundingIntent.objects.select_for_update().get(id=observation.funding_intent_id)
        if intent.status == "credited":
            observation.status = "rejected"
            observation.rejection_code = "intent-already-credited"
            observation.save(update_fields=["status", "rejection_code"])
            return observation
        if intent.bot_wallet_id:
            bot_wallet = BotWallet.objects.filter(id=intent.bot_wallet_id).first()
            if (
                not bot_wallet
                or bot_wallet.owner_id != observation.wallet_identity_id
                or bot_wallet.chain != observation.chain
                or bot_wallet.status != "active"
                or not bot_wallet.address
                or not bot_wallet.signer_key_ref
                or not _addresses_match(observation.chain, bot_wallet.address, observation.destination)
            ):
                raise ValueError("bot wallet funding destination is not an active attested wallet")

    account_kind = "bot-available" if observation.funding_intent and observation.funding_intent.bot_wallet_id else "available"
    user_account = account_for(
        observation.wallet_identity,
        observation.asset,
        observation.chain,
        account_kind,
        bot_wallet=observation.funding_intent.bot_wallet if account_kind == "bot-available" else None,
    )
    user_account = LedgerAccount.objects.select_for_update().get(id=user_account.id)
    treasury_account = account_for(None, observation.asset, observation.chain, "onchain-receivable")
    treasury_account = LedgerAccount.objects.select_for_update().get(id=treasury_account.id)
    fee_event = accrue_fee(
        kind="funding",
        source_id=observation.event_key,
        asset=observation.asset,
        chain=observation.chain,
        gross_amount_base_units=observation.amount_base_units,
        user_identity=observation.wallet_identity,
    )
    fee_amount = fee_event.amount_base_units if fee_event else Decimal("0")
    net_amount = observation.amount_base_units - fee_amount
    if net_amount <= 0:
        raise ValueError("funding amount is smaller than the configured fee")
    LedgerEntry.objects.get_or_create(
        reference_type="deposit",
        reference_id=observation.event_key,
        debit_account=treasury_account,
        credit_account=user_account,
        defaults={
            "asset": observation.asset,
            "amount_base_units": net_amount,
        },
    )
    if fee_event:
        fee_account = account_for(None, observation.asset, observation.chain, "fee-payable")
        fee_account = LedgerAccount.objects.select_for_update().get(id=fee_account.id)
        LedgerEntry.objects.get_or_create(
            reference_type="funding-fee-accrual",
            reference_id=str(fee_event.id),
            debit_account=treasury_account,
            credit_account=fee_account,
            defaults={"asset": observation.asset, "amount_base_units": fee_amount},
        )
    observation.status = "credited"
    observation.credited_at = timezone.now()
    observation.save(update_fields=["status", "credited_at"])
    if observation.funding_intent:
        observation.funding_intent.status = "credited"
        observation.funding_intent.save(update_fields=["status"])
    return observation


@transaction.atomic
def reserve_withdrawal(withdrawal_id):
    withdrawal = WithdrawalRequest.objects.select_for_update().select_related("wallet_identity").get(id=withdrawal_id)
    if withdrawal.status != "pending-approval":
        return withdrawal

    available = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "available")
    reserved = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "withdrawal-reserved")
    available = LedgerAccount.objects.select_for_update().get(id=available.id)
    reserved = LedgerAccount.objects.select_for_update().get(id=reserved.id)
    reserve_amount = withdrawal.total_reserved_base_units or withdrawal.amount_base_units
    if ledger_balance(available) < reserve_amount:
        raise InsufficientBalance("insufficient available balance")

    LedgerEntry.objects.get_or_create(
        reference_type="withdrawal-reservation",
        reference_id=str(withdrawal.id),
        debit_account=available,
        credit_account=reserved,
        defaults={
            "asset": withdrawal.asset,
            "amount_base_units": reserve_amount,
        },
    )
    return withdrawal


@transaction.atomic
def release_withdrawal(withdrawal_id, failure_code="rejected"):
    withdrawal = WithdrawalRequest.objects.select_for_update().select_related("wallet_identity").get(id=withdrawal_id)
    if withdrawal.status in {"released", "completed"}:
        return withdrawal
    available = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "available")
    reserved = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "withdrawal-reserved")
    available = LedgerAccount.objects.select_for_update().get(id=available.id)
    reserved = LedgerAccount.objects.select_for_update().get(id=reserved.id)
    LedgerEntry.objects.get_or_create(
        reference_type="withdrawal-release",
        reference_id=str(withdrawal.id),
        debit_account=reserved,
        credit_account=available,
        defaults={
            "asset": withdrawal.asset,
            "amount_base_units": withdrawal.total_reserved_base_units or withdrawal.amount_base_units,
        },
    )
    withdrawal.status = "released"
    withdrawal.failure_code = failure_code
    withdrawal.save(update_fields=["status", "failure_code"])
    return withdrawal


@transaction.atomic
def settle_withdrawal(withdrawal_id, tx_hash, *, metadata=None, idempotency_key=None):
    """Compatibility wrapper that cannot settle without signer evidence."""
    if not idempotency_key:
        raise ValueError("withdrawal settlement requires signer event idempotency")
    return record_withdrawal_event(
        withdrawal_id=withdrawal_id,
        event="confirmed",
        idempotency_key=idempotency_key,
        tx_hash=tx_hash,
        metadata=metadata,
    )


@transaction.atomic
def record_withdrawal_event(*, withdrawal_id, event, idempotency_key, tx_hash="", metadata=None):
    """Apply one proof-bound private signer event to a normal withdrawal."""
    if event not in WITHDRAWAL_EVENTS:
        raise ValueError("unsupported withdrawal event")
    if event in {"submitted", "confirmed"} and not str(tx_hash or "").strip():
        raise ValueError("withdrawal proof requires transaction hash")
    withdrawal = WithdrawalRequest.objects.select_for_update().get(id=withdrawal_id)
    validate_withdrawal_proof(withdrawal, event, metadata)
    existing = WithdrawalEvent.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.withdrawal_id != withdrawal.id:
            raise ValueError("idempotency key belongs to another withdrawal")
        if existing.event != event or existing.tx_hash != (tx_hash or "") or existing.metadata != (metadata if isinstance(metadata, dict) else {}):
            raise ValueError("idempotency key does not match the original withdrawal event")
        return withdrawal
    next_state = next_withdrawal_state(withdrawal.status, event)
    try:
        with transaction.atomic():
            WithdrawalEvent.objects.create(
                idempotency_key=idempotency_key,
                withdrawal=withdrawal,
                event=event,
                tx_hash=tx_hash or "",
                metadata=metadata if isinstance(metadata, dict) else {},
            )
    except IntegrityError:
        existing = WithdrawalEvent.objects.filter(idempotency_key=idempotency_key).first()
        if existing and existing.withdrawal_id == withdrawal.id and existing.event == event and existing.tx_hash == (tx_hash or "") and existing.metadata == (metadata if isinstance(metadata, dict) else {}):
            return withdrawal
        raise ValueError("idempotency key belongs to another withdrawal")
    if event == "confirmed":
        reserved = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "withdrawal-reserved")
        treasury = account_for(None, withdrawal.asset, withdrawal.chain, "onchain-outflow")
        LedgerEntry.objects.get_or_create(
            reference_type="withdrawal",
            reference_id=str(withdrawal.id),
            debit_account=reserved,
            credit_account=treasury,
            defaults={"asset": withdrawal.asset, "amount_base_units": withdrawal.amount_base_units},
        )
        fee_event = FeeEvent.objects.filter(kind="withdrawal", source_id=str(withdrawal.id)).first()
        if fee_event and fee_event.amount_base_units > 0:
            fee_account = account_for(None, withdrawal.asset, withdrawal.chain, "fee-payable")
            LedgerEntry.objects.get_or_create(
                reference_type="withdrawal-fee-accrual",
                reference_id=str(fee_event.id),
                debit_account=reserved,
                credit_account=fee_account,
                defaults={"asset": withdrawal.asset, "amount_base_units": fee_event.amount_base_units},
            )
        withdrawal.completed_at = timezone.now()
    elif event == "failed":
        available = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "available")
        reserved = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "withdrawal-reserved")
        LedgerEntry.objects.get_or_create(
            reference_type="withdrawal-release",
            reference_id=str(withdrawal.id),
            debit_account=reserved,
            credit_account=available,
            defaults={"asset": withdrawal.asset, "amount_base_units": withdrawal.amount_base_units},
        )
        withdrawal.failure_code = str((metadata or {}).get("code") or "signer-failed")[:64]
    withdrawal.status = next_state
    withdrawal.tx_hash = tx_hash or withdrawal.tx_hash
    withdrawal.lease_until = None
    withdrawal.save(update_fields=["status", "tx_hash", "failure_code", "lease_until", "completed_at"])
    record_event(
        "withdrawal.signer_event",
        actor_type="signer",
        subject_type="withdrawal",
        subject_id=withdrawal.id,
        metadata={"event": event, "idempotencyKey": idempotency_key, "txHash": tx_hash or None},
    )
    return withdrawal
