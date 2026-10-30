# Coni Railway deployment

## Services

Create these Railway services in the same project:

1. Coni web/API service from this repository.
2. Railway PostgreSQL service.
3. A private Redis service (or a private Redis provider).
4. A deposit worker service from `railway.worker.toml`.
5. A reconciliation worker service from `railway.reconciliation.toml`.
6. A private arbitrage scanner service from `railway.arbitrage.toml`.

The web service uses Gunicorn with Django and receives Railway's `PORT` automatically. The old Node server is retained only as a migration fallback; it is not the Railway entrypoint.

## Required variables

Set these on the Coni service. Generate secrets outside the repository:

```text
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<at least 50 random characters>
DJANGO_ALLOWED_HOSTS=laki-production.up.railway.app
CONI_ALLOWED_ORIGINS=https://laki-production.up.railway.app
DATABASE_URL=${{Postgres.DATABASE_URL}}
DATABASE_SSL=require
CONI_AUTH_DOMAIN=laki-production.up.railway.app
CONI_AUTH_URI=https://laki-production.up.railway.app
CONI_OPS_TOKEN=<separate random operations token>
CONI_EXECUTION_MODE=disabled
CONI_SIGNER_URL=
CONI_SIGNER_PROVISION_SECRET=<separate private signer callback secret>
CONI_SIGNER_FEE_KEY_REFS={"ethereum":"hsm:fee:ethereum","base":"hsm:fee:base","robinhood":"hsm:fee:robinhood","bsc":"hsm:fee:bsc","solana":"hsm:fee:solana"}
CONI_LIVE_TRADING_CONFIRMATION=
CONI_SIGNER_AUDIT_APPROVED=false
CONI_REDIS_URL=${{Redis.REDIS_URL}}
CONI_REDIS_REQUIRED=true
CONI_FUNDING_RECIPIENTS={"ETH":"<reviewed Ethereum destination>","BNB":"<reviewed BNB destination>","SOL":"<reviewed Solana destination>"}
CONI_FEE_RECIPIENTS={"ETH":{"ethereum":"<reviewed Ethereum fee destination>","base":"<reviewed Base fee destination>","robinhood":"<reviewed Robinhood fee destination>"},"BNB":{"bsc":"<reviewed BNB fee destination>"},"SOL":{"solana":"<reviewed Solana fee destination>"},"ZGAP":{"ethereum":"<reviewed Ethereum ZGAP fee destination>","base":"<reviewed Base ZGAP fee destination>","robinhood":"<reviewed Robinhood ZGAP fee destination>","bsc":"<reviewed BNB ZGAP fee destination>","solana":"<reviewed Solana ZGAP fee destination>"}}
CONI_TRADING_FEE_BPS=10
# Pangea launch fees range from 0.3% to 5%; keep this conservative default
# until each launch's exact fee is loaded and verified server-side.
CONI_DEFAULT_LAUNCH_FEE_BPS=500
CONI_BOT_FUNDING_FEE_BPS=10
CONI_BOT_WITHDRAWAL_FEE_BPS=10
CONI_WITHDRAWAL_FEE_BPS=10
CONI_DEFAULT_COST_BUFFER_BPS=55
CONI_RPC_URLS={"ethereum":"<server-side RPC URL>","base":"<server-side RPC URL>","robinhood":"<server-side RPC URL>","bsc":"<server-side RPC URL>"}
CONI_EXPECTED_CHAIN_IDS={"ethereum":"0x1","base":"0x2105","robinhood":"0x1237","bsc":"0x38"}
CONI_SOLANA_RPC_URL=<server-side Solana RPC URL>
CONI_DEPOSIT_CONFIRMATIONS=12
CONI_DEPOSIT_MAX_AGE_MINUTES=1440
CONI_DEPOSIT_SCAN_BATCH=50
CONI_DEPOSIT_SCAN_LOOKBACK_BLOCKS=2000
CONI_TOKEN_REGISTRY=<verified JSON token registry, including decimals>
CONI_DEPOSIT_RECIPIENTS=<chain-specific reviewed treasury recipients>
```

Do not use zero addresses, burn addresses, user wallet addresses, or example values. Verify every destination on the intended chain before accepting funds.

