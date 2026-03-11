-- ============================================================
-- CollectLocal — Supabase Database Schema
-- Run this in your Supabase SQL Editor (free tier)
-- ============================================================

-- Listings table: stores scraped FB Marketplace + Craigslist data
CREATE TABLE IF NOT EXISTS listings (
    id SERIAL PRIMARY KEY,
    external_id TEXT UNIQUE,            -- Original marketplace listing ID (dedup key)
    name TEXT NOT NULL,                 -- Card name (e.g., "Umbreon VMAX")
    card_set TEXT,                      -- Set name (e.g., "Evolving Skies")
    card_number TEXT,                   -- Card number (e.g., "215/203")
    grade TEXT DEFAULT 'Raw',           -- Grade (e.g., "PSA 10", "BGS 9.5", "Raw", "Sealed")
    price DECIMAL(10,2) NOT NULL,       -- Listing price in USD
    market_price DECIMAL(10,2),         -- Market value from TCGPlayer/eBay
    market_source TEXT DEFAULT 'tcgplayer', -- Where market price came from
    image_url TEXT,                     -- Card image URL
    marketplace TEXT DEFAULT 'facebook', -- Source marketplace
    location TEXT,                      -- Seller location text
    distance INTEGER DEFAULT 0,         -- Miles from configured home location
    posted TEXT,                        -- Human-readable time (e.g., "2 hrs ago")
    seller TEXT,                        -- Seller name/username
    seller_rating DECIMAL(3,1) DEFAULT 0,
    lat DECIMAL(10,6),                  -- Seller latitude
    lng DECIMAL(10,6),                  -- Seller longitude
    description TEXT,                   -- Listing description
    listing_url TEXT,                   -- Direct link to the marketplace listing
    scraped_at TIMESTAMPTZ DEFAULT NOW(),
    is_active BOOLEAN DEFAULT TRUE,     -- Set false when listing is sold/removed
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for the queries the iOS app will make
CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_listings_marketplace ON listings(marketplace);
CREATE INDEX IF NOT EXISTS idx_listings_distance ON listings(distance);
CREATE INDEX IF NOT EXISTS idx_listings_scraped ON listings(scraped_at DESC);
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

-- ============================================================
-- Optional: scrape_runs table to track scraper health
-- ============================================================
CREATE TABLE IF NOT EXISTS scrape_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    listings_found INTEGER DEFAULT 0,
    listings_new INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',      -- running, success, failed
    error_message TEXT
);

ALTER TABLE scrape_runs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read scrape_runs"
    ON scrape_runs
    FOR SELECT
    USING (true);

CREATE POLICY "Allow service role write scrape_runs"
    ON scrape_runs
    FOR ALL
    USING (auth.role() = 'service_role');
