-- ============================================================
-- CollectLocal — Supabase Database Schema
-- Run this in your Supabase SQL Editor (free tier)
-- ============================================================

-- Listings table: stores native CollectLocal listings
CREATE TABLE IF NOT EXISTS listings (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,                 -- Card name (e.g., "Umbreon VMAX")
    card_set TEXT,                      -- Set name (e.g., "Evolving Skies")
    card_number TEXT,                   -- Card number (e.g., "215/203")
    grade TEXT DEFAULT 'Raw',           -- Grade (e.g., "PSA 10", "BGS 9.5", "Raw", "Sealed")
    price DECIMAL(10,2) NOT NULL,       -- Listing price in USD
    market_price DECIMAL(10,2),         -- Market value from TCGPlayer/eBay
    market_source TEXT DEFAULT 'tcgplayer', -- Where market price came from
    image_url TEXT,                     -- Card image URL (primary/TCG API)
    listing_photos JSONB DEFAULT '[]'::jsonb,  -- All original listing photos (array of URLs)
    marketplace TEXT DEFAULT 'facebook', -- Source marketplace (renamed to `source` by pivot_native_listings.sql)
    location TEXT,                      -- Seller location text
    lat DECIMAL(10,6),                  -- Seller latitude
    lng DECIMAL(10,6),                  -- Seller longitude
    description TEXT,                   -- Listing description
    listing_url TEXT,                   -- Direct link to the source listing (renamed to `source_url` by pivot_native_listings.sql)
    ebay_price DECIMAL(10,2),           -- eBay lowest Buy It Now price (real floor price)
    ebay_url TEXT,                      -- eBay search URL for this card (tap to see listings)
    is_active BOOLEAN DEFAULT TRUE,     -- Set false when listing is sold/removed
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for the queries the iOS app will make
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_listings_marketplace ON listings(marketplace);
CREATE INDEX IF NOT EXISTS idx_listings_name ON listings USING gin(to_tsvector('english', name));

-- Row-Level Security: allow anonymous reads (iOS app uses anon key)
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read access"
    ON listings
    FOR SELECT
    USING (true);

-- Only the service_role key (backend scraper) can insert/update
CREATE POLICY "Allow service role write access"
    ON listings
    FOR ALL
    USING (auth.role() = 'service_role');
