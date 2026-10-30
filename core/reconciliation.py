"""Database-side ledger reconciliation; chain balances remain a separate provider check."""

from django.db.models import Count
from django.conf import settings
from django.utils import timezone

from .audit import record_event
from .funding import ledger_balance, release_withdrawal
from .models import BotWalletTransfer, DepositObservation, FeeEvent, LedgerAccount, LedgerEntry, ProfitSettlement, ReconciliationRun, WithdrawalRequest
from .onchain_reconciliation import reconcile_bot_wallets


def bot_transfer_ledger_anomalies(transfer, *, reservation_count, settlement_count, release_count):
    """Return ledger mismatches for one bot-wallet transfer lifecycle."""
    anomalies = []
    transfer_id = str(transfer.id)
    if transfer.direction != "withdrawal":
        return anomalies
    if transfer.state in {"pending-approval", "submitted"} and (reservation_count != 1 or settlement_count != 0 or release_count != 0):
        anomalies.append({"type": "bot-withdrawal-pending-ledger-mismatch", "id": transfer_id, "reservation": reservation_count, "settlement": settlement_count, "release": release_count})
    if transfer.state == "confirmed" and (reservation_count != 1 or settlement_count != 1 or release_count != 0):
        anomalies.append({"type": "confirmed-bot-withdrawal-ledger-mismatch", "id": transfer_id, "reservation": reservation_count, "settlement": settlement_count, "release": release_count})
    if transfer.state == "failed" and (reservation_count != 1 or settlement_count != 0 or release_count != 1):
        anomalies.append({"type": "failed-bot-withdrawal-ledger-mismatch", "id": transfer_id, "reservation": reservation_count, "settlement": settlement_count, "release": release_count})
    return anomalies


def profit_settlement_ledger_anomalies(settlement, *, settlement_entry_count):
    """Confirmed USDG payouts must have exactly one immutable ledger entry."""
    if settlement.state == "confirmed" and settlement_entry_count != 1:
        return [{"type": "confirmed-profit-settlement-ledger-mismatch", "id": str(settlement.id), "count": settlement_entry_count}]
    if settlement.state != "confirmed" and settlement_entry_count != 0:
        return [{"type": "unconfirmed-profit-settlement-ledger-entry", "id": str(settlement.id), "state": settlement.state, "count": settlement_entry_count}]
    return []


def fee_ledger_anomalies(fee, *, accrual_count, settlement_count):
    """Ensure a fee is either queued or backed by exactly one ledger sweep."""
    anomalies = []
    if fee.status == "confirmed" and settlement_count != 1:
        anomalies.append({"type": "confirmed-fee-settlement-mismatch", "id": str(fee.id), "count": settlement_count})
    if fee.status in {"pending", "leased", "submitted"} and settlement_count:
        anomalies.append({"type": "unconfirmed-fee-settlement-entry", "id": str(fee.id), "state": fee.status, "count": settlement_count})
    if fee.status == "confirmed" and accrual_count != 1:
        anomalies.append({"type": "confirmed-fee-accrual-mismatch", "id": str(fee.id), "count": accrual_count})
    return anomalies


def can_release_expired_withdrawal(*, status, reservation_count):
    """Only release a timed-out request when its reservation is intact."""
    return status == "pending-approval" and reservation_count == 1


def release_expired_withdrawals(now=None):
    """Release reservations for requests that never reached signer approval."""
    now = now or timezone.now()
    released = 0
    for withdrawal in WithdrawalRequest.objects.filter(status="pending-approval", expires_at__lte=now).only("id"):
        reservation_count = LedgerEntry.objects.filter(
            reference_type="withdrawal-reservation",
            reference_id=str(withdrawal.id),
        ).count()
        if not can_release_expired_withdrawal(status="pending-approval", reservation_count=reservation_count):
            continue
        current = release_withdrawal(withdrawal.id, "expired")
        if current.status == "released":
            released += 1
            record_event(
                "withdrawal.expired",
                actor_type="system",
                subject_type="withdrawal",
                subject_id=withdrawal.id,
                metadata={"reason": "approval-timeout"},
            )
    return released


