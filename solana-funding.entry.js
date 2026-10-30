import {
  Connection,
  Keypair,
  PublicKey,
  SystemProgram,
  Transaction,
} from "@solana/web3.js";
import { createNoopSigner } from "@solana/signers";
import {
  findAssociatedTokenPda,
  getCreateAssociatedTokenIdempotentInstruction,
  getTransferCheckedInstruction,
  TOKEN_PROGRAM_ADDRESS,
} from "@solana-program/token";

const SOLANA_TOKEN_PROGRAM = String(TOKEN_PROGRAM_ADDRESS);

const BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function decodeBase58(value) {
  const bytes = [0];
  for (const character of String(value || "")) {
    const index = BASE58.indexOf(character);
    if (index < 0) throw new Error("invalid local Solana secret");
    let carry = index;
    for (let offset = 0; offset < bytes.length; offset += 1) {
      const current = bytes[offset] * 58 + carry;
      bytes[offset] = current & 255;
      carry = current >> 8;
    }
    while (carry) { bytes.push(carry & 255); carry >>= 8; }
  }
  let leading = 0;
  while (leading < String(value || "").length && String(value)[leading] === "1") leading += 1;
  const decoded = bytes.reverse();
  if (decoded.length && decoded[0] === 0) decoded.shift();
  return Uint8Array.from([...new Array(leading).fill(0), ...decoded]);
}

function walletSigner(address) {
  return createNoopSigner(address);
}

function signatureFrom(result) {
  const signature = typeof result === "string" ? result : result?.signature;
  if (typeof signature !== "string" || !/^[1-9A-HJ-NP-Za-km-z]{40,100}$/.test(signature)) {
    throw new Error("The Solana wallet did not return a valid transaction signature");
  }
  return signature;
}

async function sendWithProvider(provider, transaction, connection, secret, from) {
  if (secret) {
    const decoded = decodeBase58(secret);
    if (decoded.length !== 64) throw new Error("local Solana secret must contain 64 bytes");
    const keypair = await Keypair.fromSecretKey(decoded);
    if (keypair.publicKey.toBase58() !== from) throw new Error("local Solana wallet does not match the authenticated address");
    await transaction.partialSign(keypair);
    return signatureFrom(await connection.sendRawTransaction(await transaction.serialize(), { preflightCommitment: "confirmed" }));
  }
  if (typeof provider?.signAndSendTransaction === "function") {
    return signatureFrom(await provider.signAndSendTransaction(transaction, { preflightCommitment: "confirmed" }));
  }
  if (typeof provider?.sendTransaction === "function") {
    return signatureFrom(await provider.sendTransaction(transaction, connection, { preflightCommitment: "confirmed" }));
  }
  if (typeof provider?.signTransaction === "function") {
    const signed = await provider.signTransaction(transaction);
    if (!signed?.serialize) throw new Error("The Solana wallet returned no serialized transaction");
    return signatureFrom(await connection.sendRawTransaction(await signed.serialize(), { preflightCommitment: "confirmed" }));
  }
  throw new Error("This Solana wallet does not support transaction approval");
}

async function preparedTransaction({ rpcUrl, from, destination, amountBaseUnits, asset, tokenAddress, tokenProgram, decimals }) {
  if (!rpcUrl || !from || !destination) throw new Error("Solana funding transaction details are incomplete");
  const connection = new Connection(rpcUrl, "confirmed");
  const source = new PublicKey(from);
  const recipient = new PublicKey(destination);
  const latest = await connection.getLatestBlockhash("confirmed");
  const transaction = new Transaction({
    feePayer: source,
    blockhash: latest.blockhash,
    lastValidBlockHeight: latest.lastValidBlockHeight,
  });

  if (asset === "SOL") {
    transaction.add(SystemProgram.transfer({
      fromPubkey: source,
      toPubkey: recipient,
      lamports: BigInt(amountBaseUnits),
    }));
    return { connection, transaction, eventIndex: 0 };
  }

  if (String(tokenProgram || SOLANA_TOKEN_PROGRAM) !== SOLANA_TOKEN_PROGRAM) {
    throw new Error("This Solana token program is not enabled for browser funding yet");
  }
  if (!tokenAddress || !Number.isInteger(Number(decimals)) || Number(decimals) < 0 || Number(decimals) > 36) {
    throw new Error("Verified SPL mint metadata is required before funding this token");
  }
  const mint = new PublicKey(tokenAddress);
  const signer = walletSigner(from);
  const [sourceAta] = await findAssociatedTokenPda({ owner: from, tokenProgram: SOLANA_TOKEN_PROGRAM, mint: tokenAddress });
  const [destinationAta] = await findAssociatedTokenPda({ owner: destination, tokenProgram: SOLANA_TOKEN_PROGRAM, mint: tokenAddress });
  if (!await connection.getAccountInfo(new PublicKey(sourceAta))) {
    throw new Error("The connected wallet has no associated token account for this mint");
  }
  let eventIndex = 0;
  if (!await connection.getAccountInfo(new PublicKey(destinationAta))) {
    transaction.add(getCreateAssociatedTokenIdempotentInstruction({
      payer: signer,
      ata: destinationAta,
      owner: destination,
      mint: tokenAddress,
      tokenProgram: SOLANA_TOKEN_PROGRAM,
    }));
    eventIndex = 1;
  }
  transaction.add(getTransferCheckedInstruction({
    source: sourceAta,
    mint: tokenAddress,
    destination: destinationAta,
    authority: signer,
    amount: BigInt(amountBaseUnits),
    decimals: Number(decimals),
  }));
  return { connection, transaction, eventIndex };
}

async function sendFunding({ provider, secret, rpcUrl, from, destination, amountBaseUnits, asset, tokenAddress, tokenProgram, decimals }) {
  const prepared = await preparedTransaction({ rpcUrl, from, destination, amountBaseUnits, asset, tokenAddress, tokenProgram, decimals });
  const simulation = await prepared.connection.simulateTransaction(prepared.transaction);
  if (simulation.value?.err) throw new Error("Solana rejected the funding transaction during simulation");
  const signature = await sendWithProvider(provider, prepared.transaction, prepared.connection, secret, from);
  return { signature, eventIndex: prepared.eventIndex };
}

window.ConiSolana = Object.freeze({ sendFunding });
