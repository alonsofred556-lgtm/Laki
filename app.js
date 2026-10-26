(() => {
  const config = window.CONI_CONFIG;
  const fallback = {
    rates: { usd: { ETH: 2310.85, BNB: 738.57, SOL: 100.75 }, asOf: new Date().toISOString() },
    launches: [
      { slug: "zgap", name: "Zero Gap", symbol: "ZGAP", pools: [
        { chain: "ethereum", priceUsd: 0.00009123, depth: 35976.69 }, { chain: "base", priceUsd: 0.00008816, depth: 35976.69 }, { chain: "robinhood", priceUsd: 0.00008881, depth: 35976.69 }, { chain: "bsc", priceUsd: 0.00008838, depth: 63805.70 }, { chain: "solana", priceUsd: 0.00008760, depth: 174072.43 }
      ] },
      { slug: "pangea-dadd0d", name: "Pangea", symbol: "PANGEA", pools: [
        { chain: "ethereum", priceUsd: 0.00211015, depth: 34704.63 }, { chain: "base", priceUsd: 0.00210283, depth: 34704.63 }, { chain: "robinhood", priceUsd: 0.00211586, depth: 34704.63 }, { chain: "bsc", priceUsd: 0.00210622, depth: 63424.03 }, { chain: "solana", priceUsd: 0.00214760, depth: 167436.78 }
      ] },
      { slug: "kimi", name: "KIMI", symbol: "KIMI", pools: [
        { chain: "ethereum", priceUsd: 0.00000301, depth: 7216.96 }, { chain: "base", priceUsd: 0.00000301, depth: 7216.96 }, { chain: "robinhood", priceUsd: 0.00000301, depth: 7216.96 }, { chain: "bsc", priceUsd: 0.00000327, depth: 12761.14 }, { chain: "solana", priceUsd: 0.00000300, depth: 34484.12 }
      ] }
    ]
  };
  const state = { data: fallback, live: false, history: null, bridgeActivity: null, metadata: new Map(), activityLoadedAt: 0, quoteRequest: 0 };
  const $ = (selector) => document.querySelector(selector);
  const money = (value, digits = 2) => value == null || Number.isNaN(value) ? "—" : `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
  const compact = (value) => value >= 1 ? `$${value.toFixed(2)}` : `$${value.toFixed(5)}`;
  const chainNames = { ethereum: "Ethereum", base: "Base", robinhood: "Robinhood", bsc: "BNB Chain", solana: "Solana" };
  const chainShort = { ethereum: "ETH", base: "BASE", robinhood: "RH", bsc: "BNB", solana: "SOL" };
  const nativeSymbol = { ethereum: "ETH", base: "ETH", robinhood: "ETH", bsc: "BNB", solana: "SOL" };
  const nativeDecimals = { ethereum: 18, base: 18, robinhood: 18, bsc: 18, solana: 9 };
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));

  function logoUrl(image) {
    if (!image || typeof image !== "string") return "";
    if (/^https?:\/\//i.test(image)) return image;
    const match = image.match(/^ipfs:\/\/([^/]+)(?:\/.*)?$/i);
    return match ? `https://www.pangea.trade/api/logo/${encodeURIComponent(match[1])}/128.webp` : "";
  }

  function logoMarkup(item) {
    const candidate = logoUrl(item.image || item.logoURI || item.logoUrl);
    const fallback = escapeHtml(String(item.symbol || "TOKEN").slice(0, 4));
    return candidate ? `<img class="token-logo-image" src="${candidate}" alt="" loading="lazy" referrerpolicy="no-referrer" data-fallback="${fallback}" />` : fallback;
  }

  function normalize(launch) {
    const pools = (launch.pools || []).map((pool) => ({ ...pool, priceUsd: Number(pool.priceUsd), depth: Number(pool.depth || 0) })).filter((pool) => Number.isFinite(pool.priceUsd) && pool.priceUsd > 0);
    if (pools.length < 2) return null;
    const cheapest = pools.reduce((a, b) => a.priceUsd < b.priceUsd ? a : b);
    const dearest = pools.reduce((a, b) => a.priceUsd > b.priceUsd ? a : b);
    const gross = ((dearest.priceUsd - cheapest.priceUsd) / cheapest.priceUsd) * 100;
    const depth = Math.min(cheapest.depth || 0, dearest.depth || 0);
    const costBuffer = launch.slug === "zgap" ? 0.42 : 0.55;
    const net = Math.max(0, gross - costBuffer);
    return { ...launch, cheapest, dearest, gross, net, depth, confidence: Math.min(98, Math.round(72 + Math.min(gross * 5, 24))) };
  }

  function render() {
    const opportunities = state.data.launches.map(normalize).filter(Boolean).sort((a, b) => b.net - a.net);
    const visibleOpportunities = opportunities.slice(0, 24);
    const list = $("#opportunity-list");
    $("#opportunity-count").textContent = String(opportunities.length).padStart(2, "0");
    $("#nav-opportunity-count").textContent = String(opportunities.length).padStart(2, "0");
    $("#metric-routes").textContent = String(opportunities.length).padStart(2, "0");
    $("#metric-edge").textContent = opportunities[0] ? `${opportunities[0].net.toFixed(2)}%` : "—";
    list.innerHTML = visibleOpportunities.map((item) => `
      <article class="opportunity-row">
        <div class="token-name"><span class="token-symbol">${logoMarkup(item)}</span><div><strong>$${item.symbol}</strong><small>${item.name}</small></div></div>
        <div class="route"><strong>${chainShort[item.cheapest.chain]} <span>→</span> ${chainShort[item.dearest.chain]}</strong><small>${chainNames[item.cheapest.chain]} to ${chainNames[item.dearest.chain]} · ${compact(item.depth)} depth</small></div>
        <div class="edge"><strong>+${item.net.toFixed(2)}%</strong><small>net est. edge</small></div>
        <div class="confidence"><i></i>${item.confidence}%<small> confidence</small></div>
        <button class="row-action" data-preview="${item.slug}">Preview</button>
      </article>`).join("");
    list.querySelectorAll("img[data-fallback]").forEach((image) => image.addEventListener("error", () => image.replaceWith(document.createTextNode(image.dataset.fallback || "TOKEN")), { once: true }));
    list.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => openPreview(opportunities.find((item) => item.slug === button.dataset.preview))));
    $("#last-sync").textContent = `${state.live ? "Live" : "Fallback"} · ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
    $("#feed-label").textContent = state.live ? "Pangea live feed" : "Pangea fallback snapshot";
    document.querySelector(".scanner-state strong").textContent = state.live ? "Scanner live" : "Scanner ready";
    renderActivity();
  }

  function formatAge(timestamp) {
    if (!timestamp) return "time unavailable";
    const date = new Date(Number(timestamp) * 1000);
    return Number.isNaN(date.getTime()) ? "time unavailable" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function shortHash(value) {
    return typeof value === "string" && value.length > 12 ? `${value.slice(0, 6)}…${value.slice(-4)}` : value || "—";
  }

  function renderActivity() {
    const feed = $("#activity-feed");
    if (!feed) return;
    const trades = (state.history?.recent || []).slice(0, 4).map((trade) => `<div class="activity-item"><span class="activity-kind trade">${String(trade.side || "trade").toUpperCase()}</span><div><strong>${escapeHtml(trade.quoteSymbol || "Quote")} · ${escapeHtml(chainShort[trade.chain] || trade.chain || "—")}</strong><small>${formatAge(trade.timestamp)} · ${shortHash(trade.txHash)}</small></div><b>${trade.valueUsd == null ? "—" : money(trade.valueUsd)}</b></div>`);
    const bridges = (state.bridgeActivity?.transfers || []).slice(0, 3).map((transfer) => `<div class="activity-item"><span class="activity-kind bridge">BRIDGE</span><div><strong>${escapeHtml(chainShort[transfer.from] || transfer.from || "—")} → ${escapeHtml(chainShort[transfer.to] || transfer.to || "—")}</strong><small>${formatAge(transfer.at)} · ${transfer.complete ? "delivered" : "in flight"}</small></div><b>${transfer.amount == null ? "—" : escapeHtml(String(transfer.amount))}</b></div>`);
    const rows = [...trades, ...bridges].slice(0, 6);
    feed.innerHTML = rows.length ? rows.join("") : `<div class="activity-empty"><span class="activity-glyph">⌁</span><div><strong>No Pangea activity returned</strong><p>Recent trades and bridge transfers will appear when the live feed is available.</p></div></div>`;
  }

  async function loadActivity() {
    if (Date.now() - state.activityLoadedAt < 40_000) return;
    state.activityLoadedAt = Date.now();
    const slug = config.token.slug;
    const [history, bridge] = await Promise.allSettled([
      fetch(`${config.marketHistoryUrl}?launch=${encodeURIComponent(slug)}&hours=24`, { headers: { Accept: "application/json" } }).then((response) => response.ok ? response.json() : null),
      fetch(`${config.bridgeActivityUrl}?token=${encodeURIComponent(slug)}&hours=24&limit=25`, { headers: { Accept: "application/json" } }).then((response) => response.ok ? response.json() : null)
    ]);
    state.history = history.status === "fulfilled" ? history.value : null;
    state.bridgeActivity = bridge.status === "fulfilled" ? bridge.value : null;
    renderActivity();
  }

  async function loadMetadata(launches) {
    const candidates = launches.slice(0, 24).filter((launch) => launch.slug && !state.metadata.has(launch.slug));
    await Promise.allSettled(candidates.map(async (launch) => {
      const response = await fetch(`${config.tokenMetadataUrl}?launch=${encodeURIComponent(launch.slug)}`, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`metadata ${response.status}`);
      state.metadata.set(launch.slug, await response.json());
    }));
  }

  async function load() {
    try {
      const response = await fetch(config.marketDataUrl || config.pangeaPoolsUrl, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(`Pangea responded ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.launches)) throw new Error("Unexpected Pangea payload");
      const selected = data.launches.filter((launch) => launch && launch.slug);
      if (selected.length) {
        await loadMetadata(selected);
        state.data = { ...data, launches: selected.map((launch) => ({ ...launch, image: launch.image || state.metadata.get(launch.slug)?.image })) };
        state.live = true;
      }
    } catch (error) {
      state.live = false;
    }
    render();
    loadActivity();
  }

  function openModal(id) { $(id).hidden = false; document.body.style.overflow = "hidden"; }
  function closeModal(target) { target.closest(".modal-backdrop").hidden = true; document.body.style.overflow = ""; }
  function openPreview(item) {
    if (!item) return;
    $("#preview-title").textContent = `$${item.symbol} route preview`;
    $("#preview-route").innerHTML = `<strong>${chainNames[item.cheapest.chain]}</strong> <span>→</span> <strong>${chainNames[item.dearest.chain]}</strong><br><small>Buy on the cheaper pool, bridge only if required, then sell on the dearer pool.</small>`;
    $("#preview-stats").innerHTML = [["Observed spread", `+${item.gross.toFixed(2)}%`], ["Net estimate", `+${item.net.toFixed(2)}%`], ["Min. route depth", compact(item.depth)], ["Mode", "Read-only quotes"]].map(([label, value]) => `<div class="preview-stat"><small>${label}</small><strong>${value}</strong></div>`).join("");
    $("#preview-quote").textContent = "Requesting read-only Pangea trade and bridge quotes…";
    openModal("#preview-modal");
    loadRouteQuotes(item);
  }

  function quoteAmount(item) {
    const usd = 25;
    const symbol = nativeSymbol[item.cheapest.chain];
    const rate = Number(state.data.rates?.usd?.[symbol]);
    if (!rate || !item.cheapest.priceUsd) return null;
    return {
      native: String(Math.floor((usd / rate) * (10 ** nativeDecimals[item.cheapest.chain]))),
      tokens: String(Math.floor((usd / item.cheapest.priceUsd) * 1e9))
    };
  }

  async function loadRouteQuotes(item) {
    const requestId = ++state.quoteRequest;
    const amounts = quoteAmount(item);
    if (!amounts) { $("#preview-quote").textContent = "Live quote unavailable: no native/USD rate was returned."; return; }
    const tradeBodies = [
      { slug: item.slug, side: "buy", chain: item.cheapest.chain, amount: amounts.native, slippageBps: 100 },
      { slug: item.slug, side: "sell", chain: item.dearest.chain, amount: amounts.tokens, slippageBps: 100 }
    ];
    const bridgeBody = { slug: item.slug, from: item.cheapest.chain, to: item.dearest.chain, amount: amounts.tokens, slippageBps: 100 };
    const [buy, sell, bridge] = await Promise.allSettled([
      fetch(config.tradeQuoteUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(tradeBodies[0]) }).then((response) => response.ok ? response.json() : null),
      fetch(config.tradeQuoteUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(tradeBodies[1]) }).then((response) => response.ok ? response.json() : null),
      item.cheapest.chain === item.dearest.chain ? Promise.resolve({ skipped: true }) : fetch(config.bridgeQuoteUrl, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(bridgeBody) }).then((response) => response.ok ? response.json() : null)
    ]);
    if (requestId !== state.quoteRequest) return;
    const usable = (result) => result.status === "fulfilled" && result.value && !result.value.unroutable;
    const labels = [`buy ${usable(buy) ? "priced" : "unavailable"}`, `sell ${usable(sell) ? "priced" : "unavailable"}`];
    if (!bridge.value?.skipped) labels.push(`bridge ${usable(bridge) ? "priced" : "unavailable"}`);
    $("#preview-quote").textContent = `Pangea read-only quote · $25 notional · ${labels.join(" · ")}`;
  }

  document.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-modal]")) closeModal(event.target);
    if (event.target.classList.contains("modal-backdrop")) closeModal(event.target);
    if (event.target.closest("#wallet-button, #vault-connect")) openModal("#wallet-modal");
    const provider = event.target.closest("[data-connect-provider]");
    if (provider) { $("#wallet-label").textContent = "Connected wallet"; closeModal(provider); }
  });
  $("#refresh-button").addEventListener("click", load);
  render();
  load();
  setInterval(load, config.refreshMs);
})();
