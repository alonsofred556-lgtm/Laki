# Coni signer / treasury boundary

This is a separate private service, not a Django view. It validates a short-lived, hashed, structured transaction intent, verifies cryptographically signed approvals, persists idempotency state in a private PostgreSQL database, writes a chained append-only decision record to a separate private audit database, and delegates the key operation to an HSM/MPC adapter over mTLS. It has no software-key fallback.

The included `HsmMpcHttpAdapter` is a custody boundary, not an HSM. The adapter endpoint must be operated by the custody team inside an HSM/MPC product or PKCS#11-backed service, with key ceremonies, quorum policy, backups, recovery drills, and an independent audit. Setting `CONI_SIGNER_AUDIT_APPROVED=true` without those controls is unsafe and does not turn this code into an audited custody system.

The private signer should pull `/api/internal/signer/execution-intent` with a
short-lived HMAC request, independently parse and simulate the returned
Pangea artifact, and then submit lifecycle events through the callback
endpoints. The returned artifact is an input for validation, not permission
to sign arbitrary calldata.

Before provisioning, poll `/api/internal/signer/bot-wallet-provision-requests`
with the provisioning HMAC. Each leased request identifies one user-scoped
bot-wallet record and chain; generate or allocate the address inside the
audited MPC/HSM system, then call `/api/internal/signer/bot-wallet-attest` with
the `provisionRequestId`, public address, opaque `signerKeyRef`, and attestation
hash. Never send a private key to Django.

`signer/django_client.py` contains the provider-neutral, HMAC-authenticated
transport for this exchange. Its `pull_execution_intent()` and
`record_execution_event()` methods validate the cross-chain route shape,
transaction-proof requirements, idempotency fields, and reject secret fields
before making a callback. The HSM/MPC adapter should call
`DjangoSignerClient.pull_provision_requests()`, create or allocate the key
inside custody, and call `attest_bot_wallet()`. The client rejects unexpected
fields and invalid chain/address/hash values before any callback is sent. It
does not generate wallets, sign transactions, or provide a software-key
fallback.

For bot-wallet top-ups and withdrawals, the adapter may call
`/api/internal/signer/bot-transfer-event` with only the transfer UUID, a
strict lifecycle event (`submitted`, `confirmed`, or `failed`), an
idempotency key, the resulting transaction hash, and bounded metadata. A
pending withdrawal is leased from `/api/internal/signer/bot-transfer-intents`
before the adapter works on it. The callback metadata must bind the exact
chain, asset, base-unit amount, recipient, and `verified: true`; a confirmed
event must also state `finality` as `confirmed` or `finalized`. A confirmed
withdrawal settles its pre-reserved ledger amount; a failed one releases the
reservation. Top-up balances are credited only by the separate deposit
verifier after an exact on-chain observation.

Normal user withdrawals use a separate, equally strict queue. An operator must
approve the request through the private `/api/ops/withdrawals/<id>/approve`
endpoint; there is no user-facing auto-approval path. The signer then leases
`/api/internal/signer/withdrawal-intents`, which returns the exact chain, asset,
integer amount, destination, one-time withdrawal nonce, and opaque key
reference. Post `submitted`, then `confirmed` (or `failed`) to
`/api/internal/signer/withdrawal-event`. The callback must include
`verified: true`, the exact nonce and intent fields, and `confirmed` or
`finalized` finality for a confirmation. Django settles or releases the
pre-reserved ledger entry exactly once from that evidence.

Keep `CONI_WITHDRAWAL_APPROVALS_ENABLED=false` until the private signer,
operator approval process, key references, and audit evidence are ready. The
approval endpoint is an operational control, not proof that the custody system
has been independently audited.

The public bot-wallet transfer endpoint intentionally rejects a `topup`
direction. The wallet UI's Top up action creates a `FundingIntent`, and the
deposit discovery/verifier is the only path that can credit the bot ledger.

Fee transfers are a separate bounded queue. The adapter pulls
`/api/internal/signer/fee-intents`, validates the chain, asset, recipient,
integer amount, opaque fee key reference, source state, and policy inside
custody, then returns `submitted` and `confirmed` proof through
`/api/internal/signer/fee-event`. Funding fees become eligible only after the
deposit is credited; withdrawal fees become eligible only after the requested
withdrawal is confirmed; trade fees become eligible only after the execution
is settled. Confirmations require `verified: true` and finalized chain
evidence. Never infer a paid fee from a pending or leased intent.

For realized profit, use `/api/internal/signer/profit-settlement-event`. A
`confirmed` event must include metadata matching the exact configured
recipient, USDG token, `robinhood` chain, and base-unit amount. Coni credits
the user's ledger only after that proof-bound callback.

For compounding, a settled execution creates a `ProfitAllocation`. The
reinvestment portion stays `pending-approval` until the custody service has
constructed and simulated the exact transfer to that execution's destination
bot wallet. Post `submitted` and then `confirmed` (or `failed`) to
`/api/internal/signer/profit-compound-event`. A confirmed callback must include
metadata for the exact `chain`, `asset`, `recipient`, integer
`amountBaseUnits`, and `amountUsd`; `amountUsd` must equal the immutable
allocation amount. Django then credits the bot-wallet ledger exactly once.
The callback does not authorize arbitrary transfers and does not accept a
private key.

