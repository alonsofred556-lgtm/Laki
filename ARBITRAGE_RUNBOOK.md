# Coni arbitrage runbook

Coni follows the public ZeroGap pattern for Pangea launches:

1. **Detect**: read every Pangea launch in the `/pools` snapshot and identify the lowest and highest priced pools for the same launch. Token addresses are compared per chain; ticker text is never used as identity.
2. **Close**: request a fresh Pangea `/trade/quote` for the buy and sell legs, then a canonical `/bridge/quote` for the LayerZero transfer. The decision subtracts both trade-fee reserves, bridge token fee, LayerZero/native fee, risk reserve, and a minimum-profit policy. A displayed gap without exact quotes is rejected.
3. **Finalize**: the bridge is tracked as `quoted -> source-submitted -> source-confirmed -> in-flight -> destination-confirmed -> settled` (or `failed`/`refunded`). The sell leg is not considered safe until the destination arrival is finalized.
4. **Settle**: only realized, reconciled net profit is prepared for payout to the user's Robinhood Chain wallet in USDG. There is no buyback allocation. Expected profit is never paid.

## Pangea bridge integration

The exact bridge amount is passed as token base units to `POST /bridge/quote`. Coni selects the canonical route and records `amountOut`, `nativeFee`, `tokenFee`, `setupFee`, `seconds`, and a hash of the returned unsigned call. When Solana is the source, it additionally calls `/bridge/solana-tx` and requires an unsigned versioned transaction (or an explicit setup/waiting stage) before the route can progress. The raw call/transaction is not accepted by the Django API as arbitrary calldata; a future signer adapter must re-fetch the quote, simulate the exact transaction, verify its expiry and allowlisted destination, then sign it through the audited HSM/MPC boundary.

Pangea documents that a transfer burns on the source chain and mints on the destination through LayerZero OFT. It is therefore not an atomic swap across chains. A source transaction can confirm while delivery is delayed or needs refund handling.

## Safe paper operation

Set the following in Railway's private environment:

```text
CONI_EXECUTION_MODE=disabled
CONI_ARBITRAGE_REQUIRE_QUOTES=true
CONI_BOT_WALLETS={"ethereum":"...","base":"...","robinhood":"...","bsc":"...","solana":"..."}
CONI_PROFIT_SETTLEMENT_ENABLED=false
CONI_PROFIT_SETTLEMENT_TOKEN_ADDRESS=<verified Robinhood USDG token address>
CONI_PROFIT_SETTLEMENT_DECIMALS=<verified USDG decimals>
```

The wallet values are public addresses used to request quotes, not private keys. A private HSM/MPC provisioner may activate a bot-wallet placeholder through `/api/internal/signer/bot-wallet-attest`; that callback must be private-network-only, mTLS-protected, and HMAC-signed over `timestamp + "." + exact request body` using `X-Coni-Signer-Timestamp` and `X-Coni-Signer-Signature`. It must send an opaque key reference plus an independently verifiable attestation hash, never key material. Start the scanner with:

```text
python manage.py scan_arbitrage --once
```

No command in this phase signs or broadcasts. Live execution still requires the independent signer service, HSM/MPC adapter, transaction simulation, approval quorum, bridge arrival worker, reconciliation, and an external security review.

## Bot-wallet attestation boundary

After the audited signer creates a wallet inside its HSM/MPC policy domain, it may call the private Django callback with the bot-wallet UUID, chain, public address, opaque key reference, and an attestation hash. Coni then marks that exact record active. The callback must be protected with private networking, mTLS, secret rotation, the short-lived HMAC replay boundary, and signer-side audit logs. Address rotation is rejected; provision a new audited record instead.