def reconcile_ledger():
    run = ReconciliationRun.objects.create(worker="ledger-reconciler")
    anomalies = []
    checked = 0
    expired_withdrawals = release_expired_withdrawals()
    onchain = {"checks": [], "anomalies": [], "checked": 0}
    for observation in DepositObservation.objects.filter(status="credited"):
        checked += 1
        entries = LedgerEntry.objects.filter(reference_type="deposit", reference_id=observation.event_key)
        if entries.count() != 1:
            anomalies.append({"type": "credited-deposit-entry-count", "id": str(observation.id), "count": entries.count()})
        if not observation.wallet_identity_id:
            anomalies.append({"type": "credited-deposit-without-owner", "id": str(observation.id)})
    for withdrawal in WithdrawalRequest.objects.all():
        checked += 1
        reservation_count = LedgerEntry.objects.filter(reference_type="withdrawal-reservation", reference_id=str(withdrawal.id)).count()
        settlement_count = LedgerEntry.objects.filter(reference_type="withdrawal", reference_id=str(withdrawal.id)).count()
        if withdrawal.status in {"pending-approval", "approved", "leased", "broadcasting", "pending-confirmation"} and reservation_count != 1:
            anomalies.append({"type": "withdrawal-reservation-mismatch", "id": str(withdrawal.id), "count": reservation_count})
        if withdrawal.status == "completed" and (reservation_count != 1 or settlement_count != 1):
            anomalies.append({"type": "completed-withdrawal-settlement-mismatch", "id": str(withdrawal.id), "reservation": reservation_count, "settlement": settlement_count})
    for transfer in BotWalletTransfer.objects.all():
        checked += 1
        anomalies.extend(bot_transfer_ledger_anomalies(
            transfer,
            reservation_count=LedgerEntry.objects.filter(reference_type="bot-withdrawal-reservation", reference_id=str(transfer.id)).count(),
            settlement_count=LedgerEntry.objects.filter(reference_type="bot-withdrawal", reference_id=str(transfer.id)).count(),
            release_count=LedgerEntry.objects.filter(reference_type="bot-withdrawal-release", reference_id=str(transfer.id)).count(),
        ))
    for settlement in ProfitSettlement.objects.all():
        checked += 1
        anomalies.extend(profit_settlement_ledger_anomalies(
            settlement,
            settlement_entry_count=LedgerEntry.objects.filter(reference_type="profit-settlement", reference_id=str(settlement.id)).count(),
        ))
    fee_accrual_refs = {
        "funding": "funding-fee-accrual",
        "trade": "trade-fee-accrual",
        "withdrawal": "withdrawal-fee-accrual",
        "bot-withdrawal": "bot-withdrawal-fee-accrual",
    }
    for fee in FeeEvent.objects.all():
        checked += 1
        anomalies.extend(fee_ledger_anomalies(
            fee,
            accrual_count=LedgerEntry.objects.filter(reference_type=fee_accrual_refs.get(fee.kind, ""), reference_id=str(fee.id)).count(),
            settlement_count=LedgerEntry.objects.filter(reference_type="fee-transfer", reference_id=str(fee.id)).count(),
        ))
    for account in LedgerAccount.objects.filter(owner__isnull=False):
        checked += 1
        balance = ledger_balance(account)
        if balance < 0:
            anomalies.append({"type": "negative-user-balance", "id": str(account.id), "balance": str(balance)})
    if getattr(settings, "CONI_RECONCILE_ONCHAIN_BALANCES", not settings.DEBUG):
        onchain = reconcile_bot_wallets()
        checked += onchain["checked"]
        anomalies.extend(onchain["anomalies"])
    run.checked_count = checked
    run.anomaly_count = len(anomalies)
    run.status = "anomaly" if anomalies else "healthy"
    run.details = {
        "anomalies": anomalies[:200],
        "expiredWithdrawalsReleased": expired_withdrawals,
        "onchain": {"checked": onchain["checked"], "checks": onchain["checks"][:200]},
    }
    run.finished_at = timezone.now()
    run.save(update_fields=["checked_count", "anomaly_count", "status", "details", "finished_at"])
    record_event("ledger.reconciliation", result=run.status, subject_type="reconciliation_run", subject_id=run.id, metadata={"checked": checked, "anomalies": len(anomalies)})
    return {"runId": str(run.id), "status": run.status, "checked": checked, "anomalies": anomalies, "expiredWithdrawalsReleased": expired_withdrawals}
