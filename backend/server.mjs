import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { createChallenge, verifyChallenge, validWallet } from "./auth.mjs";
import { dbEnabled, migrate, query, memory } from "./db.mjs";
import { createSession, readSession, sessionCookie } from "./session.mjs";

const ROOT = path.resolve(fileURLToPath(new URL("..", import.meta.url)));
const PORT = Number(process.env.PORT || 4173);
const HOST = process.env.HOST || "0.0.0.0";
const PANGEA_POOLS = "https://www.pangea.trade/api/pools";
const PANGEA_TOKEN_META = "https://www.pangea.trade/api/token/meta";
const PANGEA_HISTORY = "https://www.pangea.trade/api/history";
const PANGEA_BRIDGE_ACTIVITY = "https://www.pangea.trade/api/bridge/activity";
const PANGEA_TRADE_QUOTE = "https://www.pangea.trade/api/trade/quote";
const PANGEA_BRIDGE_QUOTE = "https://www.pangea.trade/api/bridge/quote";
const PANGEA_LAUNCHES = "https://www.pangea.trade/api/v1/launches";
const EXECUTION_MODE = process.env.CONI_EXECUTION_MODE || "disabled";
const OPS_TOKEN = process.env.CONI_OPS_TOKEN || "";
const ORIGINS = new Set((process.env.CONI_ALLOWED_ORIGINS || `http://127.0.0.1:${PORT},http://localhost:${PORT}`).split(",").map((value) => value.trim()).filter(Boolean));
const RATE_WINDOW_MS = 60_000;
const RATE_LIMIT = 120;
const buckets = new Map();
let poolsCache = { expiresAt: 0, body: null };
const tokenMetaCache = new Map();
const readCache = new Map();

function headers(origin) {
  const allowed = origin && ORIGINS.has(origin) ? origin : [...ORIGINS][0];
  return {
    "access-control-allow-origin": allowed,
    "access-control-allow-credentials": "true",
    "access-control-allow-headers": "content-type, x-coni-ops-token",
    "access-control-allow-methods": "GET,POST,OPTIONS",
    "cache-control": "no-store",
    "content-security-policy": "default-src 'self'; connect-src 'self' https://www.pangea.trade https://api.mainnet-beta.solana.com; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "referrer-policy": "no-referrer",
    "strict-transport-security": process.env.NODE_ENV === "production" ? "max-age=31536000; includeSubDomains" : "max-age=0",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "permissions-policy": "camera=(), microphone=(), geolocation=()"
  };
}

function send(res, status, body, origin, extra = {}) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { ...headers(origin), ...extra, "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(payload) });
  res.end(payload);
}

function rateLimited(req) {
  const key = req.headers["x-forwarded-for"]?.split(",")[0]?.trim() || req.socket.remoteAddress || "unknown";
  const now = Date.now();
  const item = buckets.get(key);
  if (!item || now - item.startedAt > RATE_WINDOW_MS) { buckets.set(key, { startedAt: now, count: 1 }); return false; }
  item.count += 1;
  return item.count > RATE_LIMIT;
}

async function body(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 16 * 1024) throw Object.assign(new Error("request body too large"), { status: 413 });
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try {
    const parsed = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("invalid JSON object");
    return parsed;
  } catch { throw Object.assign(new Error("invalid JSON"), { status: 400 }); }
}

function validAddress(value, asset) {
  if (typeof value !== "string") return false;
  if (["ETH", "BNB"].includes(asset)) return /^0x[a-fA-F0-9]{40}$/.test(value) && !/^0x0{40}$/i.test(value);
  if (asset === "SOL") return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value) && value !== "11111111111111111111111111111111";
  return false;
}

function fundingConfig() {
  try { return JSON.parse(process.env.CONI_FUNDING_RECIPIENTS || "{}"); } catch { return {}; }
}

function validSlug(value) { return typeof value === "string" && /^[a-z0-9][a-z0-9-]{1,80}$/i.test(value); }
function validChain(value) { return ["ethereum", "base", "robinhood", "bsc", "solana"].includes(value); }
function validAmount(value) { return typeof value === "string" && /^\d{1,78}$/.test(value) && BigInt(value) > 0n; }
function validSlippage(value) { return value === undefined || (Number.isInteger(value) && value >= 1 && value <= 9900); }
function decimalToBaseUnits(value, decimals) {
  if (typeof value !== "string" || !/^\d+(\.\d+)?$/.test(value)) return null;
  const [whole, fraction = ""] = value.split(".");
  if (fraction.length > decimals) return null;
  const base = `${whole}${fraction.padEnd(decimals, "0")}`.replace(/^0+(?=\d)/, "");
  return /^\d+$/.test(base) && BigInt(base) > 0n ? base : null;
}

