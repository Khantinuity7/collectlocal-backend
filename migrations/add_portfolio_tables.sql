-- ============================================================
-- Portfolio Feature Tables Migration
-- ============================================================

-- Enum types
DO $$ BEGIN
    CREATE TYPE snapshot_type AS ENUM ('daily', 'event', 'manual');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE transaction_type AS ENUM ('added', 'removed', 'sold', 'imported', 'price_milestone');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE milestone_type AS ENUM ('value_reached', 'card_count', 'set_completed', 'biggest_gain');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

DO $$ BEGIN
    CREATE TYPE price_direction AS ENUM ('up', 'down');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

-- 1. Portfolio Snapshots (time-series value tracking)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    total_value NUMERIC NOT NULL DEFAULT 0,
    total_cards INTEGER NOT NULL DEFAULT 0,
    snapshot_type snapshot_type NOT NULL DEFAULT 'daily',
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    change_percent DOUBLE PRECISION,
    change_dollars NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user_date ON portfolio_snapshots(user_id, recorded_at DESC);

-- 2. Collection Items (cards in user's collection)
CREATE TABLE IF NOT EXISTS collection_items (
    id TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_name TEXT NOT NULL,
    card_number TEXT,
    tcg TEXT DEFAULT 'pokemon',
    quantity INTEGER NOT NULL DEFAULT 1,
    total_qty INTEGER NOT NULL DEFAULT 1,
    cost_basis NUMERIC,
    condition TEXT,
    grade_company TEXT,
    grade_value TEXT,
    notes TEXT,
    image_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collection_items_user ON collection_items(user_id);

-- 3. Portfolio Holdings (view combining items with live pricing)
CREATE OR REPLACE VIEW portfolio_holdings AS
SELECT
    jsonb_build_object(
        'id', ci.id,
        'card_id', ci.card_id,
        'card_name', ci.card_name,
        'set_name', ci.set_name,
        'card_number', ci.card_number,
        'tcg', ci.tcg,
        'quantity', ci.quantity,
        'total_qty', ci.total_qty,
        'image_url', ci.image_url,
        'created_at', ci.created_at
    ) AS collection_item,
    COALESCE(cc.tcgplayer_price_market, 0) AS current_price,
    ci.cost_basis,
    CASE WHEN ci.cost_basis IS NOT NULL AND ci.cost_basis > 0
        THEN (COALESCE(cc.tcgplayer_price_market, 0) * ci.total_qty) - (ci.cost_basis * ci.total_qty)
        ELSE NULL
    END AS unrealized_pnl,
    CASE WHEN ci.cost_basis IS NOT NULL AND ci.cost_basis > 0
        THEN ((COALESCE(cc.tcgplayer_price_market, 0) - ci.cost_basis) / ci.cost_basis * 100)::double precision
        ELSE NULL
    END AS unrealized_pnl_percent,
    '[]'::jsonb AS sparkline_data,
    CASE WHEN ci.grade_company IS NOT NULL
        THEN jsonb_build_object('company', ci.grade_company, 'value', ci.grade_value)
        ELSE NULL
    END AS grade,
    ci.user_id,
    COALESCE(cc.tcgplayer_price_market, 0) * ci.total_qty AS total_value,
    ci.card_name AS card_name_sort,
    ci.created_at AS created_at_sort
FROM collection_items ci
LEFT JOIN card_catalog cc ON ci.card_id = cc.id
ORDER BY total_value DESC;

-- 4. Collection Transactions
CREATE TABLE IF NOT EXISTS collection_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    card_id TEXT NOT NULL,
    card_name TEXT NOT NULL,
    set_name TEXT NOT NULL,
    transaction_type transaction_type NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    price_at_time NUMERIC,
    cost_basis NUMERIC,
    sale_price NUMERIC,
    realized_pnl NUMERIC,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_collection_transactions_user ON collection_transactions(user_id, created_at DESC);

-- 5. Collection Milestones
CREATE TABLE IF NOT EXISTS collection_milestones (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    milestone_type milestone_type NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    achieved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    value NUMERIC
);
CREATE INDEX IF NOT EXISTS idx_collection_milestones_user ON collection_milestones(user_id);

-- 6. Market Movers (daily price changes)
CREATE TABLE IF NOT EXISTS market_movers (
    card_id TEXT PRIMARY KEY,
    card_name TEXT NOT NULL,
    set_name TEXT NOT NULL,
    price_change NUMERIC NOT NULL DEFAULT 0,
    price_change_percent DOUBLE PRECISION NOT NULL DEFAULT 0,
    direction price_direction NOT NULL DEFAULT 'up',
    image_url TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 7. RPC: Realized P&L Summary
CREATE OR REPLACE FUNCTION realized_pnl_summary(user_uuid UUID DEFAULT auth.uid())
RETURNS JSON AS $$
    SELECT json_build_object(
        'total_pnl', COALESCE(SUM(realized_pnl), 0),
        'cards_sold', COALESCE(SUM(quantity), 0),
        'avg_return_percent', COALESCE(
            AVG(CASE WHEN cost_basis > 0
                THEN ((sale_price - cost_basis) / cost_basis * 100)
                ELSE NULL
            END), 0
        )
    )
    FROM collection_transactions
    WHERE user_id = user_uuid
    AND transaction_type = 'sold'
    AND realized_pnl IS NOT NULL;
$$ LANGUAGE sql STABLE SECURITY DEFINER;

-- ============================================================
-- Row Level Security
-- ============================================================

ALTER TABLE portfolio_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE collection_milestones ENABLE ROW LEVEL SECURITY;

-- Portfolio Snapshots: users see own data
CREATE POLICY "Users view own snapshots" ON portfolio_snapshots
    FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Service inserts snapshots" ON portfolio_snapshots
    FOR INSERT WITH CHECK (true);

-- Collection Items: full CRUD for own items
CREATE POLICY "Users view own items" ON collection_items
    FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users insert own items" ON collection_items
    FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users update own items" ON collection_items
    FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Users delete own items" ON collection_items
    FOR DELETE USING (auth.uid() = user_id);

-- Collection Transactions: users see own
CREATE POLICY "Users view own transactions" ON collection_transactions
    FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users insert own transactions" ON collection_transactions
    FOR INSERT WITH CHECK (auth.uid() = user_id);

-- Collection Milestones: users see own
CREATE POLICY "Users view own milestones" ON collection_milestones
    FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Service inserts milestones" ON collection_milestones
    FOR INSERT WITH CHECK (true);

-- Market Movers: public read
ALTER TABLE market_movers ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Public read market movers" ON market_movers
    FOR SELECT USING (true);
CREATE POLICY "Service writes market movers" ON market_movers
    FOR ALL USING (true) WITH CHECK (true);

