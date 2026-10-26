# Coni Railway deployment

## Services

Create two Railway services in the same project:

1. Coni web/API service from this repository.
2. Railway PostgreSQL service.

The web service uses Gunicorn with Django and receives Railway's `PORT` automatically. The old Node server is retained only as a migration fallback; it is not the Railway entrypoint.

## Required variables

Set these on the Coni service. Generate secrets outside the repository:

```text
DJANGO_DEBUG=false
DJANGO_SECRET_KEY=<at least 50 random characters>
DJANGO_ALLOWED_HOSTS=laki-production.up.railway.app
CONI_ALLOWED_ORIGINS=https://laki-production.up.railway.app
DATABASE_URL=${{Postgres.DATABASE_URL}}
DATABASE_SSL=require
CONI_AUTH_DOMAIN=laki-production.up.railway.app
CONI_AUTH_URI=https://laki-production.up.railway.app
CONI_OPS_TOKEN=<separate random operations token>
CONI_EXECUTION_MODE=disabled
CONI_SIGNER_URL=
CONI_LIVE_TRADING_CONFIRMATION=
CONI_FUNDING_RECIPIENTS={"ETH":"<reviewed Ethereum destination>","BNB":"<reviewed BNB destination>","SOL":"<reviewed Solana destination>"}
CONI_FEE_RECIPIENTS={"ETH":"<reviewed Ethereum fee destination>","BNB":"<reviewed BNB fee destination>","SOL":"<reviewed Solana fee destination>"}
CONI_TRADING_FEE_BPS=25
CONI_BOT_FUNDING_FEE_BPS=0
CONI_DEFAULT_COST_BUFFER_BPS=55
CONI_RPC_URLS={"ethereum":"<server-side RPC URL>","base":"<server-side RPC URL>","robinhood":"<server-side RPC URL>","bsc":"<server-side RPC URL>"}
CONI_EXPECTED_CHAIN_IDS={"ethereum":"0x1","base":"0x2105","bsc":"0x38"}
CONI_SOLANA_RPC_URL=<server-side Solana RPC URL>
CONI_DEPOSIT_CONFIRMATIONS=12
CONI_DEPOSIT_MAX_AGE_MINUTES=1440
CONI_DEPOSIT_SCAN_BATCH=50
```

Do not use zero addresses, burn addresses, user wallet addresses, or example values. Verify every destination on the intended chain before accepting funds.

## Migration

Run once against the Railway PostgreSQL service from a trusted environment:

```text
python -m pip install -r requirements.lock
python manage.py migrate
python manage.py check --deploy
```

The migration creates wallet challenges, identities, ledger accounts, funding intents, deposit observations, withdrawal requests, fee events, bot controls, and execution records.

## Funding verification worker

The funding flow is non-custodial: the user approves a transfer in an external wallet, submits the transaction hash, and a separate verifier confirms the exact sender, destination, amount, successful receipt, network identity, and finality before crediting the ledger. Create a second Railway service from this repository and set its config file to `railway.worker.toml`; it uses the same database and server-side RPC variables:

```text
python manage.py verify_deposits --loop-seconds 30
```

For a one-off test:

```text
python manage.py verify_deposits --once
```

Do not run the worker until every RPC URL and expected chain ID has been checked. The worker never signs, broadcasts, or withdraws funds.

## Smoke checks

```text
https://laki-production.up.railway.app/api/health
https://laki-production.up.railway.app/api/bot/status
```

The expected initial bot status is `mode: "disabled"`, `paused: true`, and `executionReady: false`.

## Operations MCP process

Run the MCP operations process separately with the same API URL and operations token:

```text
$env:CONI_API_URL="https://laki-production.up.railway.app"
$env:CONI_OPS_TOKEN="<same operations token>"
python mcp/operations_server.py
```

The MCP server can inspect health, scan opportunities, view profit summaries, read fee configuration, and pause the bot. It intentionally has no withdrawal or signing tool.

## Live execution gate

Do not set live mode until a separate signer/treasury service exists. That service must validate chain, contract, recipient, calldata, quote expiry, slippage, simulation results, and limits before signing. Only after that service is audited should the following be considered:

```text
CONI_EXECUTION_MODE=live
CONI_SIGNER_URL=https://<private-signer-service>
CONI_LIVE_TRADING_CONFIRMATION=I_UNDERSTAND_LIVE_RISK
```

These variables alone do not implement custody. They only remove the Coni application-level gate after the signer endpoint is supplied.