function feeConfig() {
  try { return { recipients: JSON.parse(process.env.CONI_FEE_RECIPIENTS || "{}"), tradingFeeBps: Number(process.env.CONI_TRADING_FEE_BPS || 25), fundingFeeBps: Number(process.env.CONI_BOT_FUNDING_FEE_BPS || 0) }; }
  catch { return { recipients: {}, tradingFeeBps: 25, fundingFeeBps: 0 }; }
}

function session(req) { return readSession(req); }
function requireSession(req) { const value = session(req); if (!value) throw Object.assign(new Error("authenticated wallet session required"), { status: 401 }); return value; }
function requireOps(req) { if (!OPS_TOKEN || req.headers["x-coni-ops-token"] !== OPS_TOKEN) throw Object.assign(new Error("operations authorization required"), { status: 403 }); }
function executionReady() { return EXECUTION_MODE === "live" && Boolean(process.env.CONI_SIGNER_URL) && process.env.CONI_LIVE_TRADING_CONFIRMATION === "I_UNDERSTAND_LIVE_RISK"; }

async function botStatus() {
  const row = dbEnabled ? (await query("SELECT * FROM bot_controls WHERE scope = 'global'")).rows[0] : memory.get("bot:global") || { enabled: false, paused: true, min_net_bps: 50, max_notional_usd: 25 };
  return { mode: EXECUTION_MODE, executionReady: executionReady(), database: dbEnabled, enabled: Boolean(row?.enabled), paused: Boolean(row?.paused), minNetBps: Number(row?.min_net_bps || 50), maxNotionalUsd: Number(row?.max_notional_usd || 25), signerConfigured: Boolean(process.env.CONI_SIGNER_URL), feeConfigConfigured: Object.keys(feeConfig().recipients).length > 0 };
}

async function scanOpportunities() {
  const upstream = await fetch(PANGEA_POOLS, { headers: { accept: "application/json", "user-agent": "ConiOpportunityEngine/0.1" }, signal: AbortSignal.timeout(8_000) });
  if (!upstream.ok) throw Object.assign(new Error("market data upstream unavailable"), { status: 502 });
  const data = await upstream.json();
  const rows = [];
  for (const launch of Array.isArray(data.launches) ? data.launches : []) {
    const pools = Array.isArray(launch.pools) ? launch.pools.filter((pool) => Number(pool.priceUsd) > 0) : [];
    if (pools.length < 2) continue;
    const buy = pools.reduce((a, b) => Number(a.priceUsd) < Number(b.priceUsd) ? a : b);
    const sell = pools.reduce((a, b) => Number(a.priceUsd) > Number(b.priceUsd) ? a : b);
    const grossBps = Math.floor(((Number(sell.priceUsd) - Number(buy.priceUsd)) / Number(buy.priceUsd)) * 10_000);
    const netBps = grossBps - Number(process.env.CONI_DEFAULT_COST_BUFFER_BPS || 55);
    rows.push({ slug: launch.slug, symbol: launch.symbol, buyChain: buy.chain, sellChain: sell.chain, grossBps, netBps, depthUsd: Math.min(Number(buy.depth || 0), Number(sell.depth || 0)), executable: netBps > 0 });
  }
  return rows.sort((a, b) => b.netBps - a.netBps).slice(0, 100);
}

async function proxyJson(url, options = {}) {
  const upstream = await fetch(url, { ...options, headers: { accept: "application/json", "user-agent": "ConiMarketData/0.1", ...(options.headers || {}) }, signal: AbortSignal.timeout(10_000) });
  const data = await upstream.json().catch(() => ({ error: "invalid upstream response" }));
  return { status: upstream.status, data };
}

