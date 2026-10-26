-- Legacy Node migration reference. Django is canonical; use `python manage.py migrate`.
-- Do not apply this file to a Django database unless you have compared schemas.

CREATE TABLE IF NOT EXISTS wallet_challenges (
  id UUID PRIMARY KEY,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  message TEXT NOT NULL,
  nonce TEXT NOT NULL UNIQUE,
  expires_at TIMESTAMPTZ NOT NULL,
  consumed_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS wallet_identities (
  id UUID PRIMARY KEY,
  chain TEXT NOT NULL,
  address TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_authenticated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chain, address)
);

CREATE TABLE IF NOT EXISTS ledger_accounts (
  id UUID PRIMARY KEY,
  owner_id UUID,
  asset TEXT NOT NULL,
  chain TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('user_liability', 'platform_asset', 'fee_revenue', 'fee_receivable')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, asset, chain, kind)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
  id UUID PRIMARY KEY,
  reference_type TEXT NOT NULL,
  reference_id TEXT NOT NULL,
  debit_account UUID NOT NULL REFERENCES ledger_accounts(id),
  credit_account UUID NOT NULL REFERENCES ledger_accounts(id),
  asset TEXT NOT NULL,
  amount_base_units NUMERIC(78, 0) NOT NULL CHECK (amount_base_units > 0),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (reference_type, reference_id, debit_account, credit_account)
);

CREATE TABLE IF NOT EXISTS funding_intents (
  id UUID PRIMARY KEY,
  wallet_identity_id UUID REFERENCES wallet_identities(id),
  asset TEXT NOT NULL,
  chain TEXT NOT NULL,
  amount_base_units NUMERIC(78, 0) NOT NULL,
  destination TEXT NOT NULL,
  status TEXT NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fee_events (
  id UUID PRIMARY KEY,
  user_identity_id UUID REFERENCES wallet_identities(id),
  execution_id TEXT,
  asset TEXT NOT NULL,
  chain TEXT NOT NULL,
  amount_base_units NUMERIC(78, 0) NOT NULL CHECK (amount_base_units > 0),
  recipient TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('accrued', 'swept', 'failed')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_controls (
  scope TEXT PRIMARY KEY,
  enabled BOOLEAN NOT NULL DEFAULT false,
  paused BOOLEAN NOT NULL DEFAULT true,
  min_net_bps INTEGER NOT NULL DEFAULT 50,
  max_notional_usd NUMERIC(30, 8) NOT NULL DEFAULT 25,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS executions (
  id UUID PRIMARY KEY,
  user_identity_id UUID REFERENCES wallet_identities(id),
  slug TEXT NOT NULL,
  state TEXT NOT NULL,
  expected_net_bps INTEGER,
  expected_profit_usd NUMERIC(30, 8),
  quote_hash TEXT,
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO bot_controls (scope) VALUES ('global') ON CONFLICT (scope) DO NOTHING;