Pull the bounded transfer facts first with
`/api/internal/signer/profit-compound-intent`. The signer must still obtain a
fresh asset/USD valuation, construct the transfer inside custody, simulate it,
and retain the valuation and transaction proof in its audit record.

The service must run behind private networking or mTLS. Set `SIGNER_SHARED_SECRET` (32+ random characters), `SIGNER_DATABASE_URL`, `SIGNER_AUDIT_DATABASE_URL`, `SIGNER_AUDIT_HMAC_KEY`, `SIGNER_ALLOWED_RECIPIENTS` (a chain-to-address allowlist), `SIGNER_MAX_VALUE_BASE_UNITS`, and `SIGNER_MAX_TOTAL_VALUE_BASE_UNITS` through a secret manager. Set `SIGNER_REQUIRED_APPROVALS=2` only when `SIGNER_APPROVER_KEYS` contains two distinct Ed25519 approver public keys and the approvers sign the exact intent digest. The PostgreSQL store prevents a retry from signing the same idempotency key twice; the separate audit store prevents a successful signing decision from existing without a durable audit append. Django signer callbacks require an HMAC over `timestamp + "." + exact request body` using the callback secret, timestamp headers, and a short clock-skew window. Never put a seed phrase, raw private key, or unrestricted calldata in an environment variable. The adapter must additionally enforce the token, amount limits, aggregate amount cap, nonce/idempotency, simulation result, quote hash, expiry, and two-person approval policy before signing. For the bundled Railway custody adapter, set `SIGNER_ADAPTER_SHARED_SECRET` to the same random value as `ADAPTER_SHARED_SECRET`; keep it different from `SIGNER_SHARED_SECRET` and every MPC credential.

The signer exposes a fail-closed custody readiness report. Live signing requires
mounted mTLS CA/client credentials, a private PostgreSQL store, a separate
PostgreSQL audit store with a 32+ character HMAC key, non-empty recipient and
value policies, two-person approvals, an immutable audit-sink reference, and
operator references for the custody policy, key ceremony,
recovery drill, security review, and approval policy. These references make the
release auditable; they do not constitute an independent audit. The reports and
ceremony evidence must be retained outside the application and reviewed by an
independent security/custody assessor.

The audit database contains only bounded custody decision metadata and never a
private key. Each row includes the previous row hash and a keyed event hash.
The private operator endpoint `GET /v1/audit/verify` verifies the entire chain
without returning event contents. Export the table to restricted immutable/WORM
storage on a schedule and compare the exported chain during every review. The
audit database role should not have UPDATE or DELETE privileges in production;
the first-boot schema creation is intended for provisioning and local testing.

For the provisioning transport, configure the private signer with
`SIGNER_DJANGO_API_URL` (the private Django base URL) and
`CONI_SIGNER_PROVISION_SECRET` (a separate 32+ character callback secret).
The secret must match Django's variable of the same name and must not be
reused as `SIGNER_SHARED_SECRET` or as any custody credential. Construct the
client with those values and call its methods from the audited adapter's
worker; do not expose either value to the browser.

The repository also includes `signer.provisioning_worker`, which is only an
orchestrator for the provider-neutral exchange. It is disabled unless
`SIGNER_PROVISIONING_ENABLED=true`, and it refuses to start without
`CONI_SIGNER_PROVISION_SESSION_KEY`. Deploy it as a separate private Railway
worker using the same signer image and the start command
`python -m signer.provisioning_worker`. It calls the adapter's `/v1/provision`
endpoint, which must forward to a real MPC coordinator `/v1/provision` DKG
operation. The worker accepts only a public address, opaque key reference,
attestation hash, and matching request nonce; it never receives a key share.
The nonce key is used to make retries of one leased request idempotent after a
worker crash. Keep the worker disabled until the coordinator's DKG,
resharing, recovery, and independent review are complete.

Run locally only for policy tests:

```text
python -m pip install -r signer/requirements.txt
$env:SIGNER_SHARED_SECRET="local-only-use-at-least-32-random-characters"
$env:CONI_EXECUTION_MODE="disabled"
uvicorn signer.app:app --host 127.0.0.1 --port 8090
```

For a private Railway signer service, deploy `signer/Dockerfile` as its own
service, attach a private PostgreSQL database, mount the adapter CA/client
certificate secrets, and restrict ingress to the Django service and operator
network. Do not expose `/v1/sign-intent` publicly. The service is not ready for
live assets until the adapter contract is integration-tested against the real
HSM/MPC system and the recovery/audit evidence is complete.

Do not set `CONI_EXECUTION_MODE=live` or `CONI_SIGNER_AUDIT_APPROVED=true` until the HSM/MPC integration, recovery process, transaction simulation, independent security review, and operational approvals are complete.
