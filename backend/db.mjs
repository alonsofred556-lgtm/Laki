import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import pg from "pg";

const { Pool } = pg;
const production = process.env.NODE_ENV === "production";
const connectionString = process.env.DATABASE_URL || "";
export const dbEnabled = Boolean(connectionString);
if (production && !dbEnabled) throw new Error("DATABASE_URL is required in production");

const pool = dbEnabled ? new Pool({ connectionString, max: Number(process.env.DB_POOL_MAX || 10), idleTimeoutMillis: 30_000, connectionTimeoutMillis: 5_000, ssl: process.env.DATABASE_SSL === "disable" ? false : { rejectUnauthorized: false } }) : null;
const memory = new Map();

export async function migrate() {
  if (!pool) return { persistent: false };
  const file = path.join(path.dirname(fileURLToPath(import.meta.url)), "schema.sql");
  await pool.query(await readFile(file, "utf8"));
  return { persistent: true };
}

export async function query(text, values = []) {
  if (!pool) return { rows: [], rowCount: 0 };
  return pool.query(text, values);
}

export async function transaction(callback) {
  if (!pool) return callback({ query: async () => ({ rows: [], rowCount: 0 }) });
  const client = await pool.connect();
  try { await client.query("BEGIN"); const result = await callback(client); await client.query("COMMIT"); return result; }
  catch (error) { await client.query("ROLLBACK"); throw error; }
  finally { client.release(); }
}

export { memory };
