# Coni Railway services

Run the web API and workers as separate Railway services from the same
repository. Both services use the same PostgreSQL, Redis, Pangea, and private
RPC environment variables.

## Web service

Use the repository `railway.toml` configuration:

```text
python manage.py collectstatic --noinput
python manage.py migrate --noinput
gunicorn coni_backend.wsgi:application --bind 0.0.0.0:$PORT
```

Keep the public domain attached only to this service. The health endpoint is
`/api/health`.

Set the web service variable `PORT=8000`. The signer uses that stable port
over Railway private networking; do not point the signer at the public domain.
If you choose a different web port, update `SIGNER_DJANGO_API_URL` in the
signer service to `http://coni-web.railway.internal:<that-port>`.

## Worker service

Create a second private Railway service from the same repository. Set its
start command to:

```text
python manage.py run_coni_workers
```

Do not attach a public domain. The supervisor starts these durable workers:

- `scan_arbitrage`: reads every supported Pangea launch and cross-chain pool
  direction.
- `quote_arbitrage_opportunities`: obtains fresh Pangea trade and bridge quotes
  for queued routes.
- `verify_deposits`: discovers and verifies exact finalized native, ERC-20,
  and Solana token-account transfers.
- `reconcile_ledger`: checks double-entry balances, reads every active
  attested bot-wallet native/ERC-20/SPL balance through the private RPCs, and
  records chain-vs-ledger mismatches without mutating funds.
- `plan_arbitrage_executions`: creates user-specific, fresh-quote execution
  plans only when wallet funding and policy gates pass.
- `queue_profit_settlements`: prepares per-user USDG payout instructions after
  settled profit when verified USDG configuration and an explicit price are
  present; it never signs or broadcasts.

The planner never signs or broadcasts. It remains inert while the global bot is
paused, execution mode is disabled, or the signer/HSM/MPC boundary is absent.

Both services need the same `DATABASE_URL`, `CONI_REDIS_URL`, private RPC
endpoints, token registry, and audit configuration. The worker also needs the
same Pangea timeout and arbitrage-policy variables. The worker's logs are the
first place to inspect scanner, deposit, and reconciliation failures.

Before enabling planning, set `CONI_ARBITRAGE_NATIVE_RESERVE_BASE_UNITS` to a
JSON map containing an explicit positive native base-unit reserve for every
supported route chain. The reserve must cover gas and expected bridge-native
fees. An empty or incomplete map intentionally keeps the planner inert; it
also rejects underfunded routes and does not create overlapping active plans
for the same bot wallet.

On-chain reconciliation is enabled by default outside debug mode through
`CONI_RECONCILE_ONCHAIN_BALANCES=true`. It requires a private RPC and a
verified `CONI_TOKEN_REGISTRY` decimal scale. A `mismatch` or `unavailable`
result is an operations alert; it does not credit an account, settle a
withdrawal, or authorize a signer callback.

## Private custody/signer service

Create a third Railway service from this repository with `signer/Dockerfile`
as its Dockerfile path. Do not attach a public domain. Put it on Railway's
private network and allow only the Django service and the restricted operator
network to reach it. Give it one private PostgreSQL database for signer
idempotency and a second private PostgreSQL database for the chained audit
store; do not reuse the Django application database.

Configure the variables in `signer/env.example` using Railway secrets. The
signer must have mounted mTLS CA/client certificate/key files, a real HSM/MPC
adapter at `SIGNER_ADAPTER_URL`, a non-empty recipient allowlist, per-transfer
and aggregate caps, and at least two distinct Ed25519 approvers. Configure the
five custody evidence references and an immutable audit-sink reference before
requesting any live-mode review.

The signer service is an orchestration and policy boundary; it does not create
software keys. The actual key ceremony, signing, receipt verification, backup,
recovery drill, and independent audit belong to the HSM/MPC custody system and
must be evidenced outside this repository. Keep `CONI_EXECUTION_MODE=disabled`
until those controls are reviewed. The signer health endpoint reports missing
controls, and `/v1/sign-intent` remains fail-closed until all are present.

Create a fourth private service for bot-wallet provisioning using
`railway.signer-provisioner.toml`. Its start command is
`python -m signer.provisioning_worker`; it must not receive a public domain.
Give it the signer service's private Django URL, callback secret, adapter mTLS
credentials, and a separate `CONI_SIGNER_PROVISION_SESSION_KEY`. Keep
`SIGNER_PROVISIONING_ENABLED=false` until the real MPC coordinator implements
the request-bound `/v1/provision` operation. The worker fails closed if the
provider is absent and never handles private keys or shares.

After deployment, the restricted operator network can call `GET
/v1/audit/verify` with the signer authorization header. A valid chain is an
operational integrity check, not an independent audit certificate. Schedule
the audit table export into immutable/WORM storage and remove UPDATE/DELETE
privileges from the production audit role after schema provisioning.

Normal withdrawals are also fail-closed: Django reserves the user's ledger
balance, an operator explicitly approves the request, and only then does the
private signer lease it. Configure `CONI_SIGNER_WITHDRAWAL_KEY_REFS` with
opaque per-chain references owned by the custody adapter. The signer must post
nonce-bound, finality-bound evidence before Django marks the withdrawal
complete. A failed signer event releases the reservation; no frontend status
or submitted transaction alone can credit or settle funds.

For a smoke test before creating the long-lived service:

```text
python manage.py run_coni_workers --once
```

This will still not sign or broadcast transactions. Do not set live execution
variables merely to make the worker report success; live execution requires the
separate signer service, approval policy, and recovery evidence described in
`signer/README.md`.
