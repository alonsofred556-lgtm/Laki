# Coni architecture boundary

## Product shape

Coni is a monitoring and execution surface for cross-chain opportunities on Pangea. The website is the full operations console. The Chrome extension is the fast scanner and approval handoff. Both should consume the same normalized opportunity model. Django is the canonical backend; the earlier Node boundary remains only as a migration fallback.

## Pangea facts used by this milestone

- Ethereum, Base, Robinhood Chain, and BSC use Uniswap v4 pools with native quote assets.
- Solana uses a Meteora DAMM v2 pool with SOL as the quote asset.
- A launch can have one token address on all four EVM chains; Solana has its own mint.
- Pangea exposes read-only pool data through `/api/pools` and documents `/trade/quote` as the route builder for future execution.
- The backend also proxies `/history`, `/bridge/activity`, `/token/meta`, `/v1/launches`, `/trade/quote`, and `/bridge/quote`; quote responses are expiring, untrusted data and are never signed or broadcast by Coni.
- A cross-chain transfer uses LayerZero OFT semantics and has its own fee and delivery risk. A bridge is not free inventory movement.
- Pangea publishes the LayerZero endpoint IDs and returns the unsigned bridge transaction flow. Coni maps chain names to endpoint IDs for validation; the signer service must inspect and sign the exact Pangea-produced transaction.

## Production boundaries

1. The browser must never receive a raw private key or seed phrase.
2. WalletConnect/external wallets sign; Coni only prepares and explains transactions.
3. Every opportunity is quoted for a concrete size. Spot spread alone is not profit.
4. Net edge must include trading fees, bridge fees, gas, quote-asset conversion, price impact, slippage, and a stale-quote buffer.
5. Execution is a state machine with idempotency keys, per-chain nonce control, and recovery for partial fills, bridge delays, reverted transactions, and insufficient native gas.
6. `CONI_EXECUTION_MODE=disabled` is the default. Live trading requires Postgres, an explicit bot policy, a separately audited signer/treasury service, quote simulation, and an emergency pause.
7. Fee destinations are server-side configuration only. Fee accrual is recorded in `fee_events`; transfer to the configured recipient is a signer/treasury responsibility.

## Wallet boundary

- The wallet page supports user-approved EIP-1193 EVM providers and public Solana wallet providers. It reads public addresses and balances only.
- An internal Coni Vault is deliberately staged, not browser-generated. A production embedded wallet requires an isolated signer, threshold authorization, hardware-backed secret storage, withdrawal allowlists, rate limits, pause/rotate controls, immutable audit events, and recovery drills.
- Funding intents are created by the backend from server-owned destinations. The client cannot choose a treasury address, and the backend does not broadcast the transaction.
- Wallet connection state is treated as untrusted input. Every address is validated for its target chain, every funding amount is bounded, and every quote expires.

## Planned services

- `market-data`: polls Pangea and normalizes pool snapshots.
- `opportunity-engine`: sizes both legs and rejects stale, shallow, or unprofitable routes.
- `execution-coordinator`: creates unsigned steps and tracks them; it never owns keys.
- `wallet-adapter`: EVM WalletConnect/injected providers plus Solana wallet-adapter.
- `ledger`: records quote, consent, transaction hashes, bridge messages, realized P&L, and failure reason.
- `auth`: nonce-based EVM/Solana wallet ownership verification and short-lived sessions.
- `mcp-operations`: fixed allowlist for health, scans, bot status, profit summaries, fee status, and pause/configuration; it cannot sign or withdraw.
- `django-api`: canonical Python API, Postgres migrations, wallet authentication, Pangea proxy, fee accounting, and bot policy endpoints.
- `signer-treasury`: separate private service, not included in this repository, responsible for policy-checked signing, withdrawals, fee sweeps, key ceremonies, and emergency recovery.

## First implementation gate

Read-only `/trade/quote` and `/bridge/quote` proxies are allowed for sizing and discovery. `/api/auth/*`, the Postgres schema, bot policy controls, fee accrual records, and the operations MCP surface are now implemented. Do not wire approvals, bridging, or transaction submission until the normalized schema, risk gates, signer contract, and failure state machine have integration tests. The application continues to make that limitation visible instead of presenting expected profit as realized returns.