`10` basis points is `0.1%`. Funding credits the bot ledger net of the fee;
bot and normal withdrawals reserve the requested amount plus the fee; a
settled cross-chain trade queues a fee based on its buy-leg native input. Each
fee is source-bound and waits for its lifecycle to be final before the private
signer can pull `/api/internal/signer/fee-intents`. The signer must return
proof through `/api/internal/signer/fee-event`; recording a fee in PostgreSQL
does not mean that the fee wallet has been paid. See `FEE_POLICY.md` for the
complete policy and recipient/key-reference requirements.

## Migration

Run once against the Railway PostgreSQL service from a trusted environment:

```text
python -m pip install -r requirements.lock
python manage.py migrate
python manage.py check --deploy
```

The migration creates wallet challenges, identities, per-chain bot-wallet records, bot-isolated ledger accounts, funding intents, deposit observations, chain cursors, audit events, reconciliation runs, withdrawal requests, global and user-scoped bot controls, opportunities, settlements, and execution records.

## Funding verification worker

The funding flow is non-custodial: the user approves a transfer in an external wallet, while the worker automatically discovers transfers to reviewed recipients and active per-user bot-wallet addresses, including Solana SPL token accounts. The discovery record remains `unclaimed` when it has no exact funding intent. An authenticated bot-wallet owner can claim one exact observation only inside `CONI_DEPOSIT_MAX_AGE_MINUTES`; stale observations are rejected. A separate verifier confirms the sender (when intent-bound), destination, asset, amount, network identity, and finality before crediting the ledger. Create a second Railway service from this repository and set its config file to `railway.worker.toml`; it uses the same database, Redis, and server-side RPC variables.

Solana Token-2022 transfers are still discoverable and verifiable by the worker, but the current browser funding builder exposes only legacy SPL Token assets. Token-2022 assets are intentionally omitted from the wallet funding selector until their instruction adapter is reviewed and enabled; this does not affect read-only market discovery or reconciliation:

```text
python manage.py verify_deposits --loop-seconds 30
```

For a one-off test:

```text
python manage.py verify_deposits --once
```

Do not run the worker until every RPC URL and expected chain ID has been checked. The worker never signs, broadcasts, or withdraws funds. It only observes, classifies, verifies, and settles exact deposits through the ledger.

## Reconciliation worker

Create another private Railway service with `railway.reconciliation.toml`:

```text
python manage.py reconcile_ledger --loop-seconds 60
```

It checks that every credited deposit has exactly one ledger entry, pending withdrawals have reservations, completed withdrawals have settlements, bot-wallet withdrawals have matching reservation/settlement/release entries, USDG payouts match their confirmed ledger entries, and user balances are never negative. It also releases only expired `pending-approval` withdrawals by creating an immutable release entry. It records every run and anomaly in PostgreSQL without editing history.
When `CONI_RECONCILE_ONCHAIN_BALANCES=true` (the production default), it also reads every active signer-attested bot wallet's native and configured token balances through the private RPCs and records exact chain-vs-ledger mismatches. It never credits funds or changes ledger history from a balance read.

## Arbitrage scanner worker

Create a separate private Railway service from `railway.arbitrage.toml`:

```text
python manage.py scan_arbitrage --loop-seconds 30
```

Set `CONI_ARBITRAGE_REQUIRE_QUOTES=true` and configure only public bot wallet addresses for quote requests. The worker evaluates every profitable cross-chain pool direction for each Pangea launch by default (`CONI_ARBITRAGE_MAX_CANDIDATES=0`); a positive value is an explicit operator cap. Exact quote requests remain bounded by `CONI_ARBITRAGE_MAX_QUOTE_REQUESTS` so Pangea rate limits are respected. It follows buy → Pangea canonical bridge quote → sell-on-delivered-amount, persists rejected/take-paper decisions, and never signs or broadcasts. Each authenticated identity also receives five durable bot-wallet placeholders; an address is usable only after the private signer/MPC service provisions and attests it through the private callback endpoint. It must not be treated as a live trader until the signer, simulation, approval, bridge-arrival, and reconciliation controls are independently reviewed.

## Quote-bound execution planner

After the signer/treasury service, ledger reconciliation, and approval policy
have passed review, create one more private Railway service using
`railway.execution-planner.toml`:

```text
python manage.py plan_arbitrage_executions --loop-seconds 30 --max-plans 10
```

