(() => {
  const config = window.CONI_CONFIG;
  const state = { evm: null, solana: null };
  let activeIntent = null;
  let fundingPollTimer = null;
  let activeBotWallet = null;
  let botWalletSnapshot = null;
  let botFundingMode = false;
  let pendingRecoverySecret = null;
  let fastWalletSession = null;
  let fastWalletLockTimer = null;
  let localWalletModulePromise = null;
  let solanaFundingModulePromise = null;
  let preferredBotMode = localStorage.getItem("coni.bot-wallet.mode.v1") || "external-bot";
  const STORAGE_KEY = "coni.wallet.connections.v1";
  const FAST_WALLET_STORAGE_KEY = "coni.fast-wallet.v2";
  const LEGACY_LOCAL_VAULT_KEY = "coni.internal.vault.v1";
  const FAST_WALLET_IDLE_MS = 15 * 60 * 1000;
  const EVM_PROVIDER_KEY = "coni.wallet.evm-provider.v1";
  const SOLANA_PROVIDER_KEY = "coni.wallet.solana-provider.v1";
  const announcedEvmProviders = new Map();
  const pangeaTokens = new Map();
  const fundingAssets = new Map();
  const fundingAssetCache = new Map();
  const fundingAssetRequests = new Map();
  let pangeaTokensLoadedAt = 0;
  const solanaProviderHandlers = new WeakMap();
  const $ = (selector) => document.querySelector(selector);
  async function runBusy(button, task) {
    if (!button || button.dataset.busy === "true") return;
    const wasDisabled = button.disabled;
    const wasLoading = button.classList.contains("is-loading");
    button.dataset.busy = "true";
    button.disabled = true;
    button.classList.add("is-loading", "button-pop");
    button.setAttribute("aria-busy", "true");
    try {
      return await task();
    } finally {
      button.disabled = wasDisabled;
      button.dataset.busy = "false";
      if (!wasLoading) button.classList.remove("is-loading");
      button.removeAttribute("aria-busy");
    }
  }
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

  function cacheFastWalletSession(descriptor) {
    if (!descriptor?.secret || !descriptor.address || !descriptor.chain) return;
    fastWalletSession = { chain: descriptor.chain, address: descriptor.address, secret: descriptor.secret, lastUsedAt: Date.now() };
    scheduleFastWalletLock();
  }

  function clearFastWalletSession() {
    if (fastWalletSession) fastWalletSession.secret = "";
    fastWalletSession = null;
    if (fastWalletLockTimer) window.clearTimeout(fastWalletLockTimer);
    fastWalletLockTimer = null;
    if (typeof renderLocalVaultTools === "function" && $("#local-vault-status")) renderLocalVaultTools();
  }

  function scheduleFastWalletLock() {
    if (fastWalletLockTimer) window.clearTimeout(fastWalletLockTimer);
    fastWalletLockTimer = window.setTimeout(() => {
      clearFastWalletSession();
      message("Fast wallet locked after inactivity. Unlock it locally to continue.");
    }, FAST_WALLET_IDLE_MS);
  }

  function cachedFastWalletSecret(chain, address) {
    if (!fastWalletSession) return null;
    if (Date.now() - fastWalletSession.lastUsedAt > FAST_WALLET_IDLE_MS) {
      clearFastWalletSession();
      return null;
    }
    const sameChain = fastWalletSession.chain === chain;
    const sameAddress = chain === "solana"
      ? fastWalletSession.address === address
      : fastWalletSession.address.toLowerCase() === String(address || "").toLowerCase();
    if (!sameChain || !sameAddress) return null;
    fastWalletSession.lastUsedAt = Date.now();
    scheduleFastWalletLock();
    return fastWalletSession.secret;
  }

  function loadBrowserModule(src, globalName) {
    if (window[globalName]) return Promise.resolve(window[globalName]);
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.async = true;
      script.onload = () => window[globalName] ? resolve(window[globalName]) : reject(new Error(`${globalName} did not initialize`));
      script.onerror = () => reject(new Error(`${globalName} could not be loaded`));
      document.head.appendChild(script);
    });
  }

  function ensureLocalWalletModule() {
    localWalletModulePromise ||= loadBrowserModule("assets/local-wallet.js", "ConiLocalWallet").catch((error) => { localWalletModulePromise = null; throw error; });
    return localWalletModulePromise;
  }

  function ensureSolanaFundingModule() {
    solanaFundingModulePromise ||= loadBrowserModule("assets/solana-funding.js", "ConiSolana").catch((error) => { solanaFundingModulePromise = null; throw error; });
    return solanaFundingModulePromise;
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
      ["SafePal", window.safepal],
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
    if (provider.isSafePal || provider === window.safepalProvider || provider === window.safepal || provider === window.safepalwallet?.solana) return "SafePal";
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

  async function authenticateWallet(chain, address, sign, walletMode) {
    // The server authenticates the browser Origin header and the wallet
    // signature. walletMode is only a custody-mode preference; it never
    // authorizes a transfer and is applied server-side after verification.
    const challengeResponse = await fetch(`${config.apiBaseUrl}/api/auth/challenge`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ chain, address }) });
    const challenge = await challengeResponse.json();
    if (!challengeResponse.ok) throw new Error(challenge.error || "Could not create wallet challenge");
    message("Approve the free sign-in message. It cannot move funds…");
    const signature = await sign(challenge.message);
    const verifyResponse = await fetch(`${config.apiBaseUrl}/api/auth/verify`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ challengeId: challenge.challengeId, address, signature, walletMode }) });
    const verified = await verifyResponse.json();
    if (!verifyResponse.ok || !verified.authenticated) throw new Error(verified.error || "Wallet ownership could not be verified");
    return verified.wallet;
  }

  function personalSignPayload(messageText) {
    const bytes = new TextEncoder().encode(String(messageText));
    return `0x${Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")}`;
  }

  async function provisionLocalBotWallet() {
    const response = await fetch(`${config.apiBaseUrl}/api/bot-wallet/provision`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ mode: "internal-bot" }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "The internal bot-wallet record could not be attached");
    preferredBotMode = "internal-bot";
    localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
    return result;
  }

  async function authenticateLocalWallet(descriptor) {
    await ensureLocalWalletModule();
    const wallet = await authenticateWallet(
      descriptor.chain,
      descriptor.address,
      (messageText) => window.ConiLocalWallet.signMessage(descriptor.chain, descriptor.secret, messageText),
      "internal-bot",
    );
    cacheFastWalletSession(descriptor);
    let internalBotAttached = false;
    try {
      await provisionLocalBotWallet();
      internalBotAttached = true;
    } catch (error) {
      preferredBotMode = "external-bot";
      localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      message(`Wallet authenticated, but internal bot-wallet setup is waiting: ${error.message}`, "error");
    }
    let localBalance = "Local wallet";
    if (descriptor.chain !== "solana") {
      try {
        const raw = await localRpc(descriptor.chain, "eth_getBalance", [descriptor.address, "latest"]);
        localBalance = `${(Number(BigInt(raw)) / 1e18).toFixed(5)} native`;
      } catch { /* the wallet remains usable when the private relay is unavailable */ }
    }
    if (descriptor.chain === "solana") {
      state.solana = {
        provider: null,
        address: descriptor.address,
        chain: "solana",
        balance: localBalance,
        providerName: "Coni local wallet",
        authenticated: true,
        local: true,
      };
    } else {
      const chainId = config.wallets.evmChains[descriptor.chain];
      state.evm = {
        provider: null,
        address: descriptor.address,
        chain: descriptor.chain,
        chainId,
        chainName: chainNames[chainId] || descriptor.chain,
        balance: localBalance,
        providerName: "Coni local wallet",
        authenticated: true,
        local: true,
      };
    }
    saveConnections();
    renderConnections();
    loadFundingAssets(descriptor.chain);
    loadBotWallet();
    return { ...wallet, internalBotAttached };
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

  function tokenLogoUrl(image) {
    if (typeof image !== "string" || !image) return "";
    if (/^https?:\/\//i.test(image)) return image;
    const match = image.match(/^ipfs:\/\/([^/]+)(?:\/.*)?$/i);
    return match ? `https://www.pangea.trade/api/logo/${encodeURIComponent(match[1])}/128.webp` : "";
  }

  async function loadPangeaTokenRegistry() {
    if (Date.now() - pangeaTokensLoadedAt < 45_000) return;
    try {
      const response = await fetch(config.tokenRegistryUrl, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) return;
      const result = await response.json();
      const tokens = Array.isArray(result.tokens) ? result.tokens : [];
      pangeaTokens.clear();
      tokens.forEach((item) => {
        if (item?.slug) pangeaTokens.set(String(item.slug).toLowerCase(), item);
      });
      pangeaTokensLoadedAt = Date.now();
    } catch { /* history keeps its ticker fallback when the live feed is unavailable */ }
  }

  function tokenChip(item) {
    const slug = String(item?.slug || "");
    const meta = pangeaTokens.get(slug.toLowerCase()) || {};
    const symbol = String(meta.symbol || item?.symbol || slug || "TOKEN").toUpperCase();
    const image = tokenLogoUrl(meta.image || meta.logoURI || meta.logoUrl);
    const fallback = esc(symbol.slice(0, 4));
    const logo = image
      ? `<img class="token-logo-image" src="${esc(image)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-token-fallback="${fallback}" />`
      : fallback;
    return `<span class="network-pill token-pill"><b>${logo}</b><span>${esc(symbol)}</span></span>`;
  }

  function wireTokenLogoFallbacks() {
    document.querySelectorAll("#bot-wallet-history img[data-token-fallback]").forEach((image) => image.addEventListener("error", () => image.replaceWith(document.createTextNode(image.dataset.tokenFallback || "TOKEN")), { once: true }));
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
      await authenticateWallet(chain, address, (messageText) => provider.request({ method: "personal_sign", params: [personalSignPayload(messageText), address] }), "external-bot");
      preferredBotMode = "external-bot";
      localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      state.evm = { provider, address, chain, chainId, chainName: chainNames[chainId] || `Chain ${chainId}`, balance, providerName: providerLabel(provider), authenticated: true };
      saveConnections();
      renderConnections();
      message(`${state.evm.chainName} wallet connected.`, "success");
      loadFundingAssets(chain);
      loadBotWallet();
      provider.on?.("accountsChanged", async () => {
        const previous = state.evm;
        state.evm = null;
        saveConnections();
        renderConnections();
        await logoutWalletIdentity(previous);
        await connectEvm();
      });
      provider.on?.("chainChanged", () => connectEvm());
    } catch (error) {
      message(error?.message || "The EVM wallet connection was cancelled.", "error");
    }
  }

  async function establishSolanaSession(provider, address) {
    if (!address || !provider.signMessage) throw new Error("This Solana wallet does not expose off-chain message signing");
    let balance = "Connected";
    try {
      const result = await localRpc("solana", "getBalance", [address, { commitment: "confirmed" }]);
      balance = `${(Number(result?.value || 0) / 1e9).toFixed(5)} SOL`;
    } catch { /* balance stays connected when the private relay is unavailable */ }
    await authenticateWallet("solana", address, async (messageText) => {
      const messageBytes = new TextEncoder().encode(messageText);
      // SafePal's Solana provider accepts the message bytes as its only
      // argument. Other wallet-adapter compatible providers also accept the
      // Uint8Array; the second encoding argument is not part of the Solana
      // signMessage contract and causes SafePal to produce an incompatible
      // result on some versions.
      const signed = provider === window.safepal
        ? await provider.signMessage(messageBytes)
        : await provider.signMessage(messageBytes, "utf8");
      const bytes = signed.signature || signed;
      return btoa(String.fromCharCode(...bytes));
    }, "external-bot");
    preferredBotMode = "external-bot";
    localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
    state.solana = { provider, address, chain: "solana", balance, providerName: providerLabel(provider, true), authenticated: true };
    saveConnections();
    renderConnections();
    loadFundingAssets("solana");
    loadBotWallet();
  }

  async function logoutWalletIdentity(identity) {
    if (!identity?.chain || !identity?.address) return;
    try {
      await fetch(`${config.apiBaseUrl}/api/auth/logout-identity`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ chain: identity.chain, address: identity.address }),
      });
    } catch { /* the next wallet signature cannot authorize the removed UI state */ }
  }

  async function handleSolanaAccountChanged(provider, publicKey) {
    const address = publicKey?.toString?.() || "";
    if (address && state.solana?.address === address && state.solana.authenticated) return;
    state.solana = null;
    botWalletSnapshot = null;
    activeBotWallet = null;
    saveConnections();
    renderConnections();
    $("#bot-wallet-state").textContent = "CONNECT FIRST";
    $("#bot-wallet-content").querySelector("p").textContent = "Reconnect and sign in with the active Solana account to view its bot wallet.";
    await logoutWalletIdentity({ chain: "solana", address: state.solana?.address || address });
    if (!address) {
      message("Solana wallet disconnected.", "success");
      return;
    }
    try {
      message("Wallet account changed. Approve a new ownership signature…");
      await establishSolanaSession(provider, address);
      message("Solana wallet re-authenticated.", "success");
    } catch (error) {
      message(error?.message || "The new Solana account could not be authenticated.", "error");
    }
  }

  function bindSolanaProvider(provider) {
    if (!provider?.on || solanaProviderHandlers.has(provider)) return;
    const handler = (publicKey) => handleSolanaAccountChanged(provider, publicKey);
    solanaProviderHandlers.set(provider, handler);
    provider.on("accountChanged", handler);
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
      await establishSolanaSession(provider, address);
      bindSolanaProvider(provider);
      message("Solana wallet connected.", "success");
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
      return `<div class="connection-row"><span class="provider-icon ${isSolana ? "sol" : "eth"}">${isSolana ? "S" : "E"}</span><div><strong>${item.providerName || (isSolana ? "Solana wallet" : "EVM wallet")}</strong><small>${short(item.address)} · ${isSolana ? "Solana" : item.chainName}</small></div><span class="connection-balance">${item.balance}</span><span class="connected-dot">●</span><button class="ghost-button connection-disconnect" data-disconnect-wallet="${isSolana ? "solana" : "evm"}">Disconnect</button></div>`;
    }).join("");
    if (state.evm) $("#eth-balance").textContent = state.evm.balance;
    if (state.solana) $("#sol-balance").textContent = state.solana.balance;
  }

  async function disconnectWallet(kind) {
    const identity = kind === "solana" ? state.solana : state.evm;
    if (!identity) return;
    if (kind === "solana") state.solana = null;
    else state.evm = null;
    saveConnections();
    renderConnections();
    activeBotWallet = null;
    botWalletSnapshot = null;
    $("#bot-wallet-state").textContent = "CONNECT FIRST";
    $("#bot-wallet-content").innerHTML = `<div class="vault-ring large"><span>⌁</span></div><div><strong>Bot wallet paused</strong><p>Connect a wallet to view its separate bot-wallet record.</p></div>`;
    await logoutWalletIdentity({ chain: identity.chain, address: identity.address });
    message(`${identity.providerName || "Wallet"} disconnected. Fast Wallet remains available.`, "success");
    renderLocalVaultTools();
  }

  async function prepareFunding() {
    const asset = $("#funding-asset").value;
    const amount = $("#funding-amount").value.trim();
    const requestedChain = botFundingMode ? activeBotWallet?.chain : (state.solana?.authenticated ? "solana" : state.evm?.chain);
    const fundingAddress = requestedChain === "solana" ? state.solana?.address : state.evm?.address;
    if (!requestedChain || !fundingAddress || (requestedChain === "solana" ? state.solana?.restored : state.evm?.restored)) { $("#funding-status").textContent = "Connect the wallet for the selected bot network before preparing funding."; return; }
    const sourceWallet = requestedChain === "solana" ? state.solana : state.evm;
    const assetDecimals = Number(fundingAssets.get(asset)?.decimals);
    const decimalLimit = Number.isInteger(assetDecimals) && assetDecimals >= 0 && assetDecimals <= 36 ? assetDecimals : 18;
    const amountPattern = new RegExp(`^\\d+(\\.\\d{1,${decimalLimit}})?$`);
    if (!amountPattern.test(amount) || Number(amount) <= 0) { $("#funding-status").textContent = `Enter a positive amount with no more than ${decimalLimit} decimal places.`; return; }
    $("#funding-status").textContent = "Asking the secure backend for a whitelisted funding intent…";
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/intent`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ asset, amount, chain: requestedChain, wallet: fundingAddress, botWalletId: botFundingMode ? activeBotWallet?.id : null }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Funding intent rejected");
      activeIntent = result;
      const feeCopy = result.feeAmountBaseUnits && result.feeAmountBaseUnits !== "0" ? ` · fee ${result.feeAmountBaseUnits} base units · net credit ${result.netAmountBaseUnits}` : " · fee 0 base units";
      $("#funding-status").textContent = `${result.botWalletId ? "Bot top-up: " : ""}Send exactly ${result.amount} ${result.asset} to ${short(result.destination)}${feeCopy}. Review the full destination in your wallet before approving.`;
      $("#approve-funding").hidden = result.chain === "solana"
        ? !(state.solana?.local || (state.solana?.provider && !state.solana?.restored))
        : !(state.evm?.local || (state.evm?.provider && !state.evm?.restored));
      $("#approve-funding").textContent = result.chain === "solana" ? "Approve in connected Solana wallet ↗" : "Approve in connected EVM wallet ↗";
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

  async function claimFunding(txHash, eventIndex = 0) {
    if (!activeIntent || !txHash) throw new Error("Create a funding intent and provide a transaction hash first");
    const response = await fetch(`${config.apiBaseUrl}/api/funding/claim`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ intentId: activeIntent.intentId, txHash, eventIndex }) });
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
          $("#funding-status").textContent = "Funding confirmed and credited to the ledger. Rechecking bot reserve and automation state…";
          botFundingMode = false;
          await loadBotWallet(activeBotWallet?.id || null);
          $("#funding-status").textContent = "Funding confirmed. The bot will scan after the reserve, policy, fresh-quote, and signer gates pass.";
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

  async function localRpc(chain, method, params = []) {
    const response = await fetch(`${config.apiBaseUrl}/api/rpc/${encodeURIComponent(chain)}`, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: crypto.randomUUID(), method, params }),
    });
    const payload = await response.json();
    if (!response.ok || payload.error) throw new Error(payload.error?.message || "Private Coni RPC request failed");
    return payload.result;
  }

  async function unlockLocalEvm() {
    await ensureLocalWalletModule();
    const vault = readLocalVault();
    if (!vault || vault.chain === "solana") throw new Error("No encrypted EVM wallet is available in this browser");
    const cached = cachedFastWalletSecret(vault.chain, state.evm?.address || vault.address);
    if (cached) return { secret: cached, chain: vault.chain };
    const password = $("#existing-wallet-password")?.value || window.prompt("Enter your fast-wallet password to approve this transaction. It will not be stored or sent to Coni.");
    if (!password) throw new Error("Local wallet unlock cancelled");
    const secret = await decryptLocalSecret(vault, password);
    const descriptor = window.ConiLocalWallet.describe(vault.chain, secret);
    if (!state.evm?.address || descriptor.address.toLowerCase() !== state.evm.address.toLowerCase()) {
      throw new Error("The encrypted local wallet does not match the authenticated wallet");
    }
    cacheFastWalletSession(descriptor);
    if ($("#existing-wallet-password")) $("#existing-wallet-password").value = "";
    return { secret, chain: vault.chain };
  }

  async function unlockLocalSolana() {
    await ensureLocalWalletModule();
    const vault = readLocalVault();
    if (!vault || vault.chain !== "solana") throw new Error("No encrypted Solana wallet is available in this browser");
    const cached = cachedFastWalletSecret("solana", state.solana?.address || vault.address);
    if (cached) return { secret: cached };
    const password = $("#existing-wallet-password")?.value || window.prompt("Enter your fast-wallet password to approve this transaction. It will not be stored or sent to Coni.");
    if (!password) throw new Error("Local wallet unlock cancelled");
    const secret = await decryptLocalSecret(vault, password);
    const descriptor = window.ConiLocalWallet.describe("solana", secret);
    if (!state.solana?.address || descriptor.address !== state.solana.address) throw new Error("The encrypted local wallet does not match the authenticated wallet");
    cacheFastWalletSession(descriptor);
    if ($("#existing-wallet-password")) $("#existing-wallet-password").value = "";
    return { secret };
  }

  async function sendLocalEvmFunding(intent) {
    await ensureLocalWalletModule();
    if (!window.ConiLocalWallet?.signEvmTransaction) throw new Error("Local EVM signer is unavailable; refresh the page");
    const unlocked = await unlockLocalEvm();
    let secret = unlocked.secret;
    try {
      const chainIdHex = await localRpc(intent.chain, "eth_chainId");
      const chainId = Number.parseInt(chainIdHex, 16);
      const expectedChainId = config.wallets.evmChains[intent.chain];
      if (!Number.isInteger(chainId) || (expectedChainId && chainId !== expectedChainId)) throw new Error("Private RPC returned the wrong chain identity");
      const native = intent.asset === (intent.chain === "bsc" ? "BNB" : "ETH");
      let transaction;
      if (native) {
        transaction = { from: state.evm.address, to: intent.destination, value: `0x${BigInt(intent.amountBaseUnits).toString(16)}`, data: "0x" };
      } else {
        const tokenAddress = String(intent.tokenAddress || "");
        if (!/^0x[a-fA-F0-9]{40}$/.test(tokenAddress)) throw new Error("The server did not provide a verified token contract");
        const recipient = intent.destination.slice(2).toLowerCase().padStart(64, "0");
        const amount = BigInt(intent.amountBaseUnits).toString(16).padStart(64, "0");
        transaction = { from: state.evm.address, to: tokenAddress, value: "0x0", data: `0xa9059cbb${recipient}${amount}` };
      }
      const [nonceHex, gasPriceHex, gasHex] = await Promise.all([
        localRpc(intent.chain, "eth_getTransactionCount", [state.evm.address, "pending"]),
        localRpc(intent.chain, "eth_gasPrice"),
        localRpc(intent.chain, "eth_estimateGas", [transaction]),
      ]);
      const raw = await window.ConiLocalWallet.signEvmTransaction(intent.chain, secret, {
        type: "legacy",
        chainId,
        nonce: BigInt(nonceHex),
        gasPrice: BigInt(gasPriceHex),
        gas: BigInt(gasHex),
        to: transaction.to,
        value: BigInt(transaction.value),
        data: transaction.data,
      });
      return localRpc(intent.chain, "eth_sendRawTransaction", [raw]);
    } finally {
      secret = "";
    }
  }

  async function approveFunding() {
    if (!activeIntent) throw new Error("Create a funding intent before approving");
    if (activeIntent.chain === "solana") {
      let secret = "";
      if (state.solana?.local) secret = (await unlockLocalSolana()).secret;
      if (!secret && (!state.solana?.provider || state.solana.restored)) throw new Error("Reconnect the Solana wallet before approving");
      await ensureSolanaFundingModule();
      if (!window.ConiSolana?.sendFunding) throw new Error("The Solana transaction module is unavailable; refresh the page and try again");
      $("#funding-status").textContent = "Reviewing the exact Solana destination, mint, and amount in your wallet…";
      try {
        const result = await window.ConiSolana.sendFunding({
          provider: secret ? null : state.solana.provider,
          secret,
          rpcUrl: config.solanaRpcUrl,
          from: state.solana.address,
          destination: activeIntent.destination,
          amountBaseUnits: activeIntent.amountBaseUnits,
          asset: activeIntent.asset,
          tokenAddress: activeIntent.tokenAddress,
          tokenProgram: activeIntent.tokenProgram,
          decimals: activeIntent.decimals,
        });
        $("#funding-tx-hash").value = result.signature;
        await claimFunding(result.signature, result.eventIndex);
      } finally {
        secret = "";
      }
      return;
    }
    if (state.evm?.local) {
      $("#funding-status").textContent = "Unlocking the local wallet. Review the exact destination and amount before signing…";
      const txHash = await sendLocalEvmFunding(activeIntent);
      $("#funding-tx-hash").value = txHash;
      await claimFunding(txHash);
      return;
    }
    if (!state.evm?.provider || state.evm.restored) throw new Error("Reconnect the EVM wallet before approving");
    const expectedChainId = config.wallets.evmChains[activeIntent.chain];
    const actualChainId = Number.parseInt(await state.evm.provider.request({ method: "eth_chainId" }), 16);
    if (expectedChainId && actualChainId !== expectedChainId) throw new Error(`Switch the connected EVM wallet to ${chainNames[expectedChainId] || activeIntent.chain} before approving`);
    $("#funding-status").textContent = "Reviewing the exact destination and amount in your wallet…";
    const native = activeIntent.asset === (activeIntent.chain === "bsc" ? "BNB" : "ETH");
    let transaction;
    if (native) {
      transaction = { from: state.evm.address, to: activeIntent.destination, value: `0x${BigInt(activeIntent.amountBaseUnits).toString(16)}` };
    } else {
      const tokenAddress = String(activeIntent.tokenAddress || "");
      if (!/^0x[a-fA-F0-9]{40}$/.test(tokenAddress)) throw new Error("The server did not provide a verified token contract for this funding asset");
      const recipient = activeIntent.destination.slice(2).toLowerCase().padStart(64, "0");
      const amount = BigInt(activeIntent.amountBaseUnits).toString(16).padStart(64, "0");
      transaction = { from: state.evm.address, to: tokenAddress, value: "0x0", data: `0xa9059cbb${recipient}${amount}` };
    }
    const txHash = await state.evm.provider.request({ method: "eth_sendTransaction", params: [transaction] });
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

  function formatBaseUnits(value, decimals = 0) {
    const raw = String(value ?? "");
    if (!/^\d+$/.test(raw)) return "—";
    const scale = Number(decimals);
    if (!Number.isInteger(scale) || scale <= 0) return raw;
    const padded = raw.padStart(scale + 1, "0");
    const whole = padded.slice(0, -scale) || "0";
    const fraction = padded.slice(-scale).replace(/0+$/, "").slice(0, 8);
    return fraction ? `${whole}.${fraction}` : whole;
  }

  function applyFundingAssets(chain, assets) {
    fundingAssets.clear();
    (assets || []).forEach((item) => fundingAssets.set(String(item.asset).toUpperCase(), item));
    renderFundingAssetOptions(chain);
  }

  function renderFundingAssetOptions(chain) {
    const native = chain === "solana" ? "SOL" : chain === "bsc" ? "BNB" : "ETH";
    const fundingSelect = $("#funding-asset");
    if (fundingSelect) {
      const current = fundingSelect.value;
      fundingSelect.replaceChildren();
      if (!fundingAssets.size) fundingAssets.set(native, { asset: native, decimals: native === "SOL" ? 9 : 18, native: true });
      fundingAssets.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.asset;
        option.textContent = item.native ? item.asset : `${item.asset} token`;
        fundingSelect.appendChild(option);
      });
      fundingSelect.value = fundingAssets.has(current) ? current : (fundingAssets.has(native) ? native : fundingSelect.options[0]?.value || "");
    }
    const withdrawalSelect = $("#bot-withdraw-asset");
    if (withdrawalSelect) {
      const current = withdrawalSelect.value;
      withdrawalSelect.replaceChildren();
      if (!fundingAssets.size) fundingAssets.set(native, { asset: native, decimals: native === "SOL" ? 9 : 18, native: true });
      fundingAssets.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.asset;
        option.textContent = item.native ? item.asset : `${item.asset} token`;
        withdrawalSelect.appendChild(option);
      });
      withdrawalSelect.value = fundingAssets.has(current) ? current : (fundingAssets.has(native) ? native : withdrawalSelect.options[0]?.value || "");
    }
  }

  async function loadFundingAssets(chain) {
    if (!chain) return;
    const cached = fundingAssetCache.get(chain);
    if (cached) {
      applyFundingAssets(chain, cached);
      return;
    }
    const pending = fundingAssetRequests.get(chain);
    if (pending) {
      await pending;
      return;
    }
    const native = chain === "solana" ? "SOL" : chain === "bsc" ? "BNB" : "ETH";
    applyFundingAssets(chain, [{ asset: native, decimals: native === "SOL" ? 9 : 18, native: true }]);
    const request = (async () => {
      try {
        const response = await fetch(`${config.apiBaseUrl}/api/funding/assets?chain=${encodeURIComponent(chain)}`, { credentials: "include", cache: "no-store" });
        if (!response.ok) return;
        const result = await response.json();
        const assets = Array.isArray(result.assets) ? result.assets : [];
        if (assets.length) fundingAssetCache.set(chain, assets);
        const isCurrentChain = activeBotWallet?.chain === chain || (!activeBotWallet && (state.solana?.chain === chain || state.evm?.chain === chain));
        if (isCurrentChain) applyFundingAssets(chain, assets.length ? assets : [{ asset: native, decimals: native === "SOL" ? 9 : 18, native: true }]);
      } catch { /* native asset remains available while the registry is unavailable */ }
    })();
    fundingAssetRequests.set(chain, request);
    try {
      await request;
    } finally {
      fundingAssetRequests.delete(chain);
    }
  }

  function botWalletChoices() {
    // The API returns one active custody mode per authenticated identity. Do
    // not apply one browser-wide mode filter: an EVM identity and a Solana
    // identity may legitimately have different selected modes in the same
    // signed session.
    return botWalletSnapshot?.wallets || [];
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
    const botTopupButton = $("#bot-topup");
    const botWithdrawButton = $("#bot-withdraw");
    const botTransferReady = wallet.status === "active"
      && Boolean(wallet.address)
      && wallet.custody === "audited-signer";
    [botTopupButton, botWithdrawButton].forEach((button) => {
      if (!button) return;
      button.disabled = !botTransferReady;
      button.title = botTransferReady
        ? ""
        : "This bot wallet must receive an audited signer address before transfers are enabled.";
    });
    loadFundingAssets(wallet.chain);
    const internalWallet = wallet.mode === "internal-bot";
    $("#internal-wallet-toggle").hidden = !internalWallet;
    const surface = internalWallet && document.body.dataset.walletSurface === "normal" ? "normal" : "bot";
    if (!internalWallet) document.body.dataset.walletSurface = "bot";
    const isBotSurface = surface === "bot";
    $("#bot-wallet-state").textContent = isBotSurface ? (wallet.status === "active" ? "ACTIVE" : "GATED") : "NORMAL VIEW";
    $(".bot-wallet-actions")?.toggleAttribute("hidden", !isBotSurface);
    $("#bot-withdraw-form").hidden = true;
    const balances = (wallet.balances || []).map((item) => `<span class="network-pill">${esc(item.asset)} ${esc(formatBaseUnits(item.amountBaseUnits, item.decimals))}</span>`).join(" ") || "No verified balance";
    const normalState = wallet.chain === "solana" ? state.solana : state.evm;
    if (!isBotSurface) {
      const normalAddress = wallet.sourceAddress || normalState?.address;
      const normalBalance = normalState?.balance || "Balance refresh requires a connected provider";
      $("#bot-wallet-content").innerHTML = `<div class="vault-ring large"><span>◉</span></div><div><strong>Normal wallet</strong><p>${normalAddress ? `${esc(short(normalAddress))} · ${esc(wallet.chain)} · ${esc(normalBalance)}` : "Connect or unlock this local wallet to read its balance."}</p></div>`;
      $("#bot-wallet-history").innerHTML = `<div class="bot-pnl-strip"><strong>USER WALLET</strong><small>This is the funding source. Trading activity and PNL remain isolated in the attached bot wallet.</small></div>`;
      return;
    }
    const automation = wallet.automation || {};
    const automationLabels = {
      "awaiting-signer": "Awaiting signer attestation",
      "global-paused": "Paused by global safety control",
      "user-paused": "Paused in this wallet’s settings",
      "awaiting-reserve-policy": "Awaiting native reserve policy",
      "awaiting-funding": "Funded balance required before scanning",
      "ready-to-scan": "Ready for automatic opportunity scanning",
    };
    const automationLabel = automationLabels[automation.state] || "Planner status unavailable";
    const automationNote = automation.state === "ready-to-scan"
      ? "The worker will re-quote eligible Pangea routes; the signer remains the final execution gate."
      : automation.state === "awaiting-funding"
        ? `Top up at least ${formatBaseUnits(automation.requiredNativeReserveBaseUnits, wallet.chain === "solana" ? 9 : 18)} ${automation.nativeAsset || "native"} to enter the scan queue.`
        : automationLabel;
    $("#bot-wallet-content").innerHTML = `<div class="vault-ring large"><span>⌁</span></div><div><strong>${wallet.status === "active" ? "Bot wallet ready" : "Bot wallet record attached"}</strong><p>${wallet.address ? `${esc(short(wallet.address))} · ${esc(wallet.chain)} · ${balances}` : "Awaiting the audited signer/MPC provisioner. No address or private key has been fabricated."}</p><small class="bot-automation-state">${esc(automationLabel)}</small><small>${esc(automationNote)}</small></div>`;
    const tokens = (botWalletSnapshot?.tradedTokens || []).map(tokenChip).join(" ") || "No completed token trades";
    const pnl = botWalletSnapshot?.pnl || {};
    const compounding = botWalletSnapshot?.compounding || {};
        $("#bot-wallet-history").innerHTML = `<div class="bot-pnl-strip"><strong>PNL</strong><b>$${esc(pnl.realizedUsd || "0")}</b><div class="bot-pnl-grid"><span><small>Expected</small><b>$${esc(pnl.expectedUsd || "0")}</b></span><span><small>Compounded</small><b>$${esc(compounding.allocatedProfitUsd || "0")}</b></span><span><small>Payable</small><b>$${esc(compounding.payoutProfitUsd || "0")}</b></span></div><small>${esc(pnl.settledTrades || 0)} settled trades · ${esc(botWalletSnapshot?.tradedTokens?.length || 0)} Pangea tokens tracked</small><div class="bot-token-list">${tokens}</div></div>` + (botWalletSnapshot?.history || []).slice(0, 8).map((item) => { const route = item.buyChain && item.sellChain ? ` · ${esc(item.buyChain)} → ${esc(item.sellChain)}` : ""; const result = item.realizedProfitUsd && item.realizedProfitUsd !== "0" ? `$${esc(item.realizedProfitUsd)}` : item.expectedProfitUsd && item.expectedProfitUsd !== "0" ? `exp $${esc(item.expectedProfitUsd)}` : "—"; const fee = item.fee ? ` · fee ${esc(item.fee.amountBaseUnits)} ${esc(item.fee.asset)} (${esc(item.fee.feeBps)} bps)` : ""; return `<div class="activity-item"><span class="activity-kind ${item.kind === "trade" ? "trade" : "bridge"}">${esc(String(item.kind).toUpperCase())}</span><div><strong>${esc(item.slug || item.asset || "Bot wallet")}</strong><small>${esc(item.state)}${route}${fee} · ${esc(item.createdAt)}</small></div><b>${result}</b></div>`; }).join("");
    wireTokenLogoFallbacks();
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
      if (result.botControl?.walletMode === "external-bot" || result.botControl?.walletMode === "internal-bot") {
        preferredBotMode = result.botControl.walletMode;
        localStorage.setItem("coni.bot-wallet.mode.v1", preferredBotMode);
      }
      await loadPangeaTokenRegistry();
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

  async function encryptLocalSecret(secret, password, chain, address = "") {
    const encoder = new TextEncoder();
    const normalizedChain = chain === "evm" ? "base" : chain;
    const normalizedAddress = String(address || "");
    const kdfIterations = 310000;
    const aad = `coni-fast-wallet:v2:${normalizedChain}:${normalizedAddress}`;
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const material = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveKey"]);
    const key = await crypto.subtle.deriveKey({ name: "PBKDF2", salt, iterations: kdfIterations, hash: "SHA-256" }, material, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
    const ciphertext = await crypto.subtle.encrypt({ name: "AES-GCM", iv, additionalData: encoder.encode(aad) }, key, encoder.encode(secret));
    const encode = (bytes) => btoa(String.fromCharCode(...bytes));
    return { version: 2, walletType: "fast", chain: normalizedChain, address: normalizedAddress, algorithm: "PBKDF2-AES-GCM", kdfIterations, aad, salt: encode(salt), iv: encode(iv), ciphertext: encode(new Uint8Array(ciphertext)), createdAt: new Date().toISOString() };
  }

  function readLocalVault() {
    try {
      const raw = localStorage.getItem(FAST_WALLET_STORAGE_KEY) || localStorage.getItem(LEGACY_LOCAL_VAULT_KEY);
      const vault = JSON.parse(raw || "null");
      if (!vault || ![1, 2].includes(vault.version) || !["ethereum", "base", "robinhood", "bsc", "evm", "solana"].includes(vault.chain) || vault.algorithm !== "PBKDF2-AES-GCM") return null;
      if (vault.version === 2 && (vault.walletType !== "fast" || !Number.isInteger(vault.kdfIterations) || vault.kdfIterations < 250000 || typeof vault.aad !== "string")) return null;
      if (![vault.salt, vault.iv, vault.ciphertext].every((value) => typeof value === "string" && value.length > 0)) return null;
      return vault;
    } catch {
      return null;
    }
  }

  async function decryptLocalSecret(vault, password) {
    const decode = (value) => Uint8Array.from(atob(value), (character) => character.charCodeAt(0));
    const encoder = new TextEncoder();
    const material = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveKey"]);
    const iterations = vault.version === 2 ? vault.kdfIterations : 250000;
    const key = await crypto.subtle.deriveKey({ name: "PBKDF2", salt: decode(vault.salt), iterations, hash: "SHA-256" }, material, { name: "AES-GCM", length: 256 }, false, ["decrypt"]);
    const params = { name: "AES-GCM", iv: decode(vault.iv) };
    if (vault.version === 2) params.additionalData = encoder.encode(vault.aad);
    const plaintext = await crypto.subtle.decrypt(params, key, decode(vault.ciphertext));
    return new TextDecoder().decode(plaintext);
  }

  function renderLocalVaultTools() {
    const vault = readLocalVault();
    const tools = $("#local-vault-tools");
    const verify = $("#verify-local-vault");
    const status = $("#local-vault-status");
    if (!tools || !verify || !status) return;
    tools.hidden = !vault;
    verify.hidden = !vault;
    $("#unlock-fast-wallet")?.toggleAttribute("hidden", !vault || Boolean(fastWalletSession));
    $("#lock-fast-wallet")?.toggleAttribute("hidden", !fastWalletSession);
    if (!vault) {
      status.textContent = "No encrypted local backup stored in this browser.";
      return;
    }
    $("#existing-wallet-chain").value = vault.chain === "evm" ? "base" : vault.chain;
    const created = vault.createdAt ? new Date(vault.createdAt).toLocaleString() : "unknown time";
    const stateLabel = fastWalletSession ? "UNLOCKED IN MEMORY" : "LOCKED";
    status.textContent = `FAST WALLET ${stateLabel} · encrypted ${vault.chain === "solana" ? "Solana" : vault.chain.toUpperCase()} backup${vault.address ? ` · ${short(vault.address)}` : ""} · created ${created}`;
  }

  async function unlockFastWallet() {
    await ensureLocalWalletModule();
    const vault = readLocalVault();
    if (!vault) throw new Error("No encrypted fast-wallet backup is available in this browser");
    const password = $("#existing-wallet-password")?.value;
    if (!password || password.length < 12) throw new Error("Enter the fast-wallet password first. It is used only in this browser.");
    let secret = "";
    try {
      secret = await decryptLocalSecret(vault, password);
      const descriptor = window.ConiLocalWallet.describe(vault.chain, secret);
      if (vault.address && (vault.chain === "solana" ? descriptor.address !== vault.address : descriptor.address.toLowerCase() !== vault.address.toLowerCase())) throw new Error("The encrypted backup address does not match its wallet secret");
      await authenticateLocalWallet(descriptor);
      cacheFastWalletSession(descriptor);
      $("#vault-action-status").textContent = `Fast wallet unlocked and authenticated as ${short(descriptor.address)}. The key remains in memory only and will auto-lock after inactivity.`;
      renderLocalVaultTools();
    } finally {
      secret = "";
      if ($("#existing-wallet-password")) $("#existing-wallet-password").value = "";
    }
  }

  function lockFastWallet() {
    clearFastWalletSession();
    $("#vault-action-status").textContent = "Fast wallet locked. Its encrypted backup remains in this browser; no key was sent to Coni.";
  }

  function ensureRecoveryPanel() {
    const parent = $("#new-wallet-panel");
    if (!parent || $("#local-recovery-panel")) return;
    const panel = document.createElement("div");
    panel.id = "local-recovery-panel";
    panel.className = "modal-note recovery-panel";
    panel.hidden = true;
    panel.innerHTML = `<span>!</span><div><strong>Save this recovery secret offline</strong><p id="local-recovery-copy">It is shown only in this browser. Never send it to Coni or paste it into a website.</p><button class="ghost-button" id="reveal-local-recovery">Reveal recovery secret</button><textarea id="local-recovery-secret" class="recovery-secret" rows="3" readonly hidden spellcheck="false"></textarea><div class="recovery-actions" hidden><button class="ghost-button" id="copy-local-recovery">Copy</button><button class="ghost-button" id="hide-local-recovery">Hide and clear</button></div></div>`;
    parent.appendChild(panel);
    $("#reveal-local-recovery").addEventListener("click", () => {
      if (!pendingRecoverySecret) return;
      const field = $("#local-recovery-secret");
      field.value = pendingRecoverySecret.secret;
      field.hidden = false;
      $(".recovery-actions").hidden = false;
      $("#reveal-local-recovery").hidden = true;
    });
    $("#copy-local-recovery").addEventListener("click", async () => {
      const value = $("#local-recovery-secret").value;
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value);
        $("#local-recovery-copy").textContent = "Copied. Store it offline, then clear this panel.";
      } catch {
        $("#local-recovery-copy").textContent = "Clipboard access was denied. Select the secret and store it offline.";
      }
    });
    $("#hide-local-recovery").addEventListener("click", () => {
      $("#local-recovery-secret").value = "";
      $("#local-recovery-secret").hidden = true;
      $(".recovery-actions").hidden = true;
      $("#reveal-local-recovery").hidden = false;
      $("#local-recovery-panel").hidden = true;
      pendingRecoverySecret = null;
    });
  }

  function showRecoverySecret(chain, secret) {
    ensureRecoveryPanel();
    pendingRecoverySecret = { chain, secret };
    $("#local-recovery-panel").hidden = false;
    $("#local-recovery-copy").textContent = `This ${chain === "solana" ? "Solana secret" : "EVM private key"} is shown only in this browser. Never send it to Coni or paste it into a website.`;
  }

  function clearBrowserSecrets() {
    clearFastWalletSession();
    pendingRecoverySecret = null;
    ["#local-recovery-secret", "#existing-private-key", "#existing-wallet-password", "#new-wallet-password", "#new-wallet-password-confirm"].forEach((selector) => {
      const field = $(selector);
      if (field) field.value = "";
    });
    const recoveryPanel = $("#local-recovery-panel");
    if (recoveryPanel) recoveryPanel.hidden = true;
  }

  async function verifyLocalVault() {
    const vault = readLocalVault();
    const passwordField = $("#existing-wallet-password");
    const status = $("#local-vault-status");
    if (!vault) { renderLocalVaultTools(); return; }
    const password = passwordField.value;
    if (password.length < 12) {
      status.textContent = "Enter the local backup password (at least 12 characters) to verify it.";
      return;
    }
    let secret = "";
    try {
      secret = await decryptLocalSecret(vault, password);
      status.textContent = "Local password verified; the decrypted secret never left this browser.";
    } catch {
      status.textContent = "Password or encrypted backup is invalid. Nothing was sent to Coni.";
    } finally {
      secret = "";
      passwordField.value = "";
    }
  }

  function exportLocalVault() {
    const vault = readLocalVault();
    if (!vault) { renderLocalVaultTools(); return; }
    const blob = new Blob([JSON.stringify(vault, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `coni-fast-wallet-backup-${vault.chain}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    $("#local-vault-status").textContent = "Encrypted backup exported. It contains ciphertext only; protect the file and password separately.";
  }

  function deleteLocalVault() {
    if (!readLocalVault()) { renderLocalVaultTools(); return; }
    if (!window.confirm("Delete this encrypted local backup from this browser? This does not affect any on-chain wallet or bot wallet.")) return;
    localStorage.removeItem(FAST_WALLET_STORAGE_KEY);
    localStorage.removeItem(LEGACY_LOCAL_VAULT_KEY);
    clearFastWalletSession();
    renderLocalVaultTools();
    $("#local-vault-status").textContent = "Encrypted local backup deleted from this browser.";
  }

  async function importLocalWallet() {
    await ensureLocalWalletModule();
    const chain = $("#existing-wallet-chain").value;
    const privateKey = $("#existing-private-key").value.trim();
    const password = $("#existing-wallet-password").value;
    let descriptor = null;
    try {
      descriptor = window.ConiLocalWallet?.describe(chain, privateKey);
    } catch {
      descriptor = null;
    }
    if (!descriptor || password.length < 12) {
      $("#vault-action-status").textContent = chain === "solana" ? "Enter a valid Solana base58 secret or 64-byte JSON array and a password of at least 12 characters. The secret remains in this browser and is never posted." : "Enter a valid EVM private key and a password of at least 12 characters. The key remains in this browser and is never posted.";
      return;
    }
    if (readLocalVault() && !window.confirm("Replace the existing encrypted local backup? The old backup will be overwritten.")) return;
    let secret = descriptor.secret;
    const vault = await encryptLocalSecret(secret, password, chain, descriptor.address);
    localStorage.setItem(FAST_WALLET_STORAGE_KEY, JSON.stringify(vault));
    try {
      const session = await authenticateLocalWallet(descriptor);
      const botStatus = session.internalBotAttached ? " Internal bot-wallet records are attached and awaiting signer provisioning." : " Bot-wallet attachment is waiting for the backend; retry the internal setup before funding.";
      $("#vault-action-status").textContent = `Encrypted ${chain === "solana" ? "Solana" : "EVM"} wallet and authenticated ${short(descriptor.address)}. Transactions can be signed locally after you unlock the vault.${botStatus}`;
    } catch {
      $("#vault-action-status").textContent = `Encrypted ${chain === "solana" ? "Solana" : "EVM"} local backup created. Connect this address through a wallet provider to authenticate and fund it.`;
    } finally {
      secret = "";
      descriptor.secret = "";
    }
    $("#existing-private-key").value = "";
    $("#existing-wallet-password").value = "";
    renderLocalVaultTools();
  }

  async function createLocalWallet() {
    await ensureLocalWalletModule();
    const chain = $("#new-wallet-chain").value;
    const password = $("#new-wallet-password").value;
    const confirm = $("#new-wallet-password-confirm").value;
    if (password.length < 12 || password !== confirm) {
      $("#vault-action-status").textContent = "Use a password of at least 12 characters and confirm it exactly.";
      return;
    }
    if (!window.ConiLocalWallet) throw new Error("Local wallet module is unavailable");
    if (readLocalVault() && !window.confirm("Replace the existing encrypted local backup? The old backup will be overwritten.")) return;
    const generated = window.ConiLocalWallet.create(chain);
    let secret = generated.secret;
    const vault = await encryptLocalSecret(secret, password, chain, generated.address);
    localStorage.setItem(FAST_WALLET_STORAGE_KEY, JSON.stringify(vault));
    showRecoverySecret(chain, secret);
    try {
      const session = await authenticateLocalWallet(generated);
      const botStatus = session.internalBotAttached ? " Internal bot-wallet records are attached and awaiting signer provisioning." : " Bot-wallet attachment is waiting for the backend; retry the internal setup before funding.";
      $("#vault-action-status").textContent = `Encrypted ${chain === "solana" ? "Solana" : "EVM"} wallet and authenticated ${short(generated.address)}. Transactions can be signed locally after you unlock the vault.${botStatus}`;
    } catch {
      $("#vault-action-status").textContent = `Encrypted ${chain === "solana" ? "Solana" : "EVM"} wallet created at ${short(generated.address)}. Connect this address through a wallet provider to authenticate and fund it.`;
    } finally {
      secret = "";
      generated.secret = "";
    }
    $("#new-wallet-password").value = "";
    $("#new-wallet-password-confirm").value = "";
    renderLocalVaultTools();
  }

  async function requestInternalWallet() {
    const localWallet = state.evm?.local || state.solana?.local;
    if (!localWallet) {
      $("#vault-action-status").textContent = "Connect or create a Coni local wallet first. External wallet connections use bot-only mode.";
      return;
    }
    try {
      const result = await provisionLocalBotWallet();
      $("#vault-action-status").textContent = result.message || "Internal wallet setup is awaiting the audited wallet provisioner.";
      loadBotWallet();
    } catch (error) {
      $("#vault-action-status").textContent = error?.message || "Connect and authenticate a wallet before creating the internal-wallet record.";
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
    const assetMetadata = fundingAssets.get(asset);
    const decimals = assetMetadata?.decimals;
    if (!Number.isInteger(decimals) || decimals < 0 || decimals > 36) throw new Error("Select a verified asset before requesting withdrawal");
    if (!/^\d+(\.\d+)?$/.test(amount)) throw new Error("enter a valid withdrawal amount");
    const amountBaseUnits = toBaseUnits(amount, decimals).toString();
    const destination = activeBotWallet.chain === "solana" ? state.solana?.address : state.evm?.address;
    if (!destination) throw new Error("Connect the wallet that owns this bot network before requesting withdrawal");
    const response = await fetch(`${config.apiBaseUrl}/api/bot-wallet/transfer`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ botWalletId: activeBotWallet.id, direction: "withdrawal", asset, amountBaseUnits, destination, idempotencyKey: `bot-withdrawal-${crypto.randomUUID()}` }) });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || result.message || "withdrawal request rejected");
    $("#funding-status").textContent = `Bot withdrawal reserved for signer approval: ${result.state}. Fee ${result.feeAmountBaseUnits || "0"} base units; total reserved ${result.totalReservedBaseUnits || result.amountBaseUnits}. No transaction has been broadcast.`;
    $("#bot-withdraw-form").hidden = true;
    loadBotWallet();
  }

  $("#connect-evm").addEventListener("click", (event) => runBusy(event.currentTarget, connectEvm));
  $("#connect-solana").addEventListener("click", (event) => runBusy(event.currentTarget, connectSolana));
  $("#open-fast-wallet")?.addEventListener("click", () => { $("#vault-modal").hidden = false; document.body.style.overflow = "hidden"; });
  $("#connection-list")?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-disconnect-wallet]");
    if (button) runBusy(button, () => disconnectWallet(button.dataset.disconnectWallet));
  });
  $("#evm-wallet-choice")?.addEventListener("change", (event) => {
    if (event.target.value) localStorage.setItem(EVM_PROVIDER_KEY, event.target.value);
  });
  $("#solana-wallet-choice")?.addEventListener("change", (event) => {
    if (event.target.value) localStorage.setItem(SOLANA_PROVIDER_KEY, event.target.value);
  });
  $("#bot-wallet-chain")?.addEventListener("change", (event) => selectBotWallet(event.target.value));
  $("#prepare-funding").addEventListener("click", (event) => runBusy(event.currentTarget, prepareFunding));
  $("#approve-funding").addEventListener("click", (event) => runBusy(event.currentTarget, () => approveFunding().catch((error) => { $("#funding-status").textContent = error?.message || "Wallet approval failed."; })));
  $("#claim-funding").addEventListener("click", (event) => runBusy(event.currentTarget, submitFundingClaim));
  $("#vault-setup").addEventListener("click", () => { $("#vault-modal").hidden = false; document.body.style.overflow = "hidden"; });
  $("#bot-topup").addEventListener("click", startBotTopup);
  $("#bot-withdraw").addEventListener("click", requestBotWithdrawal);
  $("#submit-bot-withdrawal").addEventListener("click", (event) => runBusy(event.currentTarget, () => submitBotWithdrawal().catch((error) => { $("#funding-status").textContent = error?.message || "Bot withdrawal could not be requested."; })));
  $("#import-local-wallet").addEventListener("click", (event) => runBusy(event.currentTarget, () => importLocalWallet().catch(() => { $("#vault-action-status").textContent = "Local encryption failed; no key was uploaded."; })));
  $("#verify-local-vault")?.addEventListener("click", (event) => runBusy(event.currentTarget, () => verifyLocalVault().catch(() => { $("#local-vault-status").textContent = "Local backup verification failed; no secret was uploaded."; })));
  $("#export-local-vault")?.addEventListener("click", exportLocalVault);
  $("#delete-local-vault")?.addEventListener("click", deleteLocalVault);
  $("#unlock-fast-wallet")?.addEventListener("click", (event) => runBusy(event.currentTarget, () => unlockFastWallet().catch((error) => { $("#vault-action-status").textContent = error?.message || "Fast wallet unlock failed; no key was sent to Coni."; })));
  $("#lock-fast-wallet")?.addEventListener("click", lockFastWallet);
  $("#create-local-wallet").addEventListener("click", (event) => runBusy(event.currentTarget, () => createLocalWallet().catch(() => { $("#vault-action-status").textContent = "Local wallet generation failed; no key was uploaded."; })));
  $("#request-internal-wallet").addEventListener("click", (event) => runBusy(event.currentTarget, requestInternalWallet));
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
  window.addEventListener("pagehide", clearBrowserSecrets);
  document.querySelectorAll("[data-surface]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-surface]").forEach((item) => item.classList.toggle("active", item === button));
    document.body.dataset.walletSurface = button.dataset.surface;
    renderBotWalletSummary();
  }));
  document.querySelectorAll("[data-wallet-mode]").forEach((button) => button.addEventListener("click", () => {
    document.querySelectorAll("[data-wallet-mode]").forEach((item) => item.classList.toggle("active", item === button));
    $("#existing-wallet-panel").hidden = button.dataset.walletMode !== "existing";
    $("#new-wallet-panel").hidden = button.dataset.walletMode !== "new";
  }));
  document.addEventListener("click", (event) => { if (event.target.matches("[data-close-modal]") || event.target.classList.contains("modal-backdrop")) { event.target.closest(".modal-backdrop").hidden = true; document.body.style.overflow = ""; } });
  renderProviderChoices();
  ensureRecoveryPanel();
  renderLocalVaultTools();
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
