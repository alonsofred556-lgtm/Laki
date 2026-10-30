# Coni fee policy

Coni represents `0.1%` as `10` basis points (`10 / 10,000`). The fee is
calculated from integer base units on the Django server. Browser-provided fee
values are never trusted.

## Railway variables

Set these in the Django web and worker services:

```text
CONI_TRADING_FEE_BPS=10
CONI_BOT_FUNDING_FEE_BPS=10
CONI_BOT_WITHDRAWAL_FEE_BPS=10
CONI_WITHDRAWAL_FEE_BPS=10
CONI_FEE_RECIPIENTS={"ETH":{"ethereum":"0x...","base":"0x...","robinhood":"0x..."},"BNB":{"bsc":"0x..."},"SOL":{"solana":"..."},"ZGAP":{"ethereum":"0x...","base":"0x...","robinhood":"0x...","bsc":"0x...","solana":"..."}}
CONI_SIGNER_FEE_KEY_REFS={"ethereum":"hsm:fee:ethereum","base":"hsm:fee:base","robinhood":"hsm:fee:robinhood","bsc":"hsm:fee:bsc","solana":"hsm:fee:solana"}
```

Use a chain-specific recipient. The fee recipient must be independently
checked on the correct network before it is entered. Never put a private key
in any of these variables; `CONI_SIGNER_FEE_KEY_REFS` contains opaque key
references understood only by the private signer.

## What the user pays

- Funding: the user sends the gross amount. After finality verification, the
  bot ledger receives `gross - fee`; the fee is queued for the configured fee
  wallet.
- Bot withdrawal: the requested amount is sent to the authenticated wallet;
  the bot ledger reserves `requested amount + fee` and queues the fee transfer.
- Normal withdrawal: the same amount-plus-fee reservation is used. The
  withdrawal remains manually approved and signer-gated.
- Bot trade: at a settled route, Coni charges 10 bps of the buy-leg native
  input amount on the buy chain and queues a separate fee-wallet transfer.

Every fee is durable, source-bound, idempotent, and visible in the user
activity feed. A missing recipient or fee signer reference fails closed. The
fee queue does not sign or broadcast: the private custody adapter must pull
`/api/internal/signer/fee-intents` and return proof to
`/api/internal/signer/fee-event`.

Fee transfer states are `pending`, `leased`, `submitted`, `confirmed`, or
`failed`. Confirmed events require a transaction proof and finalized chain
evidence. A fee is not described as paid merely because it was recorded in
PostgreSQL.