This worker requires `CONI_EXECUTION_MODE=live`, an approved signer, an
enabled and unpaused global and user bot control, active per-user bot wallets
in the user-selected external or internal mode, and a positive
native-asset ledger balance on both route wallets. It fetches fresh Pangea
trade and bridge quotes for each user, stores a hash-bound execution plan, and
stops when the route no longer clears the net-profit policy. It does not sign,
broadcast, withdraw, or transfer funds; only the separate signer may do that.

## Smoke checks

```text
https://laki-production.up.railway.app/api/health
https://laki-production.up.railway.app/api/bot/status
```

The expected initial bot status is `mode: "disabled"`, `paused: true`, and `executionReady: false`.

The web service runs `collectstatic` during the Railway build. A warning about
`/app/staticfiles/` during an older deployment is harmless by itself, but a
new deployment should contain the generated static directory. The token list
also no longer performs one blocking Pangea metadata request per launch; logos
from the live launch feed are returned immediately, while missing logos can be
resolved through `/api/market/token-meta` without blocking `/api/market/tokens`.

## Operations MCP process

Run the MCP operations process separately with the same API URL and operations token:

```text
$env:CONI_API_URL="https://laki-production.up.railway.app"
$env:CONI_OPS_TOKEN="<same operations token>"
python mcp/operations_server.py
```

The MCP server can inspect health, scan opportunities, view profit summaries, read fee configuration, and pause the bot. It intentionally has no withdrawal or signing tool.

## Live execution gate

Do not set live mode until a separate signer/treasury service exists. That service must validate chain, contract, recipient, calldata, quote expiry, slippage, simulation results, and limits before signing. Only after that service is audited should the following be considered:

Deploy `signer/` as a separate private Railway service using its own dependency file and a private URL. Give it one private PostgreSQL database for intent idempotency and a second private PostgreSQL database for its chained audit log. Set `SIGNER_SHARED_SECRET`, `SIGNER_AUDIT_DATABASE_URL`, `SIGNER_AUDIT_HMAC_KEY`, `SIGNER_ALLOWED_RECIPIENTS`, and `SIGNER_MAX_VALUE_BASE_UNITS` only in that service. The included adapter is an mTLS orchestration bridge; it is not an HSM/MPC implementation and cannot be called audited custody until an independently operated key system, ceremony, recovery drill, and security review are complete.

```text
CONI_EXECUTION_MODE=live
CONI_SIGNER_URL=https://<private-signer-service>
CONI_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_RISK
CONI_SIGNER_AUDIT_APPROVED=true
```

These variables alone do not implement custody. They only remove the Coni application-level gate after the signer endpoint is supplied. The signer must use an audited HSM/MPC adapter, private networking or mTLS, simulation, allowlists, nonce/idempotency controls, two-person approval, and recovery drills. Configure `CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS` with explicit native base-unit reserves for every route chain; an empty map keeps the planner inert, and the planner rejects underfunded routes and overlapping active routes.

## Backups and security review

Before accepting real assets, enable Railway PostgreSQL backups/PITR, create a separate backup credential, run a restore drill into an isolated database, and record the result. Run `python manage.py check --deploy`, dependency and secret scanning, migrations on a staging clone, and an independent review of wallet authentication, the ledger, discovery/verifier workers, signer policy, and Pangea/LayerZero transaction adapters. The project is not audited merely because these checks pass.

## Bundled custody adapter endpoint

The repository also contains `custody_adapter/`, a private Railway service
that implements the endpoint consumed by `signer/adapter.py`. Deploy it with
`railway.custody-adapter.toml`. Give it separate intent and audit PostgreSQL
databases and configure the variables in `custody_adapter/env.example`.

On the signer service set:

```text
SIGNER_ADAPTER_URL=https://<private-custody-adapter-domain>
SIGNER_ADAPTER_SHARED_SECRET=<same value as adapter ADAPTER_SHARED_SECRET>
SIGNER_ADAPTER_CA_FILE=/run/secrets/adapter-ca.pem
SIGNER_ADAPTER_CLIENT_CERT=/run/secrets/signer-client.pem
SIGNER_ADAPTER_CLIENT_KEY=/run/secrets/signer-client-key.pem
```

The adapter does not generate keys or perform software signing. Set
`CONI_CUSTODY_BACKEND=mpc` only when `MPC_COORDINATOR_URL` points to a real
private MPC coordinator backed by distributed key shares. That coordinator
must return the same `intentDigest`, an opaque `signerKeyRef`, a bounded
signature, and an attestation hash. Until then `/v1/sign` intentionally
returns no signature and live execution must remain disabled.
