# Coni

Coni is a cross-chain arbitrage operations console for Pangea launches.

This milestone ships a responsive website, a dedicated wallet control page, a Chrome-extension popup, and a guarded backend foundation. It authenticates wallet ownership with off-chain signatures, stores durable identity/ledger records when Postgres is configured, exposes read-only Pangea scanning, and keeps custody, withdrawals, and live signing fail-closed until a separate signer/treasury service is configured.

## Run locally

Serve this folder with any static web server, then open `index.html` through that server. For example:

```text
python -m http.server 4173
```

Open `http://127.0.0.1:4173/` for the website and `http://127.0.0.1:4173/wallet.html` for the wallet page. For the API boundary, run `node backend/server.mjs`. The extension source is in `extension/`; load that folder as an unpacked extension in Chrome when you are ready to preview the popup.

## Railway deployment

Create a Railway service from this repository and a managed PostgreSQL service. The current deployment URL is `https://laki-production.up.railway.app`. Railway will use the `start` script in `package.json`; it injects `PORT`, and the server binds to `0.0.0.0`. Set `CONI_ALLOWED_ORIGINS` to `https://laki-production.up.railway.app`, configure all variables in `backend/env.example`, and use `/api/health` as the service health-check path. Run the migration once with `npm run db:migrate` against the Railway database. Keep `CONI_EXECUTION_MODE=disabled` until the signer and execution services have been independently audited.

## Current scope

- Live read-only Pangea `/api/pools` polling with a safe fallback snapshot, plus backend-proxied token metadata/logo resolution.
- Pangea history, bridge-activity, launch-feed, trade-quote, and bridge-quote integrations for live discovery and read-only route previews.
- ZGAP-first opportunity cards using the official contract and Solana mint supplied for this project.
- An execution-plan boundary that explains the route, estimated fees, and risk gates without accepting arbitrary calldata or broadcasting transactions.
- Wallet control center with EIP-1193 EVM connections and Phantom/Solflare/Backpack-compatible Solana connections.
- Native balance reads and a funding-intent boundary that refuses arbitrary destinations.
- Coni-branded logo and wordmark assets reused from the supplied theme.
- Responsive website shell and compact extension popup.
- No seed phrases or private keys are exposed. Deposits, withdrawals, bridge transactions, and automatic trading remain disabled until the audited signer/treasury boundary is supplied.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [backend/README.md](backend/README.md) before adding wallet signing or execution.
# ClickOt
# Laki
