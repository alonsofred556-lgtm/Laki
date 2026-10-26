# Coni backend boundary

`server.mjs` is a small integration boundary for the current static build:

- `/api/market/pools` proxies the fixed Pangea pool feed with a short cache.
- `/api/market/token-meta?launch=...` proxies Pangea token metadata so official token logos can be resolved without relying on browser cross-origin access.
- `/api/market/history`, `/api/market/bridge-activity`, and `/api/market/launches` proxy read-only activity and launch feeds with validation and short caches.
- `/api/market/trade-quote` and `/api/market/bridge-quote` proxy read-only route pricing. They return quote data only; they never sign or broadcast transaction calls.
- `/api/funding/intent` validates an asset, amount, wallet address, and server-owned allowlisted destination, then returns a short-lived intent. It does not move funds.
- `/api/health` exposes a minimal liveness check.
- Static responses include restrictive security headers and a small request-body/rate limit.

Before production, replace the in-memory rate limiter and cache with Redis, put the service behind a TLS-terminating reverse proxy, add structured audit logs, use a persistent database for funding intents, and require a separately audited signing/treasury service for any bot execution. Never put `CONI_FUNDING_RECIPIENTS` in browser code.

Run locally with `node backend/server.mjs`, then open `http://127.0.0.1:4173/`.
