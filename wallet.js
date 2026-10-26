(() => {
  const config = window.CONI_CONFIG;
  const state = { evm: null, solana: null };
  const $ = (selector) => document.querySelector(selector);
  const short = (address) => address ? `${address.slice(0, 6)}…${address.slice(-4)}` : "—";
  const chainNames = { 1: "Ethereum", 8453: "Base", 4663: "Robinhood Chain", 56: "BNB Chain" };

  function message(text, tone = "") {
    const node = $("#wallet-message");
    node.textContent = text;
    node.dataset.tone = tone;
  }

  function providerForSolana() {
    return window.phantom?.solana || window.solflare || window.backpack?.solana || window.solana || null;
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
    const provider = window.ethereum;
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
      state.evm = { provider, address, chainId, chainName: chainNames[chainId] || `Chain ${chainId}`, balance };
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
      state.solana = { provider, address, balance };
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
      const isSolana = Boolean(item.provider === state.solana?.provider);
      return `<div class="connection-row"><span class="provider-icon ${isSolana ? "sol" : "eth"}">${isSolana ? "S" : "E"}</span><div><strong>${isSolana ? "Solana wallet" : "EVM wallet"}</strong><small>${short(item.address)} · ${isSolana ? "Solana" : item.chainName}</small></div><span class="connection-balance">${item.balance}</span><span class="connected-dot">●</span></div>`;
    }).join("");
    if (state.evm) $("#eth-balance").textContent = state.evm.balance;
    if (state.solana) $("#sol-balance").textContent = state.solana.balance;
  }

  async function prepareFunding() {
    const asset = $("#funding-asset").value;
    const amount = $("#funding-amount").value.trim();
    if (!state.evm && !state.solana) { $("#funding-status").textContent = "Connect the wallet that will fund the bot before preparing a request."; return; }
    if (!/^\d+(\.\d{1,18})?$/.test(amount) || Number(amount) <= 0) { $("#funding-status").textContent = "Enter a positive amount with no more than 18 decimal places."; return; }
    $("#funding-status").textContent = "Asking the secure backend for a whitelisted funding intent…";
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/funding/intent`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ asset, amount, wallet: asset === "SOL" ? state.solana?.address : state.evm?.address }) });
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
  renderConnections();
  loadTokenLogo();
})();
