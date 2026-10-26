(() => {
  const fallback = [{ symbol: "ZGAP", name: "Zero Gap", net: 3.06, from: "BASE", to: "SOL" }, { symbol: "PANGEA", name: "Pangea", net: 1.55, from: "BASE", to: "SOL" }, { symbol: "KIMI", name: "KIMI", net: 7.10, from: "SOL", to: "BNB" }];
  const list = document.querySelector("#popup-list");
  const edge = document.querySelector("#popup-edge");
  const route = document.querySelector("#popup-route");
  const short = { ethereum: "ETH", base: "BASE", robinhood: "RH", bsc: "BNB", solana: "SOL" };
  const normalize = (launch) => { const pools = (launch.pools || []).filter((p) => Number.isFinite(p.priceUsd)); if (pools.length < 2) return null; const lo = pools.reduce((a,b) => a.priceUsd < b.priceUsd ? a : b); const hi = pools.reduce((a,b) => a.priceUsd > b.priceUsd ? a : b); const gross = ((hi.priceUsd-lo.priceUsd)/lo.priceUsd)*100; return { symbol: launch.symbol, name: launch.name, net: Math.max(0,gross-.42), from: short[lo.chain], to: short[hi.chain] }; };
  function render(items) { edge.textContent = items[0] ? `+${items[0].net.toFixed(2)}%` : "—"; route.textContent = items[0] ? `${items[0].from} → ${items[0].to} · estimated net edge` : "No route available"; list.innerHTML = items.map((item) => `<div class="extension-row"><span class="token-symbol">${item.symbol.slice(0,4)}</span><div><strong>$${item.symbol}</strong><small>${item.from} → ${item.to}</small></div><b>+${item.net.toFixed(2)}%</b></div>`).join(""); }
  async function load() { try { const response = await fetch(window.CONI_CONFIG.pangeaPoolsUrl); if (!response.ok) throw new Error(); const data = await response.json(); const items = data.launches.filter((launch) => ["zgap", "pangea-dadd0d", "kimi"].includes(launch.slug)).map(normalize).filter(Boolean).sort((a,b)=>b.net-a.net); render(items.length ? items : fallback); } catch { render(fallback); } }
  document.querySelector("#popup-refresh").addEventListener("click", load);
  document.querySelector("#open-dashboard").addEventListener("click", () => chrome.tabs.create({ url: window.CONI_CONFIG.deploymentUrl }));
  load();
})();
