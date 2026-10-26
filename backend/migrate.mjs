import { migrate } from "./db.mjs";

const result = await migrate();
console.log(result.persistent ? "Coni database migration complete" : "DATABASE_URL is not configured; no persistent migration was run");
