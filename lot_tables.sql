-- ============================================================
-- CollectLocal — Lot Analyzer Database Migration
-- Run this in your Supabase SQL Editor AFTER setup_supabase.sql
-- ============================================================

-- Add lot columns to existing listings table
ALTER TABLE listings ADD COLUMN IF NOT EXISTS card_type TEXT DEFAULT 'single';
ALTER TABLE listings ADD COLUMN IF NOT EXISTS is_lot BOOLEAN DEFAULT FALSE;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS lot_card_count INTEGER;
ALTER TABLE listings ADD COLUMN IF NOT EXISTS lot_estimated_value DECIMAL(10,2);

-- Index for filtering lot listings in the app
CREATE INDEX IF NOT EXISTS idx_listings_is_lot ON listings(is_lot) WHERE is_lot = TRUE;

-- ============================================================
-- Lot Analysis: one record per analyzed lot listing
-- ============================================================
CREATE TABLE IF NOT EXISTS lot_analysis (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    is_lot BOOLEAN NOT NULL DEFAULT TRUE,
    lot_confidence FLOAT NOT NULL DEFAULT 0,
    total_estimated_value DECIMAL(10,2),
    card_count INTEGER,
    unidentified_count INTEGER DEFAULT 0,
    analysis_model TEXT NOT NULL DEFAULT 'gemini-2.5-flash-lite',
    analyzed_at TIMESTAMPTZ DEFAULT NOW(),
    raw_response JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- One analysis per listing (re-analysis replaces the old one)
CREATE UNIQUE INDEX IF NOT EXISTS idx_lot_analysis_listing ON lot_analysis(listing_id);
CREATE INDEX IF NOT EXISTS idx_lot_analysis_value ON lot_analysis(total_estimated_value DESC NULLS LAST);

-- ============================================================
-- Lot Cards: individual cards identified within a lot
-- ============================================================
CREATE TABLE IF NOT EXISTS lot_cards (
    id SERIAL PRIMARY KEY,
    lot_analysis_id INTEGER NOT NULL REFERENCES lot_analysis(id) ON DELETE CASCADE,
    card_name TEXT NOT NULL,
    card_set TEXT,
    card_number TEXT,
    estimated_grade TEXT DEFAULT 'Raw',
    confidence FLOAT NOT NULL DEFAULT 0.5,
    market_price DECIMAL(10,2),
    price_source TEXT,              -- 'tcgplayer' or 'ebay'
    ebay_url TEXT,                  -- Affiliate link to eBay search
    source_type TEXT NOT NULL DEFAULT 'vision',  -- 'vision', 'text', or 'both'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lot_cards_analysis ON lot_cards(lot_analysis_id);
CREATE INDEX IF NOT EXISTS idx_lot_cards_price ON lot_cards(market_price DESC NULLS LAST);

-- ============================================================
-- Row-Level Security
-- ============================================================

-- lot_analysis: anonymous read, service_role write
ALTER TABLE lot_analysis ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read lot_analysis"
    ON lot_analysis
    FOR SELECT
    USING (true);

CREATE POLICY "Allow service role write lot_analysis"
    ON lot_analysis
    FOR ALL
    USING (auth.role() = 'service_role');

-- lot_cards: anonymous read, service_role write
ALTER TABLE lot_cards ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read lot_cards"
    ON lot_cards
    FOR SELECT
    USING (true);

CREATE POLICY "Allow service role write lot_cards"
    ON lot_cards
    FOR ALL
    USING (auth.role() = 'service_role');

-- ============================================================
-- View: ALL active listings with lot card data joined when available
-- The iOS app queries this view instead of the raw listings table
-- ============================================================
CREATE OR REPLACE VIEW listings_with_lot_cards AS
SELECT
    l.*,
    la.total_estimated_value AS lot_total_value,
    la.card_count AS lot_cards_count,
    la.unidentified_count AS lot_unidentified,
    la.lot_confidence,
    la.analyzed_at AS lot_analyzed_at,
    CASE WHEN la.id IS NOT NULL THEN
        COALESCE(
            json_agg(
                json_build_object(
                    'card_name', lc.card_name,
                    'card_set', lc.card_set,
                    'card_number', lc.card_number,
                    'estimated_grade', lc.estimated_grade,
                    'confidence', lc.confidence,
                    'market_price', lc.market_price,
                    'price_source', lc.price_source,
                    'ebay_url', lc.ebay_url,
                    'source_type', lc.source_type
                ) ORDER BY lc.market_price DESC NULLS LAST
            ) FILTER (WHERE lc.id IS NOT NULL),
            '[]'::json
        )
    ELSE NULL END AS lot_cards
FROM listings l
LEFT JOIN lot_analysis la ON la.listing_id = l.id
LEFT JOIN lot_cards lc ON lc.lot_analysis_id = la.id
WHERE l.is_active = TRUE
GROUP BY l.id, la.id;