async function handleApi(req, res, url) {
  const origin = req.headers.origin;
  if (url.pathname === "/api/health" && req.method === "GET") return send(res, 200, { ok: true, service: "coni-backend", time: new Date().toISOString() }, origin);
  if (url.pathname === "/api/auth/challenge" && req.method === "POST") {
    const input = await body(req);
    if (Object.keys(input).some((key) => !["chain", "address", "origin"].includes(key)) || !validWallet(input.chain, input.address)) return send(res, 400, { error: "invalid wallet challenge" }, origin);
    const result = await createChallenge({ chain: input.chain, address: input.address, origin: input.origin || origin });
    return send(res, 201, result, origin);
  }
  if (url.pathname === "/api/auth/verify" && req.method === "POST") {
    const input = await body(req);
    if (Object.keys(input).some((key) => !["challengeId", "address", "signature"].includes(key)) || typeof input.challengeId !== "string" || typeof input.signature !== "string") return send(res, 400, { error: "invalid wallet verification" }, origin);
    const identity = await verifyChallenge(input);
    const token = createSession({ walletIdentityId: identity.id, chain: identity.chain, address: identity.address });
    return send(res, 200, { authenticated: true, wallet: identity }, origin, { "set-cookie": sessionCookie(token) });
  }
  if (url.pathname === "/api/auth/session" && req.method === "GET") {
    const current = session(req);
    return send(res, 200, current ? { authenticated: true, wallet: { id: current.walletIdentityId, chain: current.chain, address: current.address } } : { authenticated: false }, origin);
  }
  if (url.pathname === "/api/auth/logout" && req.method === "POST") return send(res, 200, { authenticated: false }, origin, { "set-cookie": "coni_session=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax" });
  if (url.pathname === "/api/bot/status" && req.method === "GET") return send(res, 200, await botStatus(), origin);
  if (url.pathname === "/api/bot/pause" && req.method === "POST") {
    requireOps(req);
    const input = await body(req);
    if (typeof input.paused !== "boolean") return send(res, 400, { error: "paused must be boolean" }, origin);
    if (dbEnabled) await query("UPDATE bot_controls SET paused = $1, updated_at = now() WHERE scope = 'global'", [input.paused]);
    else memory.set("bot:global", { ...(memory.get("bot:global") || { enabled: false, min_net_bps: 50, max_notional_usd: 25 }), paused: input.paused });
    return send(res, 200, await botStatus(), origin);
  }
  if (url.pathname === "/api/bot/config" && req.method === "POST") {
    requireOps(req);
    const input = await body(req);
    const minNetBps = Number(input.minNetBps);
    const maxNotionalUsd = Number(input.maxNotionalUsd);
    if (!Number.isInteger(minNetBps) || minNetBps < 1 || minNetBps > 10_000 || !Number.isFinite(maxNotionalUsd) || maxNotionalUsd <= 0 || maxNotionalUsd > 1_000_000 || typeof input.enabled !== "boolean") return send(res, 400, { error: "invalid bot configuration" }, origin);
    if (dbEnabled) await query("UPDATE bot_controls SET enabled = $1, paused = $2, min_net_bps = $3, max_notional_usd = $4, updated_at = now() WHERE scope = 'global'", [input.enabled, input.paused !== false, minNetBps, maxNotionalUsd]);
    else memory.set("bot:global", { enabled: input.enabled, paused: input.paused !== false, min_net_bps: minNetBps, max_notional_usd: maxNotionalUsd });
    return send(res, 200, await botStatus(), origin);
  }
  if (url.pathname === "/api/fees/config" && req.method === "GET") {
    requireOps(req);
    const fees = feeConfig();
    return send(res, 200, { tradingFeeBps: fees.tradingFeeBps, fundingFeeBps: fees.fundingFeeBps, recipientAssets: Object.keys(fees.recipients) }, origin);
  }
  if (url.pathname === "/api/fees/accrue" && req.method === "POST") {
    requireOps(req);
    const input = await body(req);
    const fees = feeConfig();
    const asset = String(input.asset || "").toUpperCase();
    const recipient = fees.recipients[asset];
    if (!recipient || !validAddress(recipient, asset) || !validAmount(String(input.amountBaseUnits || "")) || !validChain(input.chain)) return send(res, 400, { error: "invalid fee accrual" }, origin);
    const id = randomUUID();
    if (dbEnabled) await query("INSERT INTO fee_events (id, execution_id, asset, chain, amount_base_units, recipient, status) VALUES ($1,$2,$3,$4,$5,$6,'accrued')", [id, input.executionId || null, asset, input.chain, String(input.amountBaseUnits), recipient]);
    return send(res, 201, { id, status: "accrued", asset, chain: input.chain, amountBaseUnits: String(input.amountBaseUnits), recipient }, origin);
  }
  if (url.pathname === "/api/market/pools" && req.method === "GET") {
    if (poolsCache.body && poolsCache.expiresAt > Date.now()) return send(res, 200, poolsCache.body, origin);
    try {
      const upstream = await fetch(PANGEA_POOLS, { headers: { accept: "application/json", "user-agent": "ConiMarketData/0.1" }, signal: AbortSignal.timeout(8000) });
      if (!upstream.ok) return send(res, 502, { error: "market data upstream unavailable" }, origin);
      const data = await upstream.json();
      poolsCache = { body: data, expiresAt: Date.now() + 15_000 };
      return send(res, 200, data, origin);
    } catch {
      return send(res, 502, { error: "market data upstream unavailable" }, origin);
    }
  }
  if (url.pathname === "/api/market/token-meta" && req.method === "GET") {
    const launch = url.searchParams.get("launch") || "";
    if (!/^[a-z0-9][a-z0-9-]{1,80}$/i.test(launch)) return send(res, 400, { error: "invalid launch" }, origin);
    const cached = tokenMetaCache.get(launch);
    if (cached && cached.expiresAt > Date.now()) return send(res, 200, cached.body, origin);
    try {
      const upstream = await fetch(`${PANGEA_TOKEN_META}?launch=${encodeURIComponent(launch)}`, { headers: { accept: "application/json", "user-agent": "ConiMarketData/0.1" }, signal: AbortSignal.timeout(8000) });
      if (!upstream.ok) return send(res, 502, { error: "token metadata upstream unavailable" }, origin);
      const data = await upstream.json();
      tokenMetaCache.set(launch, { body: data, expiresAt: Date.now() + 60_000 });
      return send(res, 200, data, origin);
    } catch {
      return send(res, 502, { error: "token metadata upstream unavailable" }, origin);
    }
  }
  if (url.pathname === "/api/market/history" && req.method === "GET") {
    const launch = url.searchParams.get("launch") || "";
    const hours = Number(url.searchParams.get("hours") || 24);
    if (!validSlug(launch) || !Number.isInteger(hours) || hours < 1 || hours > 168) return send(res, 400, { error: "invalid history query" }, origin);
    const key = `history:${launch}:${hours}`;
    const cached = readCache.get(key);
    if (cached && cached.expiresAt > Date.now()) return send(res, 200, cached.body, origin);
    try {
      const result = await proxyJson(`${PANGEA_HISTORY}?launch=${encodeURIComponent(launch)}&hours=${hours}`);
      if (result.status >= 400) return send(res, result.status === 404 ? 404 : 502, result.data, origin);
      readCache.set(key, { body: result.data, expiresAt: Date.now() + 45_000 });
      return send(res, 200, result.data, origin);
    } catch { return send(res, 502, { error: "history upstream unavailable" }, origin); }
  }
  if (url.pathname === "/api/market/bridge-activity" && req.method === "GET") {
    const token = url.searchParams.get("token") || "";
    const hours = Number(url.searchParams.get("hours") || 24);
    const limit = Number(url.searchParams.get("limit") || 25);
    if (!validSlug(token) || !Number.isInteger(hours) || hours < 1 || hours > 168 || !Number.isInteger(limit) || limit < 1 || limit > 100) return send(res, 400, { error: "invalid bridge activity query" }, origin);
    const key = `bridge:${token}:${hours}:${limit}`;
    const cached = readCache.get(key);
    if (cached && cached.expiresAt > Date.now()) return send(res, 200, cached.body, origin);
    try {
      const result = await proxyJson(`${PANGEA_BRIDGE_ACTIVITY}?token=${encodeURIComponent(token)}&hours=${hours}&limit=${limit}`);
      if (result.status >= 400) return send(res, result.status === 404 ? 404 : 502, result.data, origin);
      readCache.set(key, { body: result.data, expiresAt: Date.now() + 45_000 });
      return send(res, 200, result.data, origin);
    } catch { return send(res, 502, { error: "bridge activity upstream unavailable" }, origin); }
  }
  if (url.pathname === "/api/market/launches" && req.method === "GET") {
    const limit = Number(url.searchParams.get("limit") || 50);
    const before = url.searchParams.get("before");
    const since = url.searchParams.get("since");
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (before && before.length > 180) || (since && since.length > 180)) return send(res, 400, { error: "invalid launches query" }, origin);
    const params = new URLSearchParams({ limit: String(limit) });
    if (before) params.set("before", before);
    if (since) params.set("since", since);
    const key = `launches:${params.toString()}`;
    const cached = readCache.get(key);
    if (cached && cached.expiresAt > Date.now()) return send(res, 200, cached.body, origin);
    try {
      const result = await proxyJson(`${PANGEA_LAUNCHES}?${params}`);
      if (result.status >= 400) return send(res, 502, result.data, origin);
      readCache.set(key, { body: result.data, expiresAt: Date.now() + 15_000 });
      return send(res, 200, result.data, origin);
    } catch { return send(res, 502, { error: "launch feed upstream unavailable" }, origin); }
  }
  if (url.pathname === "/api/market/trade-quote" && req.method === "POST") {
    const input = await body(req);
    const allowedKeys = new Set(["slug", "side", "chain", "amount", "fromChain", "user", "solanaUser", "recipient", "slippageBps"]);
    if (Object.keys(input).some((key) => !allowedKeys.has(key)) || !validSlug(input.slug) || !["buy", "sell"].includes(input.side) || !(input.chain === "auto" || validChain(input.chain)) || !validAmount(input.amount) || !validSlippage(input.slippageBps)) return send(res, 400, { error: "invalid trade quote" }, origin);
    if (input.fromChain && !validChain(input.fromChain)) return send(res, 400, { error: "invalid source chain" }, origin);
    try {
      const result = await proxyJson(PANGEA_TRADE_QUOTE, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input) });
      return send(res, result.status, result.data, origin);
    } catch { return send(res, 502, { error: "trade quote upstream unavailable" }, origin); }
  }
  if (url.pathname === "/api/market/bridge-quote" && req.method === "POST") {
    const input = await body(req);
    const allowedKeys = new Set(["slug", "from", "to", "amount", "user", "solanaUser", "recipient", "slippageBps"]);
    if (Object.keys(input).some((key) => !allowedKeys.has(key)) || !validSlug(input.slug) || !validChain(input.from) || !validChain(input.to) || input.from === input.to || !validAmount(input.amount) || !validSlippage(input.slippageBps)) return send(res, 400, { error: "invalid bridge quote" }, origin);
    try {
      const result = await proxyJson(PANGEA_BRIDGE_QUOTE, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(input) });
      return send(res, result.status, result.data, origin);
    } catch { return send(res, 502, { error: "bridge quote upstream unavailable" }, origin); }
  }
  if (url.pathname === "/api/bot/scan" && req.method === "POST") {
    requireOps(req);
    const rows = await scanOpportunities();
    const status = await botStatus();
    return send(res, 200, { asOf: new Date().toISOString(), status, opportunities: rows, note: "Discovery only. No transaction was signed or broadcast." }, origin);
  }
  if (url.pathname === "/api/bot/profit" && req.method === "GET") {
    requireOps(req);
    const result = dbEnabled ? await query("SELECT COUNT(*)::int AS executions, COALESCE(SUM(expected_profit_usd),0)::text AS expected_profit_usd FROM executions WHERE state = 'settled'") : { rows: [{ executions: 0, expected_profit_usd: "0" }] };
    return send(res, 200, { executions: result.rows[0].executions, expectedProfitUsd: result.rows[0].expected_profit_usd, realizedProfitUsd: null, note: "Realized profit is reported only after confirmed fills and ledger settlement." }, origin);
  }
  if (url.pathname === "/api/execution/plan" && req.method === "POST") {
    const authenticated = requireSession(req);
    const input = await body(req);
    if (!validSlug(input.slug) || !validAmount(String(input.amount || "")) || !validSlippage(input.slippageBps)) return send(res, 400, { error: "invalid execution plan" }, origin);
    const status = await botStatus();
    if (!status.executionReady || status.paused || !status.enabled) return send(res, 409, { error: "execution is gated", status, next: "Configure the signer, database, fee recipients, risk policy, and explicitly enable the bot." }, origin);
    const executionId = randomUUID();
    if (dbEnabled) await query("INSERT INTO executions (id, user_identity_id, slug, state) VALUES ($1,$2,$3,'quoted')", [executionId, authenticated.walletIdentityId, input.slug]);
    return send(res, 202, { executionId, state: "quoted", next: "A signer adapter must validate and sign the Pangea transaction steps; this server does not accept arbitrary calldata." }, origin);
  }
  if (url.pathname === "/api/funding/intent" && req.method === "POST") {
    const authenticated = requireSession(req);
    const input = await body(req);
    const asset = String(input.asset || "").toUpperCase();
    const amount = String(input.amount || "");
    if (!["ETH", "BNB", "SOL"].includes(asset) || !/^\d+(\.\d{1,18})?$/.test(amount) || Number(amount) <= 0 || !validAddress(input.wallet, asset) || input.wallet.toLowerCase() !== authenticated.address.toLowerCase()) return send(res, 400, { error: "invalid funding request" }, origin);
    const recipients = fundingConfig();
    const destination = recipients[asset];
    if (!destination || !validAddress(destination, asset)) return send(res, 503, { error: "funding destination is not configured" }, origin);
    if (authenticated.chain === "solana" && asset !== "SOL") return send(res, 400, { error: "solana session can only fund SOL in this flow" }, origin);
    if (authenticated.chain !== "solana" && asset === "SOL") return send(res, 400, { error: "connect a Solana wallet to fund SOL" }, origin);
    if (asset === "BNB" && authenticated.chain !== "bsc") return send(res, 400, { error: "connect a BNB Chain wallet to fund BNB" }, origin);
    if (asset === "ETH" && authenticated.chain === "bsc") return send(res, 400, { error: "connect an Ethereum-compatible ETH chain wallet to fund ETH" }, origin);
    const amountBaseUnits = decimalToBaseUnits(amount, asset === "SOL" ? 9 : 18);
    if (!amountBaseUnits) return send(res, 400, { error: "amount has too many decimal places" }, origin);
    const intentId = randomUUID();
    const expiresAt = new Date(Date.now() + 5 * 60_000).toISOString();
    if (dbEnabled) await query("INSERT INTO funding_intents (id, wallet_identity_id, asset, chain, amount_base_units, destination, status, expires_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8)", [intentId, authenticated.walletIdentityId, asset, authenticated.chain, amountBaseUnits, destination, "awaiting-wallet-approval", expiresAt]);
    return send(res, 201, { intentId, state: "awaiting-wallet-approval", asset, amount, destination, expiresAt, feePolicy: feeConfig().fundingFeeBps ? `${feeConfig().fundingFeeBps} bps funding fee is accrued by the backend` : "none configured" }, origin);
  }
  return send(res, 404, { error: "not found" }, origin);
}

