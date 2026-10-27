const CONI_DEPLOYMENT_URL = "https://laki-production.up.railway.app";
window.CONI_CONFIG = {
  deploymentUrl: CONI_DEPLOYMENT_URL,
  apiBaseUrl: window.location.hostname === "laki-production.up.railway.app" ? window.location.origin : "",
  pangeaPoolsUrl: "https://www.pangea.trade/api/pools",
  marketDataUrl: "/api/market/pools",
  tokenRegistryUrl: "/api/market/tokens",
  tokenMetadataUrl: "/api/market/token-meta",
  marketHistoryUrl: "/api/market/history",
  bridgeActivityUrl: "/api/market/bridge-activity",
  tradeQuoteUrl: "/api/market/trade-quote",
  bridgeQuoteUrl: "/api/market/bridge-quote",
  launchesUrl: "/api/market/launches",
  solanaRpcUrl: "https://api.mainnet-beta.solana.com",
  refreshMs: 15000,
  branding: {
    icon: "assets/coni_logo_icon-2.png",
    wordmark: "assets/coni_wordmark-1.png"
  },
  token: {
    slug: "zgap",
    name: "Zero Gap",
    symbol: "ZGAP",
    decimals: 9,
    contracts: {
      ethereum: "0xdbA134AD399e15D9175CE6bC97BEcA7001Fb96eA",
      base: "0xdbA134AD399e15D9175CE6bC97BEcA7001Fb96eA",
      robinhood: "0xdbA134AD399e15D9175CE6bC97BEcA7001Fb96eA",
      bsc: "0xdbA134AD399e15D9175CE6bC97BEcA7001Fb96eA",
      solana: "5KtxmGhFfy6QaoU3Qtu2ouweqq1qfXfNZHg3CJH7y6ea"
    }
  },
  wallets: {
    evmChains: { ethereum: 1, base: 8453, robinhood: 4663, bsc: 56 },
    solanaClusters: ["mainnet-beta"],
    connectTimeoutMs: 20000
  }
};
