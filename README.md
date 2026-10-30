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

For a test-only run without a local PostgreSQL service, use the explicit
in-memory switch instead of `DATABASE_URL`:

```text
$env:CONI_TEST_MODE="true"
$env:DJANGO_DEBUG="true"
$env:DJANGO_SECRET_KEY="test-only-secret"
python manage.py test core signer
```

`CONI_TEST_MODE` is intentionally not a production fallback. Railway and all
normal development processes must keep it unset and use PostgreSQL.

Open `http://127.0.0.1:8000/` for the website and `http://127.0.0.1:8000/wallet.html` for the wallet page. The extension source is in `extension/`; load that folder as an unpacked extension in Chrome when you are ready to preview the popup. Django is now the canonical backend; `backend/server.mjs` is retained only as a legacy migration fallback.

## Railway deployment

Create a Railway service from this repository and a managed PostgreSQL service. The current deployment URL is `https://laki-production.up.railway.app`. Railway uses `railway.toml` to start Gunicorn/Django and injects `PORT`. Set `DJANGO_ALLOWED_HOSTS`, `CONI_ALLOWED_ORIGINS`, and the variables in `backend/env.example`, then run `python manage.py migrate` once against the Railway database. Keep `CONI_EXECUTION_MODE=disabled` until the signer and execution services have been independently audited.

Create a second private Railway service from the same repository with start command `python manage.py run_coni_workers` so market scanning, quote queuing, deposit verification, reconciliation, signer-gated planning, and optional USDG settlement preparation run continuously. See [infra/railway/README.md](infra/railway/README.md).

### Private custody service

The `signer/` service is Coni's private custody orchestration boundary. Deploy
it as a separate private service, attach one private PostgreSQL database for
signer idempotency plus a second private PostgreSQL database for the chained
audit log. Allow ingress only from the Django service and the operator network.
It never
stores a seed phrase or private key. `SIGNER_ADAPTER_URL` must point over
private HTTPS/mTLS to the HSM/MPC system that performs the key operation.

Before live mode, configure the signer secrets in its own secret manager:

1. Mount the adapter CA, client certificate, and client key.
2. Configure `SIGNER_AUDIT_DATABASE_URL` and a separate 32+ character
   `SIGNER_AUDIT_HMAC_KEY`; the signer must verify this chain in live mode.
3. Configure the recipient allowlist, per-transfer and aggregate limits, and
   at least two distinct Ed25519 approvers.
4. Supply references for the custody policy, key ceremony, recovery drill,
   security review, approval policy, and immutable audit sink.
5. Verify the HSM/MPC adapter can simulate and independently verify every
   receipt before it calls Django. Never set these references merely to bypass
   a gate; they are release evidence for an independent review.
6. Keep `CONI_EXECUTION_MODE=disabled` until the evidence is reviewed. Only
   then can the custody operator deliberately enable live mode and run a small
   capped test withdrawal.

The signer readiness endpoint is `/health`; in live mode it reports the exact
missing controls and refuses `/v1/sign-intent` until all are present. This
makes the service audit-ready, but it does not itself constitute an
independent custody or security audit.
The restricted operator endpoint `GET /v1/audit/verify` checks the chained
audit log. Export it to immutable/WORM storage and remove UPDATE/DELETE from
the production audit database role after provisioning.

See [MPC_RAILWAY_RUNBOOK.md](MPC_RAILWAY_RUNBOOK.md) for the complete Railway
topology, MPC adapter contract, key-ceremony evidence, and live-mode go/no-go
checklist. Railway hosts the policy boundary and private stores; it is not an
HSM and does not make the custody system independently audited.

Before enabling the planner, configure
`CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS` as a JSON map for every supported
chain. Each value is the native base-unit reserve for one route, including gas
and expected bridge-native fees. An empty or incomplete map keeps planning
inert; the worker never guesses gas from a positive balance.

## Current scope

- Live read-only Pangea `/api/pools` polling with a safe fallback snapshot, plus backend-proxied token metadata/logo resolution.
- Pangea history, bridge-activity, launch-feed, trade-quote, and bridge-quote integrations for live discovery and read-only route previews.
- ZGAP-first opportunity cards using the official contract and Solana mint supplied for this project.
- An execution-plan boundary that explains the route, estimated fees, and risk gates without accepting arbitrary calldata or broadcasting transactions.
- Wallet control center with EIP-1193 EVM connections and Phantom/Solflare/Backpack-compatible Solana connections.
- Native balance reads and a funding-intent boundary that refuses arbitrary destinations.
- Non-custodial funding claim and verification workflow with idempotent deposit observations, finality checks, and double-entry ledger crediting.
- Withdrawal request and balance-reservation workflow that remains signer- and broadcast-gated.
- Automatic native and configured ERC-20/SOL transfer discovery with unclaimed-deposit handling and durable chain cursors.
- Normalized Pangea token registry with per-chain addresses, tickers, metadata, and logo URLs.
- Redis fail-closed rate limiting, append-only audit events, and a reconciliation worker that compares ledger balances with active signer-attested bot-wallet balances through private RPCs.
- A separate signer policy service skeleton that requires an audited HSM/MPC adapter and rejects arbitrary calldata.
- Coni-branded logo and wordmark assets reused from the supplied theme.
- Responsive website shell and compact extension popup.
- No private key is sent to Django or the included signer bridge. Imported or newly generated non-custodial wallet secrets are encrypted locally in the browser; managed bot-wallet keys must remain inside the separately operated HSM/MPC custody boundary. Automatic bot execution remains gated until that boundary is supplied and independently reviewed.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [backend/README.md](backend/README.md) before adding wallet signing or execution.
# ClickOt
# Laki
