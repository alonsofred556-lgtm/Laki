# Coni signer / treasury boundary

This is a separate private service, not a Django view. It validates a short-lived, hashed, structured transaction intent and records the exact digest that an HSM/MPC adapter would sign. It intentionally returns `signer-adapter-required` until an independently audited custody provider is installed.

The service must run behind private networking or mTLS. Set `SIGNER_SHARED_SECRET`, `SIGNER_ALLOWED_RECIPIENTS` (a chain-to-address allowlist), and `SIGNER_MAX_VALUE_BASE_UNITS` through Railway Variables or a secret manager. Never put a seed phrase, raw private key, or unrestricted calldata in an environment variable. The adapter must additionally enforce the token, amount limits, nonce/idempotency, simulation result, quote hash, expiry, and two-person approval policy before signing.

Run locally only for policy tests:

```text
python -m pip install -r signer/requirements.txt
$env:SIGNER_SHARED_SECRET="local-only"
$env:CONI_EXECUTION_MODE="disabled"
uvicorn signer.app:app --host 127.0.0.1 --port 8090
```

Do not set `CONI_EXECUTION_MODE=live` or `CONI_SIGNER_AUDIT_APPROVED=true` until the HSM/MPC integration, recovery process, transaction simulation, independent security review, and operational approvals are complete.
