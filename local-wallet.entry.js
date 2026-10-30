import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import nacl from "tweetnacl";

const BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
const EVM_CHAINS = new Set(["evm", "ethereum", "base", "robinhood", "bsc"]);

function encodeBase58(bytes) {
  const source = Uint8Array.from(bytes);
  if (!source.length) return "";
  const digits = [0];
  for (const byte of source) {
    let carry = byte;
    for (let index = 0; index < digits.length; index += 1) {
      const value = digits[index] * 256 + carry;
      digits[index] = value % 58;
      carry = Math.floor(value / 58);
    }
    while (carry) {
      digits.push(carry % 58);
      carry = Math.floor(carry / 58);
    }
  }
  let leading = 0;
  while (leading < source.length && source[leading] === 0) leading += 1;
  const encoded = digits.reverse();
  if (encoded.length === 1 && encoded[0] === 0) encoded.pop();
  return "1".repeat(leading) + encoded.map((digit) => BASE58[digit]).join("");
}

function decodeBase58(value) {
  if (typeof value !== "string" || !value) throw new Error("Solana secret is empty");
  const bytes = [0];
  for (const character of value) {
    const index = BASE58.indexOf(character);
    if (index < 0) throw new Error("Solana secret is not valid base58");
    let carry = index;
    for (let offset = 0; offset < bytes.length; offset += 1) {
      const current = bytes[offset] * 58 + carry;
      bytes[offset] = current & 255;
      carry = current >> 8;
    }
    while (carry) {
      bytes.push(carry & 255);
      carry >>= 8;
    }
  }
  let leading = 0;
  while (leading < value.length && value[leading] === "1") leading += 1;
  const decoded = bytes.reverse();
  if (decoded.length && decoded[0] === 0) decoded.shift();
  return new Uint8Array([...new Array(leading).fill(0), ...decoded]);
}

function solanaKeypair(secret) {
  if (typeof secret === "string" && secret.trim().startsWith("[")) {
    const parsed = JSON.parse(secret);
    if (!Array.isArray(parsed) || parsed.length !== 64 || parsed.some((item) => !Number.isInteger(item) || item < 0 || item > 255)) {
      throw new Error("Solana secret array must contain 64 bytes");
    }
    return nacl.sign.keyPair.fromSecretKey(Uint8Array.from(parsed));
  }
  const decoded = decodeBase58(secret);
  if (decoded.length === 64) return nacl.sign.keyPair.fromSecretKey(decoded);
  if (decoded.length === 32) return nacl.sign.keyPair.fromSeed(decoded);
  throw new Error("Solana secret must decode to 32 or 64 bytes");
}

function create(chain) {
  if (EVM_CHAINS.has(chain)) {
    const privateKey = generatePrivateKey();
    return { chain, address: privateKeyToAccount(privateKey).address, secret: privateKey };
  }
  if (chain === "solana") {
    const keypair = nacl.sign.keyPair.fromSeed(crypto.getRandomValues(new Uint8Array(32)));
    return { chain, address: encodeBase58(keypair.publicKey), secret: encodeBase58(keypair.secretKey) };
  }
  throw new Error("unsupported local wallet chain");
}

function describe(chain, secret) {
  if (EVM_CHAINS.has(chain)) {
    if (!/^0x[a-fA-F0-9]{64}$/.test(String(secret || ""))) throw new Error("EVM private key is invalid");
    return { chain, address: privateKeyToAccount(secret).address, secret };
  }
  if (chain === "solana") {
    const keypair = solanaKeypair(String(secret || ""));
    return { chain, address: encodeBase58(keypair.publicKey), secret: encodeBase58(keypair.secretKey) };
  }
  throw new Error("unsupported local wallet chain");
}

async function signMessage(chain, secret, message) {
  if (EVM_CHAINS.has(chain)) {
    return privateKeyToAccount(secret).signMessage({ message });
  }
  if (chain === "solana") {
    const keypair = solanaKeypair(secret);
    const signed = nacl.sign.detached(new TextEncoder().encode(message), keypair.secretKey);
    return btoa(String.fromCharCode(...signed));
  }
  throw new Error("unsupported local wallet chain");
}

async function signEvmTransaction(chain, secret, transaction) {
  if (!EVM_CHAINS.has(chain)) throw new Error("unsupported EVM wallet chain");
  const account = privateKeyToAccount(secret);
  const bigintFields = ["nonce", "gas", "gasPrice", "value", "maxFeePerGas", "maxPriorityFeePerGas"];
  const normalized = { ...transaction, type: transaction.type || "legacy" };
  bigintFields.forEach((field) => {
    if (normalized[field] !== undefined && typeof normalized[field] !== "bigint") normalized[field] = BigInt(normalized[field]);
  });
  if (normalized.chainId !== undefined && typeof normalized.chainId !== "number") normalized.chainId = Number(normalized.chainId);
  return account.signTransaction(normalized);
}

window.ConiLocalWallet = Object.freeze({ create, describe, signMessage, signEvmTransaction });
