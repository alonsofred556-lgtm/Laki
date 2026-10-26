# Coni database safety and Railway setup

This project now requires PostgreSQL and uses Django migrations as the canonical database schema. The old `backend/schema.sql` file is retained only as a legacy Node reference and must not be applied to a new Django database.

## 1. Create a separate production database

In Railway:

1. Open the Coni project.
2. Add a PostgreSQL service in the same project and region as the web service.
3. Use the PostgreSQL service's private Railway connection variable. In the Coni service, set:

   ```text
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   DATABASE_SSL=require
   ```

   Replace `Postgres` with the exact Railway service name if it is different.

Do not paste a database password into frontend code, Git, README files, screenshots, or browser environment variables. The database URL belongs only in Railway's server-side variables.

## 2. Do not reuse the old schema blindly

If the previous Node backend has already created tables from `backend/schema.sql`, take a PostgreSQL backup and inspect that database before running Django migrations. The Django migration uses the same logical table names but is the authoritative schema. The safest production migration is:

1. Create a fresh Railway PostgreSQL service.
2. Deploy the Django service against the fresh database.
3. Run `python manage.py migrate` through a one-off Railway command.
4. Import only reviewed, reconciled data if existing user balances must be preserved.

Never delete a production database to make a migration pass. If the old database must be retained, clone it first and test the migration on the clone.

## 3. Required server variables

Set these in Railway, not in the repository:

```text
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<long random secret, 50+ characters>
DJANGO_ALLOWED_HOSTS=laki-production.up.railway.app
CONI_ALLOWED_ORIGINS=https://laki-production.up.railway.app
CONI_AUTH_DOMAIN=https://laki-production.up.railway.app
CONI_AUTH_URI=https://laki-production.up.railway.app/api/auth/verify
CONI_OPS_TOKEN=<long random operations token>
CONI_EXECUTION_MODE=disabled
CONI_SIGNER_URL=
CONI_LIVE_TRADING_CONFIRMATION=
```

Add `CONI_FUNDING_RECIPIENTS` and `CONI_FEE_RECIPIENTS` only after the recipient addresses have been independently checked. Leave them empty while custody and withdrawals are disabled.

## 4. Run migrations safely

Deploy the application first. Confirm the release is healthy, then run the migration as a one-off command in Railway:

```text
python manage.py migrate --noinput
```

After the migration, check:

```text
python manage.py check --deploy
```

The web process must use Gunicorn through `railway.toml`; do not use Django's `runserver` in production.

## 5. Backups and access controls

- Enable Railway PostgreSQL backups and test restoring one before holding real user funds.
- Use separate staging and production databases and separate secrets.
- Give application code one database role; reserve schema changes and destructive operations for an administrator role.
- Keep the database private to Railway whenever possible; do not expose PostgreSQL publicly.
- Rotate `DJANGO_SECRET_KEY`, operations tokens, RPC credentials, and database credentials through a planned maintenance window.
- Monitor failed logins, unusual ledger writes, migration failures, and repeated API errors.
- Treat the ledger as append-only. Corrections should be new compensating entries, never edits or deletes of historical entries.

## 6. What is still intentionally disabled

The Django service supports wallet challenge/verification, durable account and ledger records, Pangea quote/market proxies, LayerZero route metadata, fee accounting, bot controls, and execution planning. It does not hold private keys, sign withdrawals, or broadcast trades. Those capabilities require an independently audited signer/treasury service, withdrawal policy, limits, monitoring, and incident recovery process.

Do not set live execution variables merely to make the UI show an active bot. First test on a fork/test environment, verify the transaction policy, and complete a security review.
