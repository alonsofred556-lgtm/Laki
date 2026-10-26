import { randomBytes, randomUUID } from "node:crypto";
import { verifyMessage } from "viem";
import nacl from "tweetnacl";
import { dbEnabled, memory, query } from "./db.mjs";

const evmChains = new Set(["ethereum", "base", "robinhood", "bsc"]);
const solanaChains = new Set(["solana"]);
const challengeTtlMs = 5 * 60_000;

function validEvm(value) { return typeof value === "string" && /^0x[a-fA-F0-9]{40}$/.test(value); }
function validSolana(value) { return typeof value === "string" && /^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(value); }
function base58(value) { const alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"; let bytes = [0]; for (const char of value) { const index = alphabet.indexOf(char); if (index < 0) throw new Error("invalid base58"); let carry = index; for (let i = 0; i < bytes.length; i++) { carry += bytes[i] * 58; bytes[i] = carry & 255; carry >>= 8; } while (carry) { bytes.push(carry & 255); carry >>= 8; } } for (const char of value) if (char === "1") bytes.push(0); return Uint8Array.from(bytes.reverse()); }

export function validWallet(chain, address) { return evmChains.has(chain) ? validEvm(address) : solanaChains.has(chain) ? validSolana(address) : false; }

export async function createChallenge({ chain, address, origin }) {
  if (!validWallet(chain, address)) throw Object.assign(new Error("invalid wallet identity"), { status: 400 });
  const id = randomUUID();
  const nonce = randomBytes(24).toString("hex");
  const domain = process.env.CONI_AUTH_DOMAIN || new URL(origin || "http://localhost").host;
  const issuedAt = new Date().toISOString();
  const expiresAt = new Date(Date.now() + challengeTtlMs).toISOString();
  const message = `coni wants you to sign in with your ${evmChains.has(chain) ? "Ethereum" : "Solana"} account:\n${address}\n\nSign in to Coni. This signature is free and cannot move funds.\n\nURI: ${process.env.CONI_AUTH_URI || origin || "http://localhost"}\nChain: ${chain}\nNonce: ${nonce}\nIssued At: ${issuedAt}\nExpiration Time: ${expiresAt}`;
  if (dbEnabled) await query("INSERT INTO wallet_challenges (id, chain, address, message, nonce, expires_at) VALUES ($1,$2,$3,$4,$5,$6)", [id, chain, address, message, nonce, expiresAt]);
  else memory.set(`challenge:${id}`, { id, chain, address, message, expiresAt, consumed: false });
  return { challengeId: id, chain, address, message, expiresAt };
}

export async function verifyChallenge({ challengeId, address, signature }) {
  const row = dbEnabled ? (await query("SELECT * FROM wallet_challenges WHERE id = $1", [challengeId])).rows[0] : memory.get(`challenge:${challengeId}`);
  if (!row || row.consumed || new Date(row.expires_at || row.expiresAt).getTime() <= Date.now() || row.address.toLowerCase() !== String(address).toLowerCase()) throw Object.assign(new Error("challenge expired or invalid"), { status: 401 });
  let valid = false;
  if (evmChains.has(row.chain)) valid = await verifyMessage({ address: row.address, message: row.message, signature });
  else { const publicKey = base58(row.address); const signed = Buffer.from(String(signature || ""), "base64"); valid = publicKey.length === 32 && signed.length === 64 && nacl.sign.detached.verify(new TextEncoder().encode(row.message), signed, publicKey); }
  if (!valid) throw Object.assign(new Error("wallet signature verification failed"), { status: 401 });
  if (dbEnabled) {
    await query("UPDATE wallet_challenges SET consumed_at = now() WHERE id = $1 AND consumed_at IS NULL", [challengeId]);
    const result = await query("INSERT INTO wallet_identities (id, chain, address) VALUES ($1,$2,$3) ON CONFLICT (chain,address) DO UPDATE SET last_authenticated_at = now() RETURNING id, chain, address", [randomUUID(), row.chain, row.address]);
    return result.rows[0];
  }
  row.consumed = true; const key = `identity:${row.chain}:${row.address.toLowerCase()}`; const identity = memory.get(key) || { id: randomUUID(), chain: row.chain, address: row.address }; memory.set(key, identity); return identity;
}
