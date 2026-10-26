# PostgreSQL backup and restore runbook

1. Keep Railway PostgreSQL backups/PITR enabled for production. Use a separate staging database for restore tests.
2. Take or select a timestamped backup and restore it into the isolated database. Never restore over production.
3. Set the restored database URL only in the temporary staging service. Run `python manage.py migrate --plan` and `python manage.py check --deploy`.
4. Verify row counts and the latest `audit_events`, `reconciliation_runs`, `deposit_observations`, `ledger_entries`, and `withdrawal_requests`. Compare balances against the chain reconciliation report.
5. Run `python manage.py reconcile_ledger --once`. Investigate every anomaly; do not edit historical ledger rows.
6. Record the backup timestamp, restore duration, migration result, reconciliation result, and operator approval. Destroy temporary credentials and the restored database after the drill.

Backups do not replace transaction-level reconciliation or key recovery. A database backup cannot recover assets controlled by a lost signer key.