async function serveStatic(req, res, url) {
  const requested = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);
  const file = path.resolve(ROOT, `.${requested}`);
  if (!file.startsWith(ROOT) || !(await stat(file).catch(() => null))?.isFile()) return send(res, 404, { error: "not found" }, req.headers.origin);
  const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".png": "image/png", ".json": "application/json" };
  res.writeHead(200, { ...headers(req.headers.origin), "content-type": types[path.extname(file)] || "application/octet-stream" });
  res.end(await readFile(file));
}

const server = createServer(async (req, res) => {
  try {
    if (rateLimited(req)) return send(res, 429, { error: "rate limit exceeded" }, req.headers.origin);
    const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    if (req.method === "OPTIONS") { res.writeHead(204, headers(req.headers.origin)); return res.end(); }
    if (url.pathname.startsWith("/api/")) return await handleApi(req, res, url);
    if (req.method !== "GET" && req.method !== "HEAD") return send(res, 405, { error: "method not allowed" }, req.headers.origin);
    return await serveStatic(req, res, url);
  } catch (error) {
    return send(res, error.status || 500, { error: error.status ? error.message : "internal server error" }, req.headers.origin);
  }
});

try {
  await migrate();
  server.listen(PORT, HOST, () => console.log(`Coni backend listening on http://${HOST}:${PORT}`));
} catch (error) {
  console.error(`Coni backend startup failed: ${error.message}`);
  process.exitCode = 1;
}
