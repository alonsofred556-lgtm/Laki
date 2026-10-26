# Coni operations MCP server

`operations-server.mjs` is a restricted Model Context Protocol process for operational visibility. It calls the Coni API through a fixed allowlist and requires `CONI_OPS_TOKEN`.

Available tools:

- `coni_health`
- `coni_bot_status`
- `coni_scan_opportunities`
- `coni_profit_summary`
- `coni_pause_bot`
- `coni_configure_bot`
- `coni_fee_config`

There are deliberately no tools for private keys, withdrawals, arbitrary transactions, signing, or changing fee destinations.
