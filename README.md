# Coni

Coni is a cross-chain arbitrage operations console for Pangea launches.

This milestone ships a responsive website, a dedicated wallet control page, a Chrome-extension popup, and a guarded backend foundation. It authenticates wallet ownership with off-chain signatures, stores durable identity/ledger records when Postgres is configured, exposes read-only Pangea scanning, and keeps custody, withdrawals, and live signing fail-closed until a separate signer/treasury service is configured.

## Run locally

Install PostgreSQL locally (or use a managed PostgreSQL development database), then set its connection URL before migrating Django:

```text
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.lock
$env:DJANGO_DEBUG="true"
$env:DJANGO_SECRET_KEY="local-development-secret"
$env:DATABASE_URL="postgresql://coni:password@127.0.0.1:5432/coni"
$env:DATABASE_SSL="disable"
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

Open `http://127.0.0.1:8000/` for the website and `http://127.0.0.1:8000/wallet.html` for the wallet page. The extension source is in `extension/`; load that folder as an unpacked extension in Chrome when you are ready to preview the popup. Django is now the canonical backend; `backend/server.mjs` is retained only as a legacy migration fallback.

## Railway deployment

Create a Railway service from this repository and a managed PostgreSQL service. The current deployment URL is `https://laki-production.up.railway.app`. Railway uses `railway.toml` to start Gunicorn/Django and injects `PORT`. Set `DJANGO_ALLOWED_HOSTS`, `CONI_ALLOWED_ORIGINS`, and the variables in `backend/env.example`, then run `python manage.py migrate` once against the Railway database. Keep `CONI_EXECUTION_MODE=disabled` until the signer and execution services have been independently audited.

## Current scope

- Live read-only Pangea `/api/pools` polling with a safe fallback snapshot, plus backend-proxied token metadata/logo resolution.
- Pangea history, bridge-activity, launch-feed, trade-quote, and bridge-quote integrations for live discovery and read-only route previews.
- ZGAP-first opportunity cards using the official contract and Solana mint supplied for this project.
- An execution-plan boundary that explains the route, estimated fees, and risk gates without accepting arbitrary calldata or broadcasting transactions.
- Wallet control center with EIP-1193 EVM connections and Phantom/Solflare/Backpack-compatible Solana connections.
- Native balance reads and a funding-intent boundary that refuses arbitrary destinations.
- Non-custodial funding claim and verification workflow with idempotent deposit observations, finality checks, and double-entry ledger crediting.
- Withdrawal request and balance-reservation workflow that remains signer- and broadcast-gated.
- Coni-branded logo and wordmark assets reused from the supplied theme.
- Responsive website shell and compact extension popup.
- No seed phrases or private keys are exposed. Deposits, withdrawals, bridge transactions, and automatic trading remain disabled until the audited signer/treasury boundary is supplied.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [backend/README.md](backend/README.md) before adding wallet signing or execution.
# ClickOt
# Laki
