from decimal import Decimal

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from .models import BotWallet, DepositObservation, FundingIntent, LedgerAccount, LedgerEntry, WithdrawalRequest


class InsufficientBalance(Exception):
    pass


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
                or bot_wallet.address.lower() != observation.destination.lower()
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
    LedgerEntry.objects.get_or_create(
        reference_type="deposit",
        reference_id=observation.event_key,
        debit_account=treasury_account,
        credit_account=user_account,
        defaults={
            "asset": observation.asset,
            "amount_base_units": observation.amount_base_units,
        },
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
    if ledger_balance(available) < withdrawal.amount_base_units:
        raise InsufficientBalance("insufficient available balance")

    LedgerEntry.objects.get_or_create(
        reference_type="withdrawal-reservation",
        reference_id=str(withdrawal.id),
        debit_account=available,
        credit_account=reserved,
        defaults={
            "asset": withdrawal.asset,
            "amount_base_units": withdrawal.amount_base_units,
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
            "amount_base_units": withdrawal.amount_base_units,
        },
    )
    withdrawal.status = "released"
    withdrawal.failure_code = failure_code
    withdrawal.save(update_fields=["status", "failure_code"])
    return withdrawal


@transaction.atomic
def settle_withdrawal(withdrawal_id, tx_hash):
    withdrawal = WithdrawalRequest.objects.select_for_update().select_related("wallet_identity").get(id=withdrawal_id)
    if withdrawal.status == "completed":
        return withdrawal
    if not tx_hash:
        raise ValueError("transaction hash is required")
    reserved = account_for(withdrawal.wallet_identity, withdrawal.asset, withdrawal.chain, "withdrawal-reserved")
    reserved = LedgerAccount.objects.select_for_update().get(id=reserved.id)
    treasury = account_for(None, withdrawal.asset, withdrawal.chain, "onchain-outflow")
    treasury = LedgerAccount.objects.select_for_update().get(id=treasury.id)
    LedgerEntry.objects.get_or_create(
        reference_type="withdrawal",
        reference_id=str(withdrawal.id),
        debit_account=reserved,
        credit_account=treasury,
        defaults={
            "asset": withdrawal.asset,
            "amount_base_units": withdrawal.amount_base_units,
        },
    )
    withdrawal.status = "completed"
    withdrawal.tx_hash = tx_hash
    withdrawal.completed_at = timezone.now()
    withdrawal.save(update_fields=["status", "tx_hash", "completed_at"])
    return withdrawal
