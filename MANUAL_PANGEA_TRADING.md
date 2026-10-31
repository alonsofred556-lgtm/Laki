# Manual Pangea trading

Coni's manual mode is user-signed. The scanner and quote workers may discover
routes, but they never sign or broadcast transactions. The connected wallet
approves every swap, bridge, and setup transaction.

## Supported route classes

- EVM to EVM: Ethereum, Base, Robinhood Chain, and BNB Chain.
- Solana to EVM: Solana to any supported EVM chain.
- EVM to Solana: requires the Pangea route-opening flow before the send.

The API boundary is:

```text
POST /api/manual-arbitrage/prepare
POST /api/manual-arbitrage/solana-swap
POST /api/manual-arbitrage/solana-bridge
POST /api/manual-arbitrage/evm-swap
POST /api/manual-arbitrage/status
```

The server validates wallet ownership, chain, token slug, amount, slippage,
and session identity. It returns Pangea quote/transaction data only. Private
keys never enter Django, Coni, or the request body.

## Profit gate

The UI must show a trade action only when a fresh quote proves:

```text
expected destination value
- source gas
- destination gas
- Pangea trading fee
- bridge fee and relayer fee
- Coni fee
- slippage/risk buffer
> 0
```

An observed pool spread is not sufficient. Quotes must be refreshed immediately
before a wallet approval and invalidated when their expiry, price, route, or
minimum-received value changes.

## Wallet signing

EVM transactions are signed through the connected EIP-1193 provider. Solana
transactions are base64 versioned transactions returned by Pangea and are
signed through the connected Solana provider. The browser must display the
chain, recipient/program, value, minimum received, and fee before requesting
approval.

## Deployment mode

Manual mode does not require the custody adapter, Turnkey, MPC coordinator, or
signer provisioner:

```env
CONI_CUSTODY_BACKEND=disabled
CONI_EXECUTION_MODE=disabled
```

Keep the scanner, quote, PostgreSQL, Redis, and Django services. Do not enable
the old automatic execution planner. Keep historical execution tables until
manual execution history has been reconciled and migrated.

## Pangea integration references

Pangea documents trade quotes, Solana transaction builders, bridge quotes,
Solana bridge transactions, EVM OFT bridging, chain transaction status, and
bridge arrival tracking in its integrator documentation:

https://www.pangea.trade/app/docs

The remaining implementation gate is to test one real response for each route
class and map its exact transaction fields before enabling wallet broadcasts.
