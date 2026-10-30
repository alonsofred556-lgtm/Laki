# Coni MPC/HSM deployment runbook

This repository contains the Coni policy and signer boundary. It does **not**
contain an HSM, an MPC key ceremony, blockchain private keys, or an independent
audit certificate. Those controls must be supplied by the custody operator and
reviewed by an independent security/custody assessor.

The safe production shape is:

```text
Browser / extension
        |
        v
Public Coni web API  <---->  Pangea API
        |
        +--> private worker services
        |
        +--> private signer policy service (this repo: signer/)
                    |
                    +--> private HSM/MPC adapter (outside Django)
                              |
                              +--> custody key shares / HSM quorum
```

The browser Fast Wallet is a separate non-custodial path. It creates and
encrypts a key in the browser and signs only after the user unlocks it. It must
never be used as the unattended bot signer.

## What Railway should host

Create these services in one Railway project and production environment:

1. `coni-web`: the public Django/Gunicorn service. Attach the public domain.
2. `coni-worker`: `python manage.py run_coni_workers`. Do not attach a public
   domain.
3. `coni-signer`: the private `signer/` policy service. Do not attach a public
   domain.
4. `coni-db`: the application PostgreSQL service.
5. `coni-signer-db`: a separate PostgreSQL service for signer idempotency.
6. `coni-audit-db`: a separate PostgreSQL service for the chained custody
   audit log.
7. `coni-custody-adapter`: the private `custody_adapter/` endpoint consumed by
   the signer.
8. `coni-adapter-db`: a separate PostgreSQL service for adapter idempotency.
9. `coni-adapter-audit-db`: a separate PostgreSQL service for adapter audit.
10. `coni-redis`: the private Redis service used by the web API and workers.
11. `coni-signer-provisioner`: the private signer image running
    `python -m signer.provisioning_worker`; it leases bot-wallet provisioning
    requests and never handles key shares.

