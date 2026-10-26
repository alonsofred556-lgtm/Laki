# Coni

Coni is a cross-chain arbitrage operations console for Pangea launches.

This milestone ships a responsive website, a dedicated wallet control page, a Chrome-extension popup, and a secure backend boundary. It is intentionally paper-mode for execution: it can connect to user-approved browser wallets and read public balances, but it does not request seed phrases, hold private keys, or submit funding/trading transactions.

## Run locally

Serve this folder with any static web server, then open `index.html` through that server. For example:

```text
python -m http.server 4173
```

Open `http://127.0.0.1:4173/` for the website and `http://127.0.0.1:4173/wallet.html` for the wallet page. For the API boundary, run `node backend/server.mjs`. The extension source is in `extension/`; load that folder as an unpacked extension in Chrome when you are ready to preview the popup.

## Current scope

- Live read-only Pangea `/api/pools` polling with a safe fallback snapshot, plus backend-proxied token metadata/logo resolution.
- Pangea history, bridge-activity, launch-feed, trade-quote, and bridge-quote integrations for activity and paper route previews.
- ZGAP-first opportunity cards using the official contract and Solana mint supplied for this project.
- A paper-execution preview that explains the route, estimated fees, and risk gates.
- Wallet control center with EIP-1193 EVM connections and Phantom/Solflare/Backpack-compatible Solana connections.
- Native balance reads and a funding-intent boundary that refuses arbitrary destinations.
- Coni-branded logo and wordmark assets reused from the supplied theme.
- Responsive website shell and compact extension popup.
- No seed phrases, private keys, custody, funding approvals, bridge transactions, or automatic trading.

See [ARCHITECTURE.md](ARCHITECTURE.md) and [backend/README.md](backend/README.md) before adding wallet signing or execution.
# ClickOt
# Laki
