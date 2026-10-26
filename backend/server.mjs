import { createServer } from "node:http";
import { randomUUID } from "node:crypto";
import { readFile, stat } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

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
    "access-control-allow-headers": "content-type",
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

function send(res, status, body, origin) {
  const payload = JSON.stringify(body);
  res.writeHead(status, { ...headers(origin), "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(payload) });
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
  if (["ETH", "BNB"].includes(asset)) return /^0x[a-fA-F0-9]{40}$/.test(value);
  if (asset === "SOL") return /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value);
  return false;
}

function fundingConfig() {
  try { return JSON.parse(process.env.CONI_FUNDING_RECIPIENTS || "{}"); } catch { return {}; }
}

function validSlug(value) { return typeof value === "string" && /^[a-z0-9][a-z0-9-]{1,80}$/i.test(value); }
function validChain(value) { return ["ethereum", "base", "robinhood", "bsc", "solana"].includes(value); }
function validAmount(value) { return typeof value === "string" && /^\d{1,78}$/.test(value) && BigInt(value) > 0n; }
function validSlippage(value) { return value === undefined || (Number.isInteger(value) && value >= 1 && value <= 9900); }

async function proxyJson(url, options = {}) {
  const upstream = await fetch(url, { ...options, headers: { accept: "application/json", "user-agent": "ConiMarketData/0.1", ...(options.headers || {}) }, signal: AbortSignal.timeout(10_000) });
  const data = await upstream.json().catch(() => ({ error: "invalid upstream response" }));
  return { status: upstream.status, data };
}

async function handleApi(req, res, url) {
  const origin = req.headers.origin;
  if (url.pathname === "/api/health" && req.method === "GET") return send(res, 200, { ok: true, service: "coni-backend", time: new Date().toISOString() }, origin);
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
  if (url.pathname === "/api/funding/intent" && req.method === "POST") {
    const input = await body(req);
    const asset = String(input.asset || "").toUpperCase();
    const amount = String(input.amount || "");
    if (!["ETH", "BNB", "SOL"].includes(asset) || !/^\d+(\.\d{1,18})?$/.test(amount) || Number(amount) <= 0 || !validAddress(input.wallet, asset)) return send(res, 400, { error: "invalid funding request" }, origin);
    const recipients = fundingConfig();
    const destination = recipients[asset];
    if (!destination || !validAddress(destination, asset)) return send(res, 503, { error: "funding destination is not configured" }, origin);
    return send(res, 201, { intentId: randomUUID(), state: "awaiting-wallet-approval", asset, amount, destination, expiresAt: new Date(Date.now() + 5 * 60_000).toISOString() }, origin);
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

server.listen(PORT, HOST, () => console.log(`Coni backend listening on http://${HOST}:${PORT}`));
