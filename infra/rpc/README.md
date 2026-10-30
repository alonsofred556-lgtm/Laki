# Coni private RPC topology

Coni now includes a private RPC relay at `/api/rpc/<chain>`. The relay is the
only RPC URL the browser should use. It authenticates the wallet session,
applies a Redis-backed rate limit, allows only the chain methods Coni needs,
and accepts `eth_sendRawTransaction`/`sendTransaction` only when the payload
is already signed. It never accepts or stores private keys.

## What “custom RPC nodes” means here

The Django service is not a blockchain node. A real Ethereum-compatible or
Solana RPC node is a separate process with chain data, peer discovery, disk,
snapshots, upgrades, and monitoring. Railway should host the Coni API/relay;
run node processes on dedicated persistent compute or a node operator that
supports private networking. Put their private HTTPS endpoints in Railway
secrets, never in `config.js` or Git.

Configure one or more EVM endpoints per chain:

```text
CONI_RPC_URLS={"ethereum":["https://ethereum-node.internal","https://ethereum-node-2.internal"],"base":["https://base-node.internal"],"robinhood":["https://robinhood-node.internal"],"bsc":["https://bsc-node.internal"]}
CONI_SOLANA_RPC_URL=https://solana-node.internal
```

The first endpoint is tried first and the next endpoint is used only after a
transport failure. Provider credentials may be embedded in these server-side
URLs if the node operator requires them; they are never returned by the API.

## Node requirements before funding

For every EVM chain, the node must expose the JSON-RPC methods used by deposit
verification (`eth_chainId`, transaction/receipt reads, `eth_blockNumber`,
logs, calls, and gas estimation). It must be pinned to the correct network and
protected by firewall rules, TLS, authentication, request limits, and disk/
latency monitoring.

For Solana, use a node endpoint that supports finalized `getTransaction`,
`getLatestBlockhash`, account/token-account reads, simulation, and raw signed
transaction submission. Keep commitment at `finalized` for deposit crediting.

## Railway deployment checklist

1. Deploy the Django service with `CONI_RPC_URLS`, `CONI_SOLANA_RPC_URL`,
   `CONI_REDIS_URL`, PostgreSQL, and the existing production secrets.
2. Allow the Railway service to reach the private node network. If a node is
   public, restrict it with an API key, IP allowlist, TLS, and provider-side
   quotas.
3. Open `/api/health` after deployment. Each chain should show
   `configured: true`; this only verifies configuration, not chain finality.
4. Run a read-only smoke test through an authenticated wallet session before
   enabling funding. Test chain identity, latest block, balance, simulation,
   and receipt lookup on every chain.
5. Keep live signing disabled until the independent signer/HSM/MPC review,
   approval controls, reconciliation worker, and withdrawal drills are done.

The relay is intentionally not a substitute for a signer service. An RPC node
can broadcast a signed transaction, but it must never receive a private key.
