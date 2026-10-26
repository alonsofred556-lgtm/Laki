(() => {
  const fallback = [{ symbol: "ZGAP", name: "Zero Gap", gross: 0, from: "—", to: "—" }];
  const list = document.querySelector("#popup-list");
  const edge = document.querySelector("#popup-edge");
  const route = document.querySelector("#popup-route");
  const short = { ethereum: "ETH", base: "BASE", robinhood: "RH", bsc: "BNB", solana: "SOL" };
  const logoUrl = (image) => { if (!image || typeof image !== "string") return ""; if (/^https?:\/\//i.test(image)) return image; const match = image.match(/^ipfs:\/\/([^/]+)/i); return match ? `https://www.pangea.trade/api/logo/${encodeURIComponent(match[1])}/128.webp` : ""; };
  const esc = (value) => String(value || "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const normalize = (launch) => { const pools = (launch.pools || []).filter((p) => Number.isFinite(Number(p.priceUsd)) && Number(p.priceUsd) > 0); if (pools.length < 2) return null; const lo = pools.reduce((a,b) => a.priceUsd < b.priceUsd ? a : b); const hi = pools.reduce((a,b) => a.priceUsd > b.priceUsd ? a : b); const gross = ((hi.priceUsd-lo.priceUsd)/lo.priceUsd)*100; return { symbol: launch.symbol || launch.slug, name: launch.name || launch.slug, image: logoUrl(launch.image), gross, from: short[lo.chain] || lo.chain, to: short[hi.chain] || hi.chain }; };
  function render(items) { edge.textContent = items[0] ? `+${items[0].gross.toFixed(2)}%` : "—"; route.textContent = items[0] ? `${items[0].from} → ${items[0].to} · observed gap` : "No route available"; list.innerHTML = items.map((item) => `<div class="extension-row"><span class="token-symbol">${item.image ? `<img class="token-logo-image" src="${esc(item.image)}" alt="" />` : esc(item.symbol.slice(0,4))}</span><div><strong>$${esc(item.symbol)}</strong><small>${esc(item.from)} → ${esc(item.to)}</small></div><b>+${item.gross.toFixed(2)}%</b></div>`).join(""); }
  async function load() {
    try {
      const [response, registryResponse] = await Promise.all([
        fetch(window.CONI_CONFIG.marketDataUrl, { headers: { Accept: "application/json" } }),
        fetch(window.CONI_CONFIG.tokenRegistryUrl, { headers: { Accept: "application/json" } })
      ]);
      if (!response.ok) throw new Error();
      const data = await response.json();
      const registry = registryResponse.ok ? await registryResponse.json() : { tokens: [] };
      const bySlug = new Map((registry.tokens || []).map((item) => [item.slug, item]));
      const items = (data.launches || []).map((launch) => normalize({ ...launch, ...(bySlug.get(launch.slug) || {}), image: launch.image || bySlug.get(launch.slug)?.image })).filter(Boolean).sort((a,b)=>b.gross-a.gross).slice(0, 12);
      render(items.length ? items : fallback);
    } catch { render(fallback); }
  }
  document.querySelector("#popup-refresh").addEventListener("click", load);
  document.querySelector("#open-dashboard").addEventListener("click", () => chrome.tabs.create({ url: window.CONI_CONFIG.deploymentUrl }));
  load();
})();
