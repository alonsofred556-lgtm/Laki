(() => {
  const config = window.CONI_CONFIG;
  const state = { evm: null, solana: null };
  let activeIntent = null;
  let fundingPollTimer = null;
  let activeBotWallet = null;
  let botWalletSnapshot = null;
  let botFundingMode = false;
  let preferredBotMode = localStorage.getItem("coni.bot-wallet.mode.v1") || "external-bot";
  const STORAGE_KEY = "coni.wallet.connections.v1";
  const EVM_PROVIDER_KEY = "coni.wallet.evm-provider.v1";
  const SOLANA_PROVIDER_KEY = "coni.wallet.solana-provider.v1";
  const announcedEvmProviders = new Map();
  const $ = (selector) => document.querySelector(selector);
  const short = (address) => address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "—";
  const chainNames = { 1: "Ethereum", 8453: "Base", 4663: "Robinhood Chain", 56: "BNB Chain" };

  // EIP-6963 allows multiple injected wallets to coexist without silently
  // selecting whichever provider happened to win window.ethereum.
  window.addEventListener("eip6963:announceProvider", (event) => {
    const info = event.detail?.info;
    const provider = event.detail?.provider;
    if (!provider || !info?.uuid) return;
    announcedEvmProviders.set(info.uuid, { info, provider });
    renderProviderChoices();
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));

  function message(text, tone = "") {
    const node = $("#wallet-message");
    node.textContent = text;
    node.dataset.tone = tone;
  }

  function legacyEvmProviders() {
    const providers = Array.isArray(window.ethereum?.providers) ? window.ethereum.providers : [window.ethereum].filter(Boolean);
    return [
      ["OKX Wallet", window.okxwallet?.ethereum],
      ["SafePal", window.safepalProvider],
      ["Phantom", window.phantom?.ethereum],
      ...providers.map((provider) => [providerLabel(provider), provider]),
    ].filter(([, provider], index, items) => provider && items.findIndex(([, candidate]) => candidate === provider) === index)
      .map(([name, provider], index) => ({ key: `legacy-${index}`, name, provider }));
  }

  function evmProviderOptions() {
    const announced = [...announcedEvmProviders.entries()].map(([key, value]) => ({ key, name: value.info.name || "EVM wallet", provider: value.provider }));
    const options = [...announced, ...legacyEvmProviders()];
    return options.filter((item, index) => options.findIndex((candidate) => candidate.provider === item.provider) === index);
  }

  function solanaProviderOptions() {
    const candidates = [
      ["Phantom", window.phantom?.solana],
      ["OKX Wallet", window.okxwallet?.solana],
      ["SafePal", window.safepalwallet?.solana],
      ["Solflare", window.solflare],
      ["Backpack", window.backpack?.solana],
      ["Solana wallet", window.solana],
    ];
    return candidates.filter(([, provider], index, items) => provider && items.findIndex(([, candidate]) => candidate === provider) === index)
      .map(([name, provider], index) => ({ key: `solana-${index}`, name, provider }));
  }

  function setProviderOptions(selector, options, storageKey, label) {
    const select = $(selector);
    if (!select) return;
    const current = localStorage.getItem(storageKey) || "";
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = `Choose ${label}`;
    select.appendChild(placeholder);
    options.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.key;
      option.textContent = item.name;
      select.appendChild(option);
    });
    select.value = options.some((item) => item.key === current) ? current : (options.length === 1 ? options[0].key : "");
    select.hidden = options.length < 2;
  }

  function renderProviderChoices() {
    setProviderOptions("#evm-wallet-choice", evmProviderOptions(), EVM_PROVIDER_KEY, "EVM wallet");
    setProviderOptions("#solana-wallet-choice", solanaProviderOptions(), SOLANA_PROVIDER_KEY, "Solana wallet");
  }

  function providerForEvm() {
    const options = evmProviderOptions();
    const selected = localStorage.getItem(EVM_PROVIDER_KEY);
    return options.find((item) => item.key === selected)?.provider || options[0]?.provider || null;
  }

  function providerForSolana() {
    const options = solanaProviderOptions();
    const selected = localStorage.getItem(SOLANA_PROVIDER_KEY);
    return options.find((item) => item.key === selected)?.provider || options[0]?.provider || null;
  }

  function providerLabel(provider, isSolana = false) {
    if (!provider) return isSolana ? "Solana wallet" : "EVM wallet";
    if (provider.isOkxWallet || provider === window.okxwallet?.ethereum || provider === window.okxwallet?.solana) return "OKX Wallet";
    if (provider.isSafePal || provider === window.safepalProvider || provider === window.safepalwallet?.solana) return "SafePal";
    if (provider.isPhantom || provider === window.phantom?.ethereum || provider === window.phantom?.solana) return "Phantom";
    if (provider.isMetaMask) return "MetaMask";
    if (provider.isRabby) return "Rabby";
    return isSolana ? "Solana wallet" : "EVM wallet";
  }

  function saveConnections() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ evm: state.evm && { address: state.evm.address, chainId: state.evm.chainId, chainName: state.evm.chainName, providerName: state.evm.providerName }, solana: state.solana && { address: state.solana.address, providerName: state.solana.providerName } })); } catch { /* storage is optional */ }
  }

  function restoreConnectionLabels() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      if (saved?.evm?.address) state.evm = { ...saved.evm, balance: "Reconnect to refresh", restored: true };
      if (saved?.solana?.address) state.solana = { ...saved.solana, balance: "Reconnect to refresh", restored: true };
    } catch { /* ignore invalid local state */ }
  }

  async function authenticateWallet(chain, address, sign) {
    const challengeResponse = await fetch(`${config.apiBaseUrl}/api/auth/challenge`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ chain, address, origin: window.location.origin }) });
    const challenge = await challengeResponse.json();
    if (!challengeResponse.ok) throw new Error(challenge.error || "Could not create wallet challenge");
    message("Approve the free sign-in message. It cannot move funds…");
    const signature = await sign(challenge.message);
    const verifyResponse = await fetch(`${config.apiBaseUrl}/api/auth/verify`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ challengeId: challenge.challengeId, address, signature }) });
    const verified = await verifyResponse.json();
    if (!verifyResponse.ok || !verified.authenticated) throw new Error(verified.error || "Wallet ownership could not be verified");
    return verified.wallet;
  }

  async function loadTokenLogo() {
    try {
      const response = await fetch(`${config.tokenMetadataUrl}?launch=${encodeURIComponent(config.token.slug)}`, { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const meta = await response.json();
      const image = typeof meta.image === "string" && /^https?:\/\//i.test(meta.image)
        ? meta.image
        : meta.image?.match?.(/^ipfs:\/\/([^/]+)/i)?.[1] && `https://www.pangea.trade/api/logo/${encodeURIComponent(meta.image.match(/^ipfs:\/\/([^/]+)/i)[1])}/128.webp`;
      if (!image) return;
      const logo = $("#zgap-logo");
      logo.src = image;
      logo.hidden = false;
      $("#zgap-logo-fallback").hidden = true;
    } catch { /* the monogram remains a safe local fallback */ }
  }

  async function readEvmBalance(provider, address) {
    const raw = await provider.request({ method: "eth_getBalance", params: [address, "latest"] });
    return `${(Number(BigInt(raw)) / 1e18).toFixed(5)} native`;
  }

  async function connectEvm() {
    const provider = providerForEvm();
    if (!provider) {
      message("No EVM wallet detected. Install MetaMask, Rabby, Coinbase Wallet, OKX, or Phantom EVM, then try again.", "error");
      return;
    }
    try {
      message("Waiting for wallet approval…");
      const accounts = await provider.request({ method: "eth_requestAccounts" });
      const chainIdHex = await provider.request({ method: "eth_chainId" });
      const chainId = Number.parseInt(chainIdHex, 16);
      const address = accounts[0];
      const supportedChainIds = Object.values(config.wallets.evmChains);
      if (!supportedChainIds.includes(chainId)) {
        state.evm = null;
        renderConnections();
        message("Wallet connected on an unsupported network. Switch to Ethereum, Base, Robinhood Chain, or BNB Chain.", "error");
        return;
      }
      const balance = await readEvmBalance(provider, address);
      const chain = Object.entries(config.wallets.evmChains).find(([, id]) => id === chainId)?.[0];
      if (!chain) throw new Error("Unsupported EVM chain");
      await authenticateWallet(chain, address, (messageText) => provider.request({ method: "personal_sign", params: [messageText, address] }));
      preferredBotMode = "external-bot";
      localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      state.evm = { provider, address, chain, chainId, chainName: chainNames[chainId] || `Chain ${chainId}`, balance, providerName: providerLabel(provider), authenticated: true };
      saveConnections();
      renderConnections();
      message(`${state.evm.chainName} wallet connected.`, "success");
      loadBotWallet();
      provider.on?.("accountsChanged", () => connectEvm());
      provider.on?.("chainChanged", () => connectEvm());
    } catch (error) {
      message(error?.message || "The EVM wallet connection was cancelled.", "error");
    }
  }

  async function connectSolana() {
    const provider = providerForSolana();
    if (!provider?.connect) {
      message("No Solana wallet detected. Install Phantom, Solflare, or Backpack, then try again.", "error");
      return;
    }
    try {
      message("Waiting for Solana wallet approval…");
      const response = await provider.connect();
      const address = response.publicKey?.toString?.() || provider.publicKey?.toString?.();
      let balance = "Connected";
      try {
        const rpc = await fetch("https://api.mainnet-beta.solana.com", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getBalance", params: [address] }) });
        const json = await rpc.json();
        balance = `${((json.result?.value || 0) / 1e9).toFixed(5)} SOL`;
      } catch { /* balance stays connected when the public RPC is unavailable */ }
      if (!provider.signMessage) throw new Error("This Solana wallet does not expose off-chain message signing");
      await authenticateWallet("solana", address, async (messageText) => {
        const signed = await provider.signMessage(new TextEncoder().encode(messageText), "utf8");
        const bytes = signed.signature || signed;
        return btoa(String.fromCharCode(...bytes));
      });
      preferredBotMode = "external-bot";
      localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      state.solana = { provider, address, chain: "solana", balance, providerName: providerLabel(provider, true), authenticated: true };
      saveConnections();
      renderConnections();
      message("Solana wallet connected.", "success");
      loadBotWallet();
      provider.on?.("accountChanged", (publicKey) => { if (publicKey) { state.solana.address = publicKey.toString(); renderConnections(); } });
    } catch (error) {
      message(error?.message || "The Solana wallet connection was cancelled.", "error");
    }
  }

  function renderConnections() {
    const connections = [state.evm, state.solana].filter(Boolean);
    $("#connection-count").textContent = `${connections.length} connected`;
    if (!connections.length) {
      $("#connection-list").innerHTML = `<div class="wallet-empty"><span class="empty-orb">◉</span><strong>No wallet connected</strong><p>Choose an EVM wallet or a Solana wallet to begin reading your balances.</p></div>`;
      return;
    }
    $("#connection-list").innerHTML = connections.map((item) => {
      const isSolana = item === state.solana;
      return `<div class="connection-row"><span class="provider-icon ${isSolana ? "sol" : "eth"}">${isSolana ? "S" : "E"}</span><div><strong>${item.providerName || (isSolana ? "Solana wallet" : "EVM wallet")}</strong><small>${short(item.address)} · ${isSolana ? "Solana" : item.chainName}</small></div><span class="connection-balance">${item.balance}</span><span class="connected-dot">●</span></div>`;
    }).join("");
    if (state.evm) $("#eth-balance").textContent = state.evm.balance;
    if (state.solana) $("#sol-balance").textContent = state.solana.balance;
  }

  async function prepareFunding() {
    const asset = $("#funding-asset").value;
    const amount = $("#funding-amount").value.trim();
    const requestedChain = botFundingMode ? activeBotWallet?.chain : (state.solana?.authenticated ? "solana" : state.evm?.chain);
    const fundingAddress = requestedChain === "solana" ? state.solana?.address : state.evm?.address;
    if (!requestedChain || !fundingAddress || (requestedChain === "solana" ? state.solana?.restored : state.evm?.restored)) { $("#funding-status").textContent = "Connect the wallet for the selected bot network before preparing funding."; return; }
    if (!/^\d+(\.\d{1,18})?$/.test(amount) || Number(amount) <= 0) { $("#funding-status").textContent = "Enter a positive amount with no more than 18 decimal places."; return; }
    $("#funding-status").textContent = "Asking the secure backend for a whitelisted funding intent…";
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/intent`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ asset, amount, chain: requestedChain, wallet: fundingAddress, botWalletId: botFundingMode ? activeBotWallet?.id : null }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Funding intent rejected");
      activeIntent = result;
      $("#funding-status").textContent = `${result.botWalletId ? "Bot top-up: " : ""}Send exactly ${result.amount} ${result.asset} to ${short(result.destination)}. Review the full destination in your wallet before approving.`;
      $("#approve-funding").hidden = !(result.chain !== "solana" && state.evm?.provider && !state.evm?.restored);
      $("#funding-hash-row").hidden = false;
      $("#claim-funding").hidden = false;
    } catch {
      $("#funding-status").textContent = "No funding intent was created. The secure backend destination is not configured in this build, so no funds can move.";
    }
  }

  function toBaseUnits(value, decimals) {
    const [whole, fraction = ""] = value.split(".");
    if (fraction.length > decimals) throw new Error("Amount has too many decimal places");
    return BigInt(`${whole}${fraction.padEnd(decimals, "0")}`);
  }

  async function claimFunding(txHash) {
    if (!activeIntent || !txHash) throw new Error("Create a funding intent and provide a transaction hash first");
    const response = await fetch(`${config.apiBaseUrl}/api/funding/claim`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ intentId: activeIntent.intentId, txHash }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Funding claim rejected");
    $("#funding-status").textContent = "Transfer submitted for server verification. The ledger is credited only after finality is confirmed.";
    $("#approve-funding").hidden = true;
    watchFundingStatus();
    return result;
  }

  async function watchFundingStatus() {
    if (!activeIntent) return;
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/status/${encodeURIComponent(activeIntent.intentId)}`, { credentials: "include", cache: "no-store" });
      if (response.ok) {
        const result = await response.json();
        const latest = result.observations?.[0];
        if (result.state === "credited") {
          $("#funding-status").textContent = "Funding confirmed and credited to the ledger.";
          return;
        }
        if (latest?.state === "rejected") {
          $("#funding-status").textContent = `Funding was rejected: ${latest.rejectionCode || "transaction did not match the intent"}.`;
          return;
        }
        if (latest?.state === "pending-verification" || latest?.state === "verifying") {
          $("#funding-status").textContent = `Verifying transfer${latest.confirmations ? ` · ${latest.confirmations} confirmations` : ""}…`;
        }
      }
    } catch { /* polling is best effort; the status can be refreshed later */ }
    clearTimeout(fundingPollTimer);
    fundingPollTimer = setTimeout(watchFundingStatus, 5000);
  }

  async function approveFunding() {
    if (!activeIntent || !state.evm?.provider || state.evm.restored) throw new Error("Reconnect the EVM wallet before approving");
    const expectedChainId = config.wallets.evmChains[activeIntent.chain];
    const actualChainId = Number.parseInt(await state.evm.provider.request({ method: "eth_chainId" }), 16);
    if (expectedChainId && actualChainId !== expectedChainId) throw new Error(`Switch the connected EVM wallet to ${chainNames[expectedChainId] || activeIntent.chain} before approving`);
    const value = `0x${toBaseUnits(activeIntent.amount, activeIntent.asset === "BNB" ? 18 : 18).toString(16)}`;
    $("#funding-status").textContent = "Reviewing the exact destination and amount in your wallet…";
    const txHash = await state.evm.provider.request({ method: "eth_sendTransaction", params: [{ from: state.evm.address, to: activeIntent.destination, value }] });
    $("#funding-tx-hash").value = txHash;
    await claimFunding(txHash);
  }

  async function submitFundingClaim() {
    try {
      await claimFunding($("#funding-tx-hash").value.trim());
    } catch (error) {
      $("#funding-status").textContent = error?.message || "Could not submit funding for verification.";
    }
  }

  function esc(value) {
    return String(value ?? "—").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  }

  function setNativeAssetOptions(chain) {
    const asset = chain === "solana" ? "SOL" : chain === "bsc" ? "BNB" : "ETH";
    [$("#funding-asset"), $("#bot-withdraw-asset")].forEach((select) => {
      if (!select) return;
      select.innerHTML = `<option value="${asset}">${asset}</option>`;
      select.value = asset;
    });
  }

  function botWalletChoices() {
    const wallets = botWalletSnapshot?.wallets || [];
    const preferred = wallets.filter((item) => item.mode === preferredBotMode);
    return preferred.length ? preferred : wallets.filter((item) => item.mode === "external-bot");
  }

  function renderBotWalletChoices() {
    const select = $("#bot-wallet-chain");
    if (!select) return;
    const choices = botWalletChoices();
    select.replaceChildren();
    choices.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = `${chainNames[config.wallets.evmChains[item.chain]] || (item.chain === "solana" ? "Solana" : item.chain)} · ${item.status === "active" ? "active" : "awaiting provision"}`;
      select.appendChild(option);
    });
    select.hidden = choices.length < 2;
    if (activeBotWallet && choices.some((item) => item.id === activeBotWallet.id)) select.value = activeBotWallet.id;
  }

  function renderBotWalletSummary() {
    const wallet = activeBotWallet;
    if (!wallet) return;
    setNativeAssetOptions(wallet.chain);
    $("#internal-wallet-toggle").hidden = wallet.mode !== "internal-bot";
    $("#bot-wallet-state").textContent = wallet.status === "active" ? "ACTIVE" : "GATED";
    const balances = (wallet.balances || []).map((item) => `<span class="network-pill">${esc(item.asset)} ${esc(item.amountBaseUnits)}</span>`).join(" ") || "No verified balance";
    $("#bot-wallet-content").innerHTML = `<div class="vault-ring large"><span>⌁</span></div><div><strong>${wallet.status === "active" ? "Bot wallet ready" : "Bot wallet record attached"}</strong><p>${wallet.address ? `${esc(short(wallet.address))} · ${esc(wallet.chain)} · ${balances}` : "Awaiting the audited signer/MPC provisioner. No address or private key has been fabricated."}</p></div>`;
    const tokens = (botWalletSnapshot?.tradedTokens || []).map((item) => `<span class="network-pill">${esc(item.slug)}</span>`).join(" ") || "No completed token trades";
    $("#bot-wallet-history").innerHTML = `<div class="bot-pnl-strip"><strong>PNL</strong><b>$${esc(botWalletSnapshot?.pnl?.realizedUsd || "0")}</b><small>${esc(botWalletSnapshot?.pnl?.settledTrades || 0)} settled trades · ${esc(botWalletSnapshot?.tradedTokens?.length || 0)} Pangea tokens tracked</small><div class="bot-token-list">${tokens}</div></div>` + (botWalletSnapshot?.history || []).slice(0, 8).map((item) => `<div class="activity-item"><span class="activity-kind ${item.kind === "trade" ? "trade" : "bridge"}">${esc(String(item.kind).toUpperCase())}</span><div><strong>${esc(item.slug || item.asset || "Bot wallet")}</strong><small>${esc(item.state)} · ${esc(item.createdAt)}</small></div><b>${item.realizedProfitUsd ? `$${esc(item.realizedProfitUsd)}` : "—"}</b></div>`).join("");
  }

  function selectBotWallet(walletId) {
    const wallet = botWalletChoices().find((item) => item.id === walletId);
    if (!wallet) return;
    activeBotWallet = wallet;
    renderBotWalletChoices();
    renderBotWalletSummary();
    loadBotWallet(wallet.id);
  }

  async function loadBotWallet(walletId = null) {
    try {
      const query = walletId ? `?walletId=${encodeURIComponent(walletId)}` : "";
      const response = await fetch(`${config.apiBaseUrl}/api/bot-wallet${query}`, { credentials: "include", cache: "no-store" });
      if (response.status === 401) {
        botWalletSnapshot = null;
        activeBotWallet = null;
        $("#bot-wallet-state").textContent = "CONNECT FIRST";
        $("#bot-wallet-content").querySelector("p").textContent = "Connect and sign in with a wallet. Coni will then create this user's separate bot-wallet record.";
        return;
      }
      const result = await response.json();
      botWalletSnapshot = result;
      const currentChain = state.solana?.authenticated ? "solana" : state.evm?.chain || result.currentChain;
      const choices = botWalletChoices();
      activeBotWallet = choices.find((item) => walletId && item.id === walletId) || choices.find((item) => item.chain === currentChain) || result.currentWallet || choices[0] || null;
      renderBotWalletChoices();
      renderBotWalletSummary();
      if (!activeBotWallet) return;
      loadUnclaimedDeposits();
    } catch {
      $("#bot-wallet-state").textContent = "UNAVAILABLE";
    }
  }

  async function loadUnclaimedDeposits() {
    const panel = $("#unclaimed-deposits");
    if (!panel) return;
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/unclaimed`, { credentials: "include", cache: "no-store" });
      if (!response.ok) { panel.innerHTML = ""; return; }
      const result = await response.json();
      const deposits = Array.isArray(result.deposits) ? result.deposits : [];
      panel.innerHTML = deposits.length
        ? `<div class="bot-pnl-strip"><strong>UNCLAIMED DISCOVERIES</strong><small>These transfers reached your active bot wallet. Claiming only creates a verification intent; it does not credit funds immediately.</small></div>` + deposits.map((item) => `<div class="activity-item"><span class="activity-kind bridge">${esc(item.asset)}</span><div><strong>${esc(item.chain)} · ${esc(item.amountBaseUnits)} base units</strong><small>${esc(short(item.txHash))} · ${esc(item.observedAt)}</small></div><button class="secondary-button" data-claim-observation="${esc(item.id)}">Claim for verification</button></div>`).join("")
        : "";
    } catch {
      panel.innerHTML = "";
    }
  }

  async function claimUnclaimedDeposit(observationId) {
    const response = await fetch(`${config.apiBaseUrl}/api/funding/claim-unclaimed`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ observationId }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Could not claim the discovered deposit");
    $("#funding-status").textContent = "Discovered deposit claimed for exact transaction and finality verification.";
    loadUnclaimedDeposits();
    loadBotWallet();
  }

  async function encryptLocalSecret(secret, password, chain) {
    const encoder = new TextEncoder();
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const material = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey({ name: "PBKDF2", salt, iterations: 250000, hash: "SHA-256" }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, encoder.encode(secret));
    const encode = (bytes) => btoa(String.fromCharCode(...bytes));
    return { version: 1, chain, algorithm: "PBKDF2-AES-GCM", salt: encode(salt), iv: encode(iv), ciphertext: encode(new Uint8Array(ciphertext)), createdAt: new Date().toISOString() };
  }

  function validLocalSecret(chain, secret) {
    if (chain === "evm") return /^0x[a-fA-F0-9]{64}$/.test(secret);
    if (/^[1-9A-HJ-NP-Za-km-z]{64,128}$/.test(secret)) return true;
    try {
      const parsed = JSON.parse(secret);
      return Array.isArray(parsed) && parsed.length === 64 && parsed.every((item) => Number.isInteger(item) && item >= 0 && item <= 255);
    } catch { return false; }
  }

  async function importLocalWallet() {
    const chain = $("#existing-wallet-chain").value;
    const privateKey = $("#existing-private-key").value.trim();
    const password = $("#existing-wallet-password").value;
    if (!validLocalSecret(chain, privateKey) || password.length < 12) {
      $("#vault-action-status").textContent = chain === "solana" ? "Enter a valid Solana base58 secret or 64-byte JSON array and a password of at least 12 characters. The secret remains in this browser and is never posted." : "Enter a valid EVM private key and a password of at least 12 characters. The key remains in this browser and is never posted.";
      return;
    }
    const vault = await encryptLocalSecret(privateKey, password, chain);
    localStorage.setItem("coni.internal.vault.v1", JSON.stringify(vault));
    $("#existing-private-key").value = "";
    $("#existing-wallet-password").value = "";
    $("#vault-action-status").textContent = `Encrypted ${chain === "solana" ? "Solana" : "EVM"} local backup created. It is not a live bot signer until an audited wallet SDK and signer policy are connected.`;
  }

  async function createInternalWallet() {
    const password = $("#new-wallet-password").value;
    const confirm = $("#new-wallet-password-confirm").value;
    if (password.length < 12 || password !== confirm) {
      $("#vault-action-status").textContent = "Use a password of at least 12 characters and confirm it exactly.";
      return;
    }
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/bot-wallet/provision`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ mode: "internal-bot" }) });
      const result = await response.json();
      preferredBotMode = "internal-bot";
      localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      $("#vault-action-status").textContent = result.message || "Internal wallet setup is awaiting the audited wallet provisioner.";
      loadBotWallet();
    } catch {
      $("#vault-action-status").textContent = "Connect and authenticate a wallet before creating the internal-wallet record.";
    }
  }

  function startBotTopup() {
    if (!activeBotWallet) { $("#funding-status").textContent = "Connect a wallet first; the per-user bot wallet record will be attached automatically."; return; }
    botFundingMode = true;
    $("#funding-status").textContent = activeBotWallet.status === "active" ? "Bot top-up mode enabled. Choose the asset and amount, then prepare the verified transfer." : "This bot wallet is awaiting audited signer provisioning, so top-up is not enabled yet.";
    document.querySelector("#funding").scrollIntoView({ behavior: "smooth", block: "center" });
  }

  async function requestBotWithdrawal() {
    if (!activeBotWallet || activeBotWallet.status !== "active") { $("#funding-status").textContent = "Withdrawal is unavailable until this user's bot wallet has an audited signer address."; return; }
    $("#bot-withdraw-form").hidden = !$("#bot-withdraw-form").hidden;
  }

  async function submitBotWithdrawal() {
    if (!activeBotWallet || activeBotWallet.status !== "active") throw new Error("bot wallet is not active");
    const asset = $("#bot-withdraw-asset").value;
    const amount = $("#bot-withdraw-amount").value.trim();
    const decimals = asset === "SOL" ? 9 : 18;
    if (!/^\d+(\.\d+)?$/.test(amount)) throw new Error("enter a valid withdrawal amount");
    const amountBaseUnits = toBaseUnits(amount, decimals).toString();
    const destination = activeBotWallet.chain === "solana" ? state.solana?.address : state.evm?.address;
    if (!destination) throw new Error("Connect the wallet that owns this bot network before requesting withdrawal");
    const response = await fetch(`${config.apiBaseUrl}/api/bot-wallet/transfer`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ botWalletId: activeBotWallet.id, direction: "withdrawal", asset, amountBaseUnits, destination, idempotencyKey: `bot-withdrawal-${crypto.randomUUID()}` }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.message || "withdrawal request rejected");
    $("#funding-status").textContent = `Bot withdrawal reserved for signer approval: ${result.state}. No transaction has been broadcast.`;
    $("#bot-withdraw-form").hidden = true;
    loadBotWallet();
  }

  $("#connect-evm").addEventListener("click", connectEvm);
  $("#connect-solana").addEventListener("click", connectSolana);
  $("#evm-wallet-choice")?.addEventListener("change", (event) => {
    if (event.target.value) localStorage.setItem(EVM_PROVIDER_KEY, event.target.value);
  });
  $("#solana-wallet-choice")?.addEventListener("change", (event) => {
    if (event.target.value) localStorage.setItem(SOLANA_PROVIDER_KEY, event.target.value);
  });
  $("#bot-wallet-chain")?.addEventListener("change", (event) => selectBotWallet(event.target.value));
  $("#prepare-funding").addEventListener("click", prepareFunding);
  $("#approve-funding").addEventListener("click", () => approveFunding().catch((error) => { $("#funding-status").textContent = error?.message || "Wallet approval failed."; }));
  $("#claim-funding").addEventListener("click", submitFundingClaim);
  $("#vault-setup").addEventListener("click", () => { $("#vault-modal").hidden = false; document.body.style.overflow = "hidden"; });
  $("#bot-topup").addEventListener("click", startBotTopup);
  $("#bot-withdraw").addEventListener("click", requestBotWithdrawal);
  $("#submit-bot-withdrawal").addEventListener("click", () => submitBotWithdrawal().catch((error) => { $("#funding-status").textContent = error?.message || "Bot withdrawal could not be requested."; }));
  $("#import-local-wallet").addEventListener("click", () => importLocalWallet().catch(() => { $("#vault-action-status").textContent = "Local encryption failed; no key was uploaded."; }));
  $("#create-internal-wallet").addEventListener("click", createInternalWallet);
  document.addEventListener("click", (event) => {
    const claim = event.target.closest("[data-claim-observation]");
    if (claim) {
      claim.disabled = true;
      claim.textContent = "Claiming…";
      claimUnclaimedDeposit(claim.dataset.claimObservation).catch((error) => {
        claim.disabled = false;
        claim.textContent = "Claim for verification";
        $("#funding-status").textContent = error?.message || "Could not claim the discovered deposit.";
      });
    }
  });
  document.querySelectorAll("[data-surface]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-surface]").forEach((item) => item.classList.toggle("active", item === button));
    document.body.dataset.walletSurface = button.dataset.surface;
    $("#bot-wallet-state").textContent = button.dataset.surface === "bot" ? (activeBotWallet?.status === "active" ? "ACTIVE" : "GATED") : "NORMAL VIEW";
  }));
  document.querySelectorAll("[data-wallet-mode]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-wallet-mode]").forEach((item) => item.classList.toggle("active", item === button));
    $("#existing-wallet-panel").hidden = button.dataset.walletMode !== "existing";
    $("#new-wallet-panel").hidden = button.dataset.walletMode !== "new";
  }));
  document.addEventListener("click", (event) => { if (event.target.matches("[data-close-modal]") || event.target.classList.contains("modal-backdrop")) { event.target.closest(".modal-backdrop").hidden = true; document.body.style.overflow = ""; } });
  renderProviderChoices();
  restoreConnectionLabels();
  renderConnections();
  loadTokenLogo();
  loadBotWallet();
  window.setInterval(() => {
    if (state.evm?.authenticated || state.solana?.authenticated) loadBotWallet();
  }, 15000);
  const requestedConnect = new URLSearchParams(window.location.search).get("connect");
  if (requestedConnect === "evm" || requestedConnect === "solana") {
    window.setTimeout(() => (requestedConnect === "evm" ? connectEvm() : connectSolana()), 0);
  }
})();
