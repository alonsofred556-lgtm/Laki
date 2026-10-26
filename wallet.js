(() => {
  const config = window.CONI_CONFIG;
  const state = { evm: null, solana: null };
  const STORAGE_KEY = "coni.wallet.connections.v1";
  const $ = (selector) => document.querySelector(selector);
  const short = (address) => address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "—";
  const chainNames = { 1: "Ethereum", 8453: "Base", 4663: "Robinhood Chain", 56: "BNB Chain" };

  function message(text, tone = "") {
    const node = $("#wallet-message");
    node.textContent = text;
    node.dataset.tone = tone;
  }

  function providerForEvm() {
    const providers = Array.isArray(window.ethereum?.providers) ? window.ethereum.providers : [window.ethereum].filter(Boolean);
    return window.okxwallet?.ethereum || window.safepalProvider || window.phantom?.ethereum || providers.find((provider) => provider.isOkxWallet) || providers.find((provider) => provider.isSafePal) || providers[0] || null;
  }

  function providerForSolana() {
    return window.phantom?.solana || window.okxwallet?.solana || window.safepalwallet?.solana || window.solflare || window.backpack?.solana || window.solana || null;
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
      state.evm = { provider, address, chainId, chainName: chainNames[chainId] || `Chain ${chainId}`, balance, providerName: providerLabel(provider), authenticated: true };
      saveConnections();
      renderConnections();
      message(`${state.evm.chainName} wallet connected.`, "success");
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
      state.solana = { provider, address, balance, providerName: providerLabel(provider, true), authenticated: true };
      saveConnections();
      renderConnections();
      message("Solana wallet connected.", "success");
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
    if ((!state.evm || state.evm.restored) && (!state.solana || state.solana.restored)) { $("#funding-status").textContent = "Reconnect the wallet that will fund the bot so the approval can be tied to an active provider."; return; }
    if (!/^\d+(\.\d{1,18})?$/.test(amount) || Number(amount) <= 0) { $("#funding-status").textContent = "Enter a positive amount with no more than 18 decimal places."; return; }
    $("#funding-status").textContent = "Asking the secure backend for a whitelisted funding intent…";
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/intent`, { method: "POST", credentials: "include", headers: { "content-type": "application/json" }, body: JSON.stringify({ asset, amount, wallet: asset === "SOL" ? state.solana?.address : state.evm?.address }) });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Funding intent rejected");
      $("#funding-status").textContent = `Intent ${result.intentId} is ready. Review the destination and approve in your wallet; Coni will not auto-submit it.`;
    } catch {
      $("#funding-status").textContent = "No funding intent was created. The secure backend destination is not configured in this build, so no funds can move.";
    }
  }

  $("#connect-evm").addEventListener("click", connectEvm);
  $("#connect-solana").addEventListener("click", connectSolana);
  $("#prepare-funding").addEventListener("click", prepareFunding);
  $("#vault-setup").addEventListener("click", () => { $("#vault-modal").hidden = false; document.body.style.overflow = "hidden"; });
  document.addEventListener("click", (event) => { if (event.target.matches("[data-close-modal]") || event.target.classList.contains("modal-backdrop")) { event.target.closest(".modal-backdrop").hidden = true; document.body.style.overflow = ""; } });
  restoreConnectionLabels();
  renderConnections();
  loadTokenLogo();
})();
