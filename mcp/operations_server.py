import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


API = os.environ.get("CONI_API_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("CONI_OPS_TOKEN", "")
TOOLS = [
    {"name": "coni_health", "description": "Read Coni backend health.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "coni_bot_status", "description": "Read bot mode, pause state, database, signer, and fee configuration status.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "coni_scan_opportunities", "description": "Run a read-only Pangea opportunity scan.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "coni_profit_summary", "description": "Read settled execution and expected-profit totals.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "coni_pause_bot", "description": "Pause or unpause the global bot. This never submits a trade.", "inputSchema": {"type": "object", "properties": {"paused": {"type": "boolean"}}, "required": ["paused"], "additionalProperties": False}},
    {"name": "coni_configure_bot", "description": "Set guarded bot policy; live signing remains separately gated.", "inputSchema": {"type": "object", "properties": {"enabled": {"type": "boolean"}, "paused": {"type": "boolean"}, "minNetBps": {"type": "integer"}, "maxNotionalUsd": {"type": "number"}}, "required": ["enabled", "minNetBps", "maxNotionalUsd"], "additionalProperties": False}},
    {"name": "coni_fee_config", "description": "Read fee rates and configured asset recipients without returning addresses.", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
]


def call(path, method="GET", payload=None, include_token=True):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if include_token:
        headers["X-Coni-Ops-Token"] = TOKEN
    request = Request(API + path, method=method, headers=headers, data=json.dumps(payload).encode() if payload is not None else None)
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode())
    except (HTTPError, URLError) as exc:
        raise RuntimeError(str(exc)) from exc


def dispatch(name, arguments):
    if name == "coni_health":
        return call("/api/health", include_token=False)
    if name == "coni_bot_status":
        return call("/api/bot/status")
    if name == "coni_scan_opportunities":
        return call("/api/bot/scan", "POST", {})
    if name == "coni_profit_summary":
        return call("/api/bot/profit")
    if name == "coni_pause_bot":
        if not isinstance(arguments.get("paused"), bool):
            raise ValueError("paused must be boolean")
        return call("/api/bot/pause", "POST", {"paused": arguments["paused"]})
    if name == "coni_configure_bot":
        return call("/api/bot/config", "POST", arguments)
    if name == "coni_fee_config":
        return call("/api/fees/config")
    raise ValueError(f"unknown tool: {name}")


def reply(request_id, result=None, exc=None):
    value = {"jsonrpc": "2.0", "id": request_id}
    if exc:
        value["error"] = {"code": -32000, "message": str(exc)}
    else:
        value["result"] = result
    sys.stdout.write(json.dumps(value) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    try:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            reply(request.get("id"), {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "coni-operations", "version": "0.2.0"}})
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply(request.get("id"), {"tools": TOOLS})
        elif method == "tools/call":
            result = dispatch(request.get("params", {}).get("name"), request.get("params", {}).get("arguments", {}))
            reply(request.get("id"), {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result})
        else:
            raise ValueError(f"unsupported method: {method}")
    except Exception as exc:
        reply(request.get("id") if "request" in locals() else None, exc=exc)
