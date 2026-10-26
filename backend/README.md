# Coni backend boundary

`server.mjs` is the production boundary for the current static build. It fails closed for production when `DATABASE_URL` or a sufficiently strong `CONI_SESSION_SECRET` is missing.

- `/api/market/pools` proxies the fixed Pangea pool feed with a short cache.
- `/api/market/token-meta?launch=...` proxies Pangea token metadata so official token logos can be resolved without relying on browser cross-origin access.
- `/api/market/history`, `/api/market/bridge-activity`, and `/api/market/launches` proxy read-only activity and launch feeds with validation and short caches.
- `/api/market/trade-quote` and `/api/market/bridge-quote` proxy read-only route pricing. They return quote data only; they never sign or broadcast transaction calls.
- `/api/funding/intent` validates an asset, amount, wallet address, and server-owned allowlisted destination, then returns a short-lived intent. It does not move funds.
- `/api/health` exposes a minimal liveness check.
- `/api/auth/challenge`, `/api/auth/verify`, `/api/auth/session`, and `/api/auth/logout` implement nonce-based off-chain wallet authentication for EVM and Solana wallets. They do not authorize transactions.
- `/api/bot/status`, `/api/bot/scan`, `/api/bot/profit`, and `/api/bot/pause` expose guarded operations. Operations routes require `x-coni-ops-token` where applicable.
- `/api/fees/accrue` records fee liabilities in the ledger. It never sends a fee on-chain; the signer/treasury service must later sweep accrued balances.
- `/api/execution/plan` creates a persisted quoted execution only after authentication. It returns a conflict while execution is gated and never accepts arbitrary calldata.
- Static responses include restrictive security headers and a small request-body/rate limit.

`schema.sql` defines the durable wallet identity, double-entry ledger, funding intent, fee, bot-control, and execution tables. Run `npm run db:migrate` with `DATABASE_URL` before starting a production service. The `mcp/operations-server.mjs` process is a restricted operations MCP surface: it can inspect health, scan opportunities, inspect profit summaries, read fee configuration, and pause the bot, but it has no signing or withdrawal tool.

Before accepting real funds, replace the in-memory rate limiter and cache with Redis, put the service behind a TLS-terminating reverse proxy, add structured audit logs, deploy a separate audited signer/treasury service, add chain finality monitors and reconciliation workers, and complete an independent security review. Never put `CONI_FUNDING_RECIPIENTS`, `CONI_FEE_RECIPIENTS`, or signer credentials in browser code.

Run locally with `node backend/server.mjs`, then open `http://127.0.0.1:4173/`.
