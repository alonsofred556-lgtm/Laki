# Coni signer / treasury boundary

This is a separate private service, not a Django view. It validates a short-lived, hashed, structured transaction intent and records the exact digest that an HSM/MPC adapter would sign. It intentionally returns `signer-adapter-required` until an independently audited custody provider is installed.

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
transport for this exchange. The HSM/MPC adapter should call
`DjangoSignerClient.pull_provision_requests()`, create or allocate the key
inside custody, and call `attest_bot_wallet()`. The client rejects unexpected
fields and invalid chain/address/hash values before any callback is sent. It
does not generate wallets, sign transactions, or provide a software-key
fallback.

For bot-wallet top-ups and withdrawals, the adapter may call
`/api/internal/signer/bot-transfer-event` with only the transfer UUID, a
strict lifecycle event (`submitted`, `confirmed`, or `failed`), an
idempotency key, the resulting transaction hash, and bounded metadata. A
confirmed withdrawal settles its pre-reserved ledger amount; a failed one
releases the reservation. Top-up balances are credited only by the separate
deposit verifier after an exact on-chain observation.

For realized profit, use `/api/internal/signer/profit-settlement-event`. A
`confirmed` event must include metadata matching the exact configured
recipient, USDG token, `robinhood` chain, and base-unit amount. Coni credits
the user's ledger only after that proof-bound callback.

The service must run behind private networking or mTLS. Set `SIGNER_SHARED_SECRET`, `SIGNER_ALLOWED_RECIPIENTS` (a chain-to-address allowlist), `SIGNER_MAX_VALUE_BASE_UNITS`, and (optionally) `SIGNER_MAX_TOTAL_VALUE_BASE_UNITS` through Railway Variables or a secret manager. Django signer callbacks require an HMAC over `timestamp + "." + exact request body` using the callback secret, timestamp headers, and a short clock-skew window. Never put a seed phrase, raw private key, or unrestricted calldata in an environment variable. The adapter must additionally enforce the token, amount limits, aggregate amount cap, nonce/idempotency, simulation result, quote hash, expiry, and two-person approval policy before signing.

For the provisioning transport, configure the private signer with
`SIGNER_DJANGO_API_URL` (the private Django base URL) and
`CONI_SIGNER_PROVISION_SECRET` (a separate 32+ character callback secret).
The secret must match Django's variable of the same name and must not be
reused as `SIGNER_SHARED_SECRET` or as any custody credential. Construct the
client with those values and call its methods from the audited adapter's
worker; do not expose either value to the browser.

Run locally only for policy tests:

```text
python -m pip install -r signer/requirements.txt
$env:SIGNER_SHARED_SECRET="local-only"
$env:CONI_EXECUTION_MODE="disabled"
uvicorn signer.app:app --host 127.0.0.1 --port 8090
```

Do not set `CONI_EXECUTION_MODE=live` or `CONI_SIGNER_AUDIT_APPROVED=true` until the HSM/MPC integration, recovery process, transaction simulation, independent security review, and operational approvals are complete.
