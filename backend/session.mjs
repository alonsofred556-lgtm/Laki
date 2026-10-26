import { createHmac, timingSafeEqual } from "node:crypto";

const secret = process.env.CONI_SESSION_SECRET || "";
if (process.env.NODE_ENV === "production" && secret.length < 32) throw new Error("CONI_SESSION_SECRET must be at least 32 characters in production");
const key = secret || "development-only-change-me";

function sign(value) { return createHmac("sha256", key).update(value).digest("base64url"); }

export function createSession(payload) {
  const body = Buffer.from(JSON.stringify({ ...payload, exp: Math.floor(Date.now() / 1000) + 60 * 60 * 12 })).toString("base64url");
  return `${body}.${sign(body)}`;
}

export function readSession(req) {
  const cookie = String(req.headers.cookie || "").split(";").map((item) => item.trim()).find((item) => item.startsWith("coni_session="));
  if (!cookie) return null;
  const token = decodeURIComponent(cookie.slice("coni_session=".length));
  const [body, signature] = token.split(".");
  if (!body || !signature) return null;
  const expected = sign(body);
  if (signature.length !== expected.length || !timingSafeEqual(Buffer.from(signature), Buffer.from(expected))) return null;
  try { const payload = JSON.parse(Buffer.from(body, "base64url").toString("utf8")); return payload.exp > Math.floor(Date.now() / 1000) ? payload : null; } catch { return null; }
}

export function sessionCookie(token) {
  const secure = process.env.NODE_ENV === "production" ? "; Secure" : "";
  return `coni_session=${encodeURIComponent(token)}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200${secure}`;
}
