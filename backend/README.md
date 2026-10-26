# Coni backend boundary

The canonical production backend is Django in `coni_backend/` and `core/`. `server.mjs` is retained as a legacy Node fallback during this migration; Railway starts Gunicorn with `coni_backend.wsgi:application`.

- `/api/market/pools` proxies the fixed Pangea pool feed with a short cache.
- `/api/market/token-meta?launch=...` proxies Pangea token metadata so official token logos can be resolved without relying on browser cross-origin access.
- `/api/market/history`, `/api/market/bridge-activity`, and `/api/market/launches` proxy read-only activity and launch feeds with validation and short caches.
- `/api/market/trade-quote` and `/api/market/bridge-quote` proxy read-only route pricing. They return quote data only; they never sign or broadcast transaction calls.
- `/api/funding/intent` validates an asset, amount, wallet address, and server-owned allowlisted destination, then returns a short-lived intent. It does not move funds.
- `/api/funding/claim` accepts a user-submitted transaction hash as an untrusted claim; `/api/funding/status/<intent-id>` reports verification state. Only `python manage.py verify_deposits` can confirm the chain transfer and credit the ledger.
- `/api/funding/unclaimed` lists transfers automatically discovered into the authenticated user's active bot wallet; `/api/funding/claim-unclaimed` creates a verification intent for one exact observation only while it is inside `CONI_DEPOSIT_MAX_AGE_MINUTES`. Claiming never credits funds by itself, and stale observations are rejected.
- `/api/withdrawals/request` validates and reserves an available ledger balance for a withdrawal request. It never signs or broadcasts; `/api/withdrawals/status/<withdrawal-id>` reports the state.
- `/api/health` exposes a minimal liveness check.
- `/api/auth/challenge`, `/api/auth/verify`, `/api/auth/session`, and `/api/auth/logout` implement nonce-based off-chain wallet authentication for EVM and Solana wallets. They do not authorize transactions.
- `/api/bot-wallet` creates/reads five per-chain bot-wallet records plus PNL/trade history; `/api/bot-wallet/transfer` prepares top-up or withdrawal requests but requires an active audited signer-provisioned bot wallet and never broadcasts.
- `/api/bot/control` stores the authenticated user's enabled/paused state, conservative risk limits, and selected `external-bot` or `internal-bot` mode. The global operator pause and signer gate always override it.
- `/api/internal/signer/bot-wallet-attest` is a private signer callback. It binds only a public address, opaque HSM/MPC key reference, and attestation hash; it rejects private keys and must be reachable only through private networking/mTLS.
- `/api/internal/signer/bot-wallet-provision-requests` is the private, HMAC-authenticated pull queue for pending per-user bot-wallet records. Requests are leased and retryable; the signer must return the request ID when it attests the public address.
- `/api/internal/signer/bot-transfer-event` is the private idempotent callback for approved bot top-up/withdrawal lifecycle events. A confirmed bot withdrawal settles the reserved ledger amount; a failed withdrawal releases it. It accepts only a transfer ID, event, transaction reference, metadata, and HMAC authorization.
- `/api/internal/signer/profit-settlement-event` is the private idempotent USDG payout callback. Confirmation is accepted only when signer metadata matches the configured Robinhood recipient, USDG token, chain, and exact base-unit amount; the user ledger is credited only after that callback.
- `/api/internal/signer/execution-intent` is the private signer pull endpoint. It returns one exact hash-bound Pangea quote artifact and its legs, marks the execution `awaiting-signer`, and never returns private keys or accepts arbitrary calldata from the caller.
- Private signer callbacks require a short-lived HMAC over the exact request body in production; a static secret header is available only for local development when `CONI_SIGNER_CALLBACK_REQUIRE_HMAC=false`.
- `/api/bot/status`, `/api/bot/scan`, `/api/bot/profit`, and `/api/bot/pause` expose guarded operations. Operations routes require `x-coni-ops-token` where applicable.
- `/api/profit/prepare-settlement` creates a pending USDG payout instruction only for a settled execution and the user's Robinhood Chain identity. It never signs or broadcasts.
- `/api/fees/accrue` records fee liabilities in the ledger. It never sends a fee on-chain; the signer/treasury service must later sweep accrued balances.
- `/api/execution/plan` creates a persisted quoted execution only after authentication. It returns a conflict while execution is gated and never accepts arbitrary calldata.
- `python manage.py plan_arbitrage_executions` is an optional private worker. It re-quotes approved opportunities for each active and funded user bot wallet, then creates signer-gated execution plans; it never signs or broadcasts.
- Static responses include restrictive security headers and a small request-body/rate limit.

`core/models.py` and the generated migrations define the durable wallet identity, per-chain bot-wallet, bot-isolated double-entry ledger, funding intent, fee, bot-control, opportunity, settlement, and execution tables. Run `python manage.py migrate` with `DATABASE_URL` before starting a production service. The `mcp/operations_server.py` process is a restricted operations MCP surface: it can inspect health, scan opportunities, inspect profit summaries, read fee configuration, and pause the bot, but it has no signing or withdrawal tool.

Before accepting real funds, replace the in-memory rate limiter and cache with Redis, put the service behind a TLS-terminating reverse proxy, add structured audit logs, run the deposit verifier as a separate worker, add chain reconciliation and alerting, deploy a separate audited signer/treasury service for withdrawals, and complete an independent security review. Never put `CONI_FUNDING_RECIPIENTS`, `CONI_FEE_RECIPIENTS`, RPC credentials, or signer credentials in browser code.

Run locally with `python manage.py runserver 127.0.0.1:8000`, then open `http://127.0.0.1:8000/`.
