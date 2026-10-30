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
  const state = { data: fallback, live: false, motion: null, history: null, bridgeActivity: null, metadata: new Map(), persistedOpportunities: [], activityLoadedAt: 0, quoteRequest: 0, previewRequest: 0, visibleOpportunityCount: 24 };
  let marketRefreshController = null;
  const $ = (selector) => document.querySelector(selector);
  const money = (value, digits = 2) => value == null || Number.isNaN(value) ? "—" : `$${Number(value).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })}`;
  const compact = (value) => value >= 1 ? `$${value.toFixed(2)}` : `$${value.toFixed(5)}`;
  const chainNames = { ethereum: "Ethereum", base: "Base", robinhood: "Robinhood", bsc: "BNB Chain", solana: "Solana" };
  const chainShort = { ethereum: "ETH", base: "BASE", robinhood: "RH", bsc: "BNB", solana: "SOL" };
  const nativeSymbol = { ethereum: "ETH", base: "ETH", robinhood: "ETH", bsc: "BNB", solana: "SOL" };
  const nativeDecimals = { ethereum: 18, base: 18, robinhood: 18, bsc: 18, solana: 9 };
  const escapeHtml = (value) => String(value).replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
  const numberValue = (value) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  };
  const marketCapFor = (price, launch) => {
    const explicit = numberValue(launch?.marketCapUsd || launch?.market_cap_usd || launch?.marketCap || launch?.market_cap);
    if (explicit != null && explicit > 0) return explicit;
    const capSupply = numberValue(launch?.capSupply || launch?.supply?.capSupply || launch?.supply?.total);
    const tokenPrice = numberValue(price);
    return capSupply != null && capSupply > 0 && tokenPrice != null && tokenPrice > 0 ? capSupply * tokenPrice : null;
  };
  const formatPrice = (value) => {
    const parsed = numberValue(value);
    if (parsed == null || parsed <= 0) return "—";
    return parsed >= 1 ? money(parsed, 2) : `$${parsed.toLocaleString(undefined, { maximumSignificantDigits: 7 })}`;
  };
  const formatMarketCap = (value) => {
    const parsed = numberValue(value);
    if (parsed == null || parsed <= 0) return "Not supplied";
    if (parsed >= 1e9) return `$${(parsed / 1e9).toFixed(2)}B`;
    if (parsed >= 1e6) return `$${(parsed / 1e6).toFixed(2)}M`;
    if (parsed >= 1e3) return `$${(parsed / 1e3).toFixed(1)}K`;
    return money(parsed, 2);
  };

  function logoUrl(image) {
    if (!image || typeof image !== "string") return "";
    if (/^https?:\/\//i.test(image)) return image;
    const match = image.match(/^ipfs:\/\/([^/]+)(?:\/.*)?$/i);
    return match ? `https://www.pangea.trade/api/logo/${encodeURIComponent(match[1])}/128.webp` : "";
  }

  function logoMarkup(item) {
    const candidate = logoUrl(item.image || item.logoURI || item.logoUrl);
    const fallback = escapeHtml(String(item.symbol || "TOKEN").slice(0, 4));
    return candidate ? `<img class="token-logo-image" src="${escapeHtml(candidate)}" alt="" loading="lazy" referrerpolicy="no-referrer" data-fallback="${fallback}" />` : fallback;
  }

  function normalize(launch) {
    const pools = (launch.pools || []).map((pool) => ({ ...pool, priceUsd: Number(pool.priceUsd), depth: Number(pool.depth || 0) })).filter((pool) => Number.isFinite(pool.priceUsd) && pool.priceUsd > 0);
    if (pools.length < 2) return null;
    const cheapest = pools.reduce((a, b) => a.priceUsd < b.priceUsd ? a : b);
    const dearest = pools.reduce((a, b) => a.priceUsd > b.priceUsd ? a : b);
    const gross = ((dearest.priceUsd - cheapest.priceUsd) / cheapest.priceUsd) * 100;
    const depth = Math.min(cheapest.depth || 0, dearest.depth || 0);
    const persisted = state.persistedOpportunities.find((row) => row.slug === launch.slug && row.buyChain === cheapest.chain && row.sellChain === dearest.chain);
    const decision = persisted?.decisionSummary || persisted?.decision || {};
    // A pool spread is not a profit forecast. Exact trade, bridge, gas, and
    // risk costs are requested only in the route preview/backend scanner.
    return { ...launch, cheapest: { ...cheapest, marketCapUsd: marketCapFor(cheapest.priceUsd, launch) }, dearest: { ...dearest, marketCapUsd: marketCapFor(dearest.priceUsd, launch) }, gross, net: gross, depth, marketCapUsd: marketCapFor(cheapest.priceUsd, launch), confidenceBps: Number(decision.confidenceBps), decision: decision.executable === true ? "take" : decision.reasons?.length ? "avoid" : "observed", quoteState: decision.quoteState || "not-requested", reasons: Array.isArray(decision.reasons) ? decision.reasons : [], netProfitUsd: numberValue(decision.netProfitUsd ?? persisted?.netProfitUsd), netBps: numberValue(decision.netBps ?? persisted?.netBps), persistedState: persisted?.state || "" };
  }

  function render() {
    const opportunities = state.data.launches.map(normalize).filter(Boolean).sort((a, b) => b.net - a.net);
    const visibleOpportunities = opportunities.slice(0, state.visibleOpportunityCount);
    const list = $("#opportunity-list");
    $("#opportunity-count").textContent = String(opportunities.length).padStart(2, "0");
    $("#nav-opportunity-count").textContent = String(opportunities.length).padStart(2, "0");
    $("#metric-routes").textContent = String(opportunities.length).padStart(2, "0");
    $("#metric-edge").textContent = opportunities[0] ? `${opportunities[0].gross.toFixed(2)}%` : "—";
    list.innerHTML = visibleOpportunities.map((item) => `
      <article class="opportunity-row">
        <button class="token-name token-detail-trigger" data-token-detail="${escapeHtml(item.slug)}" aria-label="Open live arbitrage detail for ${escapeHtml(item.symbol)}"><span class="token-symbol">${logoMarkup(item)}</span><span><strong>$${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.name)}</small></span></button>
        <div class="route"><strong>${chainShort[item.cheapest.chain]} <span>→</span> ${chainShort[item.dearest.chain]}</strong><small>${chainNames[item.cheapest.chain]} to ${chainNames[item.dearest.chain]} · ${compact(item.depth)} depth</small></div>
        <div class="edge"><strong>+${item.gross.toFixed(2)}%</strong><small>observed gap</small></div>
        <div class="confidence"><i></i>${Number.isFinite(item.netBps) && item.netBps > 0 ? `${(item.netBps / 100).toFixed(1)}% NET` : "OBSERVED"}<small>${item.decision === "take" ? "quote-cleared" : item.decision === "avoid" ? "policy rejected" : "quote pending"}</small></div>
        <button class="row-action" data-preview="${item.slug}">Preview</button>
      </article>`).join("");
    list.querySelectorAll("img[data-fallback]").forEach((image) => image.addEventListener("error", () => image.replaceWith(document.createTextNode(image.dataset.fallback || "TOKEN")), { once: true }));
    list.querySelectorAll("[data-preview]").forEach((button) => button.addEventListener("click", () => openPreview(opportunities.find((item) => item.slug === button.dataset.preview))));
    const loadMore = $("#load-more-opportunities");
    if (loadMore) {
      loadMore.hidden = visibleOpportunities.length >= opportunities.length;
      loadMore.textContent = visibleOpportunities.length >= opportunities.length ? "All live launches shown" : `Load more · ${opportunities.length - visibleOpportunities.length} remaining`;
    }
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
    const allTrades = Array.isArray(state.history?.recent)
      ? state.history.recent
      : (state.history?.launches || []).flatMap((launch) => (launch.recent || []).map((trade) => ({ ...trade, launchSymbol: launch.symbol || launch.slug })));
    const trades = allTrades.slice(0, 4).map((trade) => `<div class="activity-item"><span class="activity-kind trade">${String(trade.side || "trade").toUpperCase()}</span><div><strong>${escapeHtml(trade.launchSymbol || trade.quoteSymbol || "Quote")} · ${escapeHtml(chainShort[trade.chain] || trade.chain || "—")}</strong><small>${formatAge(trade.timestamp)} · ${shortHash(trade.txHash)}</small></div><b>${trade.valueUsd == null ? "—" : money(trade.valueUsd)}</b></div>`);
    const bridges = (state.bridgeActivity?.transfers || []).slice(0, 3).map((transfer) => `<div class="activity-item"><span class="activity-kind bridge">BRIDGE</span><div><strong>${escapeHtml(transfer.slug || "Pangea route")} · ${escapeHtml(chainShort[transfer.from] || transfer.from || "—")} → ${escapeHtml(chainShort[transfer.to] || transfer.to || "—")}</strong><small>${formatAge(transfer.at)} · ${transfer.complete ? "delivered" : "in flight"}</small></div><b>${transfer.amount == null ? "—" : escapeHtml(String(transfer.amount))}</b></div>`);
    const rows = [...trades, ...bridges].slice(0, 6);
    feed.innerHTML = rows.length ? rows.join("") : `<div class="activity-empty"><span class="activity-glyph">⌁</span><div><strong>No Pangea activity returned</strong><p>Recent trades and bridge transfers will appear when the live feed is available.</p></div></div>`;
  }

  function renderMotionDiscoveries() {
    const card = $("#motion-discovery");
    const list = $("#motion-discovery-list");
    const status = $("#motion-discovery-status");
    if (!card || !list || !status) return;
    const rows = Array.isArray(state.motion?.rows) ? state.motion.rows.slice(0, 9) : [];
    card.hidden = !state.motion?.enabled || rows.length === 0;
    if (card.hidden) return;
    status.textContent = state.motion.stale ? "STALE READ-ONLY" : "LIVE READ-ONLY";
    list.innerHTML = rows.map((row) => `<div class="motion-discovery-row"><div><strong>${escapeHtml(row.symbol || "TOKEN")}</strong><small>${escapeHtml(row.name || "")}</small></div><span>${escapeHtml(chainShort[row.chain] || row.chain || "—")}</span><b>${formatPrice(row.priceUsd)}</b><small>${row.mcapUsd ? `MC ${formatMarketCap(row.mcapUsd)}` : "Market cap unavailable"}</small><a href="${escapeHtml(row.url || "#")}" target="_blank" rel="noreferrer">Open ↗</a></div>`).join("");
  }

  async function loadMotionDiscoveries(requestOptions) {
    if (!config.motionMarketUrl) return;
    try {
      const response = await fetch(`${config.motionMarketUrl}?limit=3`, requestOptions);
      if (!response.ok) return;
      state.motion = await response.json();
      renderMotionDiscoveries();
    } catch { /* supplementary feed never blocks the Pangea dashboard */ }
  }

  async function loadActivity() {
    if (Date.now() - state.activityLoadedAt < 40_000) return;
    state.activityLoadedAt = Date.now();
    const [history, bridge] = await Promise.allSettled([
      fetch(`${config.marketHistoryUrl}?hours=24`, { headers: { Accept: "application/json" } }).then((response) => response.ok ? response.json() : null),
      fetch(`${config.bridgeActivityUrl}?hours=24&limit=25`, { headers: { Accept: "application/json" } }).then((response) => response.ok ? response.json() : null)
    ]);
    state.history = history.status === "fulfilled" ? history.value : null;
    state.bridgeActivity = bridge.status === "fulfilled" ? bridge.value : null;
    renderActivity();
    renderMotionDiscoveries();
  }

  async function loadMetadata(launches, signal) {
    const candidates = launches.slice(0, state.visibleOpportunityCount).filter((launch) => launch.slug && !state.metadata.has(launch.slug));
    await Promise.allSettled(candidates.map(async (launch) => {
      const response = await fetch(`${config.tokenMetadataUrl}?launch=${encodeURIComponent(launch.slug)}`, { headers: { Accept: "application/json" }, signal });
      if (!response.ok) throw new Error(`metadata ${response.status}`);
      state.metadata.set(launch.slug, await response.json());
    }));
  }

  async function load() {
    marketRefreshController?.abort();
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    marketRefreshController = controller;
    const requestOptions = { headers: { Accept: "application/json" }, ...(controller ? { signal: controller.signal } : {}) };
    try {
      const [response, registryResponse, opportunityResponse] = await Promise.all([
        fetch(config.marketDataUrl || config.pangeaPoolsUrl, requestOptions),
        fetch(config.tokenRegistryUrl, requestOptions),
        fetch(`${config.apiBaseUrl}/api/opportunities?limit=100`, requestOptions)
      ]);
      loadMotionDiscoveries(requestOptions);
      if (!response.ok) throw new Error(`Pangea responded ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.launches)) throw new Error("Unexpected Pangea payload");
      const selected = data.launches.filter((launch) => launch && launch.slug);
      if (selected.length) {
        state.persistedOpportunities = opportunityResponse.ok ? ((await opportunityResponse.json()).opportunities || []) : [];
        const registry = registryResponse.ok ? await registryResponse.json() : { tokens: [] };
        const bySlug = new Map((registry.tokens || []).map((item) => [item.slug, item]));
        const liveLaunches = selected.map((launch) => ({ ...launch, ...(bySlug.get(launch.slug) || {}) }));
        // Pool data is useful immediately; token metadata is cosmetic and
        // hydrates after the first live render.
        state.data = { ...data, launches: liveLaunches };
        state.live = true;
        render();
        await loadMetadata(selected, controller?.signal);
        if (!controller?.signal.aborted) {
          state.data = { ...state.data, launches: liveLaunches.map((launch) => ({ ...launch, image: launch.image || state.metadata.get(launch.slug)?.image })) };
          render();
        }
      }
    } catch (error) {
      if (error?.name === "AbortError") return;
      state.live = false;
      render();
    } finally {
      if (marketRefreshController === controller) marketRefreshController = null;
    }
    if (!controller?.signal.aborted) {
      const runActivity = () => loadActivity();
      if (typeof window.requestIdleCallback === "function") window.requestIdleCallback(runActivity, { timeout: 1200 });
      else window.setTimeout(runActivity, 0);
    }
  }

  function openModal(id) { $(id).hidden = false; document.body.style.overflow = "hidden"; }
  function closeModal(target) { target.closest(".modal-backdrop").hidden = true; document.body.style.overflow = ""; }
  async function openPreview(item) {
    if (!item) return;
    const previewRequest = ++state.previewRequest;
    let persisted = state.persistedOpportunities.find((row) => row.slug === item.slug && row.buyChain === item.cheapest.chain && row.sellChain === item.dearest.chain);
    const snapshotTime = item.readAt || state.data.readAt || state.data.asOf;
    const routePools = [item.cheapest, item.dearest].filter((pool, index, pools) => pools.findIndex((candidate) => candidate.chain === pool.chain) === index);
    const decision = persisted?.decisionSummary || persisted?.decision || {};
    const launchMarketCap = item.marketCapUsd || marketCapFor(item.cheapest.priceUsd, item);
    const inputUsd = 10;
    const estimatedGross = inputUsd * (item.dearest.priceUsd / item.cheapest.priceUsd - 1);
    const lastNet = numberValue(decision.netProfitUsd ?? persisted?.netProfitUsd);
    const persistedNotional = numberValue(persisted?.notionalUsd) || inputUsd;
    const lastNetLabel = lastNet != null && persistedNotional === inputUsd ? money(lastNet, 4) : "Pending exact quote";
    $("#preview-token-logo").innerHTML = logoMarkup(item);
    $("#preview-title").textContent = `$${item.symbol} route detail`;
    $("#preview-token-subtitle").textContent = `${item.name} · live Pangea pool snapshot`;
    $("#preview-route").innerHTML = `<strong>Buy ${chainNames[item.cheapest.chain]}</strong> <span>→</span> <strong>Bridge</strong> <span>→</span> <strong>Sell ${chainNames[item.dearest.chain]}</strong><br><small>Cheapest observed pool to the dearer observed pool. The bridge leg is only quoted when the chains differ.</small>`;
    $("#preview-stats").innerHTML = [["Trade size", money(inputUsd)], ["Net estimate", lastNetLabel], ["Observed gross spread", `+${money(estimatedGross, 4)}`], ["Observed gap", `+${item.gross.toFixed(2)}%`], ["Quote state", escapeHtml(decision.quoteState || "pending")], ["Route depth", compact(item.depth)]].map(([label, value]) => `<div class="preview-stat"><small>${label}</small><strong>${value}</strong></div>`).join("");
    $("#preview-chains").innerHTML = routePools.map((pool) => {
      const poolMarketCap = pool.marketCapUsd || marketCapFor(pool.priceUsd, item) || launchMarketCap;
      const role = pool.chain === item.cheapest.chain ? "BUY · cheaper pool" : "SELL · dearer pool";
      const poolDepth = numberValue(pool.depth || pool.depthUsd || pool.liquidityUsd);
      return `<div class="preview-chain-row"><div><span class="chain-dot ${escapeHtml(pool.chain)}"></span><strong>${escapeHtml(chainNames[pool.chain] || pool.chain)}</strong><small>${role}</small></div><div><small>Price</small><strong>${formatPrice(pool.priceUsd)}</strong></div><div><small>Market cap</small><strong>${formatMarketCap(poolMarketCap)}</strong></div><div><small>Depth</small><strong>${poolDepth == null ? "—" : compact(poolDepth)}</strong></div></div>`;
    }).join("");
    $("#preview-read-at").textContent = snapshotTime ? `Snapshot ${formatAge(new Date(snapshotTime).getTime() / 1000)}` : "Snapshot time unavailable";
    $("#preview-quote").textContent = "Requesting fresh read-only Pangea quotes for $10…";
    const prepare = $("#prepare-route");
    prepare.disabled = !persisted || decision.executable !== true;
    prepare.dataset.opportunityId = persisted?.id || "";
    prepare.textContent = !persisted ? "Run scanner worker first" : decision.executable === true ? "Prepare bot route →" : "Waiting for quote clearance";
    openModal("#preview-modal");
    if (!persisted) {
      try {
        const query = new URLSearchParams({ limit: "1", slug: item.slug, buyChain: item.cheapest.chain, sellChain: item.dearest.chain });
        const response = await fetch(`${config.apiBaseUrl}/api/opportunities?${query}`, { headers: { Accept: "application/json" }, cache: "no-store" });
        if (response.ok && previewRequest === state.previewRequest) {
          const rows = (await response.json()).opportunities || [];
          persisted = rows[0] || null;
          if (persisted) {
            state.persistedOpportunities.push(persisted);
            const freshDecision = persisted.decisionSummary || persisted.decision || {};
            prepare.disabled = freshDecision.executable !== true;
            prepare.dataset.opportunityId = persisted.id;
            prepare.textContent = freshDecision.executable === true ? "Prepare bot route →" : "Waiting for quote clearance";
            const lastNet = numberValue(freshDecision.netProfitUsd ?? persisted.netProfitUsd);
            if (lastNet != null) $("#preview-stats").querySelectorAll(".preview-stat")[1].innerHTML = `<small>Net estimate</small><strong>${money(lastNet, 4)}</strong>`;
            $("#preview-stats").querySelectorAll(".preview-stat")[4].innerHTML = `<small>Quote state</small><strong>${escapeHtml(freshDecision.quoteState || "pending")}</strong>`;
          }
        }
      } catch { /* the route remains a read-only preview when persistence is unavailable */ }
    }
    loadRouteQuotes(item);
  }

  function quoteAmount(item) {
    const usd = 10;
    const symbol = nativeSymbol[item.cheapest.chain];
    const rate = Number(state.data.rates?.usd?.[symbol]);
    if (!rate || !item.cheapest.priceUsd) return null;
    const tokenDecimals = Number(item.cheapest.tokenDecimals ?? item.cheapest.decimals ?? item.decimals ?? config.token.decimals ?? 9);
    return {
      native: String(Math.floor((usd / rate) * (10 ** nativeDecimals[item.cheapest.chain]))),
      tokens: String(Math.floor((usd / item.cheapest.priceUsd) * (10 ** tokenDecimals)))
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
    const scannerNet = numberValue(item.netProfitUsd);
    const netLabel = scannerNet != null ? `scanner net estimate ${money(scannerNet, 4)}` : "net estimate pending scanner quote";
    $("#preview-quote").textContent = `Pangea read-only quote · $10 notional · ${labels.join(" · ")} · ${netLabel} · execution remains signer-gated`;
  }

  async function prepareRoute(button) {
    const opportunityId = button?.dataset.opportunityId;
    if (!opportunityId) return;
    button.disabled = true;
    button.textContent = "Checking execution gate…";
    try {
      const response = await fetch(`${config.apiBaseUrl}/api/execution/plan`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ opportunityId })
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || result.reason || "Execution remains gated");
      $("#preview-quote").textContent = `Execution ${result.execution?.state || "planned"}. The signer must still validate and approve each exact leg.`;
    } catch (error) {
      $("#preview-quote").textContent = error?.message || "Execution remains gated until wallet authentication and signer approval are complete.";
    } finally {
      button.disabled = false;
      button.textContent = "Prepare bot route →";
    }
  }

  document.addEventListener("click", (event) => {
    if (event.target.matches("[data-close-modal]")) closeModal(event.target);
    if (event.target.classList.contains("modal-backdrop")) closeModal(event.target);
    if (event.target.closest("#wallet-button, #vault-connect")) openModal("#wallet-modal");
    const provider = event.target.closest("[data-connect-provider]");
    if (provider) {
      closeModal(provider);
      const chain = provider.dataset.connectChain;
      if (chain) window.location.href = `wallet.html?connect=${encodeURIComponent(chain)}`;
    }
    if (event.target.closest("#prepare-route")) prepareRoute(event.target.closest("#prepare-route"));
    const tokenTrigger = event.target.closest("[data-token-detail]");
    if (tokenTrigger) {
      const item = state.data.launches.map(normalize).find((candidate) => candidate?.slug === tokenTrigger.dataset.tokenDetail);
      openPreview(item);
    }
  });
  $("#refresh-button").addEventListener("click", load);
  $("#load-more-opportunities")?.addEventListener("click", async () => {
    state.visibleOpportunityCount += 24;
    await loadMetadata(state.data.launches || []);
    state.data = { ...state.data, launches: state.data.launches.map((launch) => ({ ...launch, image: launch.image || state.metadata.get(launch.slug)?.image })) };
    render();
  });
  render();
  load();
  setInterval(load, config.refreshMs);
})();
