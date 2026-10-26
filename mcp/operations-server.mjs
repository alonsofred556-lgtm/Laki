import readline from "node:readline";

const api = (process.env.CONI_API_URL || "http://127.0.0.1:4173").replace(/\/$/, "");
const token = process.env.CONI_OPS_TOKEN || "";

async function call(path, options = {}) {
  const response = await fetch(`${api}${path}`, { ...options, headers: { accept: "application/json", "content-type": "application/json", "x-coni-ops-token": token, ...(options.headers || {}) } });
  const data = await response.json().catch(() => ({ error: "invalid backend response" }));
  if (!response.ok) throw new Error(data.error || `backend responded ${response.status}`);
  return data;
}

const tools = [
  { name: "coni_health", description: "Read Coni backend health.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "coni_bot_status", description: "Read bot mode, pause state, database, signer, and fee configuration status.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "coni_scan_opportunities", description: "Run a read-only Pangea opportunity scan. It never signs or broadcasts.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "coni_profit_summary", description: "Read settled execution and expected-profit totals.", inputSchema: { type: "object", properties: {}, additionalProperties: false } },
  { name: "coni_pause_bot", description: "Pause or unpause the global bot. This never submits a trade.", inputSchema: { type: "object", properties: { paused: { type: "boolean" } }, required: ["paused"], additionalProperties: false } },
  { name: "coni_configure_bot", description: "Set the guarded bot policy. Live signing remains unavailable until the signer gate is satisfied.", inputSchema: { type: "object", properties: { enabled: { type: "boolean" }, paused: { type: "boolean" }, minNetBps: { type: "integer", minimum: 1 }, maxNotionalUsd: { type: "number", exclusiveMinimum: 0 } }, required: ["enabled", "minNetBps", "maxNotionalUsd"], additionalProperties: false } },
  { name: "coni_fee_config", description: "Read configured fee rates and which asset recipients exist; addresses are not returned.", inputSchema: { type: "object", properties: {}, additionalProperties: false } }
];

async function dispatch(name, args = {}) {
  if (name === "coni_health") return call("/api/health", { headers: { "x-coni-ops-token": undefined } });
  if (name === "coni_bot_status") return call("/api/bot/status");
  if (name === "coni_scan_opportunities") return call("/api/bot/scan", { method: "POST", body: "{}" });
  if (name === "coni_profit_summary") return call("/api/bot/profit");
  if (name === "coni_pause_bot") { if (typeof args.paused !== "boolean") throw new Error("paused must be boolean"); return call("/api/bot/pause", { method: "POST", body: JSON.stringify({ paused: args.paused }) }); }
  if (name === "coni_configure_bot") return call("/api/bot/config", { method: "POST", body: JSON.stringify(args) });
  if (name === "coni_fee_config") return call("/api/fees/config");
  throw new Error(`unknown tool: ${name}`);
}

function reply(id, result, error) { process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, ...(error ? { error: { code: -32000, message: error.message } } : { result }) })}\n`); }

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on("line", async (line) => {
  if (!line.trim()) return;
  let request;
  try { request = JSON.parse(line); } catch { return reply(null, null, new Error("invalid JSON-RPC request")); }
  try {
    if (request.method === "initialize") return reply(request.id, { protocolVersion: "2025-06-18", capabilities: { tools: {} }, serverInfo: { name: "coni-operations", version: "0.1.0" } });
    if (request.method === "notifications/initialized") return;
    if (request.method === "tools/list") return reply(request.id, { tools });
    if (request.method === "tools/call") { const data = await dispatch(request.params?.name, request.params?.arguments || {}); return reply(request.id, { content: [{ type: "text", text: JSON.stringify(data) }], structuredContent: data }); }
    return reply(request.id, null, new Error(`unsupported method: ${request.method}`));
  } catch (error) { return reply(request.id, null, error); }
});
