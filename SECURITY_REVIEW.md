# Coni security review gate

This is an engineering checklist, not a claim that Coni has been audited. The live execution gate must remain closed until every item below has evidence attached to the release.

## Required before real funds

- [ ] Independent review of wallet challenge construction, EVM signature recovery, Solana signature verification, replay protection, session cookies, CSRF and origin policy.
- [ ] PostgreSQL migration reviewed on a staging clone; backups and a restore drill completed.
- [ ] Ledger invariant tests: double-entry balance, idempotent deposits, concurrent withdrawals, compensating entries, and no negative available balance.
- [ ] RPC provider identity, finality, reorg rewind, token contract/mint, decimals, and event decoding verified on every supported chain.
- [ ] Automatic discovery tested with duplicate transactions, duplicate logs, reorgs, provider timeouts, malformed RPC data, unclaimed transfers, and wrong-wallet claims.
- [ ] Redis outage behavior tested; auth, funding, and withdrawal endpoints fail closed when required rate limiting is unavailable.
- [ ] Signer/treasury review completed by an independent specialist. HSM/MPC keys, quorum, rotation, recovery, allowlists, transaction simulation, nonces, expiry, quote hash, calldata hash, limits, and two-person approval are documented and tested.
- [ ] Pangea and LayerZero quote expiry, slippage, bridge delay, partial failure, delivery/refund, and non-atomic settlement behavior tested without assuming cross-chain atomicity.
- [ ] Dependency, secret, SAST, DAST, API authorization, and penetration tests completed; no private keys or production database credentials in Git history.
- [ ] Incident runbooks tested: pause bot, stop signer, freeze withdrawals, rotate credentials, restore database, reconcile chain balances, and communicate an asset incident.

## Release evidence

Record the commit, reviewer, environment, test result, and date for every checked item. Set `CONI_SIGNER_AUDIT_APPROVED=true` only from the approved release process, never as a developer convenience.
