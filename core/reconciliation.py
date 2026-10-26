"""Database-side ledger reconciliation; chain balances remain a separate provider check."""

from django.db.models import Count
from django.utils import timezone

from .audit import record_event
from .funding import ledger_balance
from .models import DepositObservation, LedgerAccount, LedgerEntry, ReconciliationRun, WithdrawalRequest


def reconcile_ledger():
    run = ReconciliationRun.objects.create(worker="ledger-reconciler")
    anomalies = []
    checked = 0
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
        if withdrawal.status in {"pending-approval", "approved", "broadcasting", "pending-confirmation"} and reservation_count != 1:
            anomalies.append({"type": "withdrawal-reservation-mismatch", "id": str(withdrawal.id), "count": reservation_count})
        if withdrawal.status == "completed" and (reservation_count != 1 or settlement_count != 1):
            anomalies.append({"type": "completed-withdrawal-settlement-mismatch", "id": str(withdrawal.id), "reservation": reservation_count, "settlement": settlement_count})
    for account in LedgerAccount.objects.filter(owner__isnull=False):
        checked += 1
        balance = ledger_balance(account)
        if balance < 0:
            anomalies.append({"type": "negative-user-balance", "id": str(account.id), "balance": str(balance)})
    run.checked_count = checked
    run.anomaly_count = len(anomalies)
    run.status = "anomaly" if anomalies else "healthy"
    run.details = {"anomalies": anomalies[:200]}
    run.finished_at = timezone.now()
    run.save(update_fields=["checked_count", "anomaly_count", "status", "details", "finished_at"])
    record_event("ledger.reconciliation", result=run.status, subject_type="reconciliation_run", subject_id=run.id, metadata={"checked": checked, "anomalies": len(anomalies)})
    return {"runId": str(run.id), "status": run.status, "checked": checked, "anomalies": anomalies}
