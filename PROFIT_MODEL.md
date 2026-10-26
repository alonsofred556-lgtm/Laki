# Coni profit model

Coni must report realized profit only after both trading legs, bridge delivery where applicable, and ledger settlement are confirmed.

```text
realized P&L
= sell proceeds
- buy cost
- trading fees
- gas on every chain
- bridge and LayerZero message fees
- router/relayer fees
- slippage and price impact
- failed-route and inventory reserve
- Coni fee
```

The scanner reports an opportunity estimate, not a promise. A route is eligible only if the simulated net result exceeds the configured `minNetBps`, has sufficient depth, uses a fresh quote, and passes the chain/contract/recipient policy.

Cross-chain routes are not atomic. The first transaction can confirm while the destination leg is delayed or fails. The execution coordinator must therefore reserve inventory, track bridge state, and book profit only after settlement.

Do not use a fixed “90–99% profit” rule. A large displayed spread can be stale, illiquid, or malicious. Use net basis points after all costs and a conservative reserve. For rollout, use Coni-owned test funds, then small same-chain limits, then expand only after reconciliation and recovery tests pass.