The real MPC/HSM adapter should normally be a separate private custody system,
not a process sharing the Railway application container. Railway private
networking protects service-to-service traffic with encrypted WireGuard
tunnels and internal DNS; use a private service name such as
`http://coni-signer.railway.internal:8090` for internal calls. See the Railway
[private networking documentation](https://docs.railway.com/networking/private-networking).

Railway can build the signer from this repository's nested Dockerfile. In the
`coni-signer` service settings set:

```text
RAILWAY_DOCKERFILE_PATH=signer/Dockerfile
```

Railway documents this custom Dockerfile-path variable in its
[Dockerfile guide](https://docs.railway.com/builds/dockerfiles). Do not expose
the signer with a public domain.

## Railway variables

Set the following only on `coni-signer` through Railway's secret variables:

```text
CONI_EXECUTION_MODE=disabled
CONI_SIGNER_AUDIT_APPROVED=false
SIGNER_SHARED_SECRET=<random, unique, 32+ characters>
SIGNER_DATABASE_URL=<coni-signer-db private postgres URL>
SIGNER_AUDIT_DATABASE_URL=<coni-audit-db private postgres URL>
SIGNER_AUDIT_HMAC_KEY=<different random 32+ character secret>
SIGNER_DJANGO_API_URL=http://coni-web.railway.internal:<private-port>
CONI_SIGNER_PROVISION_SECRET=<different random 32+ character secret>
CONI_SIGNER_EVENT_SECRET=<different random 32+ character secret>
SIGNER_ALLOWED_RECIPIENTS=<exact chain-to-address JSON allowlist>
SIGNER_MAX_VALUE_BASE_UNITS=<small initial per-step cap>
SIGNER_MAX_TOTAL_VALUE_BASE_UNITS=<small initial aggregate cap>
SIGNER_REQUIRED_APPROVALS=2
SIGNER_APPROVER_KEYS=<JSON map of two distinct Ed25519 public keys>
SIGNER_CUSTODY_POLICY_ID=<external evidence reference>
SIGNER_KEY_CEREMONY_ID=<external evidence reference>
SIGNER_RECOVERY_DRILL_ID=<external evidence reference>
SIGNER_SECURITY_REVIEW_ID=<external evidence reference>
SIGNER_APPROVAL_POLICY_ID=<external evidence reference>
SIGNER_AUDIT_SINK_ID=<immutable/WORM audit sink reference>
SIGNER_ADAPTER_URL=https://<private-mpc-adapter-host>
SIGNER_ADAPTER_SHARED_SECRET=<same random value as ADAPTER_SHARED_SECRET>
SIGNER_ADAPTER_TIMEOUT_SECONDS=5
SIGNER_PROVISIONING_ENABLED=false
SIGNER_PROVISIONING_POLL_SECONDS=15
CONI_SIGNER_PROVISION_SESSION_KEY=<different random 32+ character secret>
```

Never put a seed phrase, raw private key, MPC share, HSM PIN, or unrestricted
calldata in a Railway variable. The adapter must keep key material inside its
custody boundary. If mTLS is required, mount the CA/client certificate and
client key using the custody platform's secret mechanism; do not commit them
or put them in the browser bundle.

On `coni-web` and `coni-worker`, set the normal application variables from
`backend/env.example`, plus:

```text
CONI_SIGNER_URL=http://coni-signer.railway.internal:8090
CONI_SIGNER_PROVISION_SECRET=<same value as the signer callback secret>
CONI_SIGNER_EVENT_SECRET=<same value as the signer callback secret>
CONI_EXECUTION_MODE=disabled
```

The two callback secrets must be distinct from `SIGNER_SHARED_SECRET` and from
each other. Keep the web and worker in the same Railway environment as the
signer so the private DNS name resolves correctly.

Deploy `coni-signer-provisioner` from the same repository and signer Dockerfile
as `coni-signer`, with start command:

```text
python -m signer.provisioning_worker
```

It needs the signer variables above, the same private Django URL and
`CONI_SIGNER_PROVISION_SECRET`, and the same adapter mTLS credentials. Keep
`SIGNER_PROVISIONING_ENABLED=false` until the coordinator implements and has
tested `/v1/provision`; enabling it while the coordinator is absent fails
closed rather than fabricating a wallet.

## MPC/HSM adapter contract

The included `HsmMpcHttpAdapter` is only an mTLS transport and response-boundary
check. The actual adapter must perform all of these actions before asking an
HSM/MPC quorum to sign:

1. Pull the exact Coni execution intent over the private HMAC channel.
2. Recompute the quote hash and simulation hash from the received Pangea
   artifact.
3. Parse every EVM calldata or Solana instruction; reject opaque or unexpected
   programs, routers, factories, token contracts, recipients, and bridge
   endpoints.
4. Check source wallet, destination wallet, token mint/contract, integer amount,
   minimum output, slippage, expiry, chain ID, nonce, gas limits, bridge route,
   and the configured per-step and aggregate caps.
5. Simulate the transaction on the matching chain immediately before signing.
6. Require two distinct approver signatures over the exact intent digest when
   the policy requires it.
7. Ask the HSM/MPC quorum to sign only the exact serialized transaction or
   message that was simulated.
8. Broadcast from the adapter, verify the resulting transaction proof, and
   post lifecycle events back to Coni with the exact chain, recipient, amount,
   finality, and transaction/message IDs.

The adapter must refuse a quote whose price, route, calldata, minimum output,
or recipient changed after simulation. A successful HTTP response from an RPC
node is not proof of settlement.

## Key ceremony and independent review

Before live mode, create and retain restricted evidence for:

- a documented MPC/HSM product and version;
- a two-person or threshold key-generation ceremony;
- separate key references for each bot-wallet chain and withdrawal role;
- backup and restore of the key shares without reconstructing them in Django;
- operator separation and two-person approval;
- key rotation and revocation;
- emergency pause and loss-of-RPC procedures;
- a recovery drill in a staging environment;
- transaction simulation and allowlist tests;
- an independent security/custody review and remediation record;
- immutable export of the signer audit chain.

The IDs for those records populate the `SIGNER_*_ID` variables, but the IDs
alone are not proof of an audit. Keep the actual reports outside the application
database and limit access to the custody operators and reviewer.

## Deployment sequence

1. Deploy `coni-db` and `coni-redis`.
2. Deploy `coni-web`; run migrations and verify `/api/health`.
3. Deploy `coni-worker` with execution disabled. Confirm scanning, deposit
   discovery, and reconciliation logs.
4. Deploy `coni-signer` from `signer/Dockerfile` with no public domain.
5. Check the private signer health endpoint from the operator network:

   ```text
   GET http://coni-signer.railway.internal:8090/health
   ```

   In disabled mode it is acceptable for live readiness to be false. The
   health report must show every required missing control explicitly.

6. Connect a staging MPC/HSM adapter and exercise provisioning, quote-bound
   simulation, signed approval, broadcast, failure, refund, and reconciliation
   tests using testnet or a capped non-production wallet.
7. Export and verify the signer audit chain. Run a database restore drill; a
   backup that has never been restored is unverified. Railway documents volume
   backups, PITR, and logical dump restore drills in its
   [Postgres backup guide](https://docs.railway.com/guides/postgres-backups-restores).
8. Have the independent reviewer sign the release evidence.
9. Only then set `CONI_SIGNER_AUDIT_APPROVED=true`, switch the signer and web
   services to `CONI_EXECUTION_MODE=live`, and use very small limits.
10. Increase limits only after reconciliation and incident drills pass.

## Go/no-go checks

Do not enable live signing if any of these is true:

- `coni-signer` has a public domain;
- the adapter is a mock, software-key process, or single unprotected host;
- the HSM/MPC shares are in Railway variables, a Railway volume, Django, or
  browser storage;
- two-person approvals are not cryptographically verified;
- the signer cannot independently simulate the exact Pangea artifact;
- audit database, audit export, or restore drill is missing;
- a bot wallet is not active and attested on both route chains;
- reconciliation reports a mismatch or unavailable RPC;
- the independent security/custody review is incomplete.

Railway is suitable for the application, worker, policy boundary, and audit
stores. It is not, by itself, an HSM or an independent custody audit.

## Chain-specific MPC domains

The bundled adapter sends an explicit curve and chain domain to the MPC
coordinator. Ethereum, Base, Robinhood Chain, and BNB Chain use
`secp256k1`/EVM transaction policy. Solana uses `ed25519`/Solana-message
policy. Configure and independently verify:

```text
ADAPTER_CHAIN_IDS={"ethereum":"<verified>","base":"<verified>","robinhood":"<verified>","bsc":"<verified>"}
ADAPTER_SOLANA_CLUSTER=devnet
ADAPTER_KEY_REFS={"ethereum":"<coordinator-key-ref>","base":"<coordinator-key-ref>","robinhood":"<coordinator-key-ref>","bsc":"<coordinator-key-ref>","solana":"<coordinator-key-ref>"}
```

Robinhood must have its own verified chain ID and key domain; never copy the
Ethereum ID. Do not switch the Solana cluster to `mainnet-beta` until the
Solana transaction parser, recent-blockhash expiry, SPL-token handling, and
devnet failure tests have been reviewed.
