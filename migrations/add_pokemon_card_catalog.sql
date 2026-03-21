-- ============================================================
-- CollectLocal — Pokémon Card Catalog Tables
-- Stores the canonical database of all Pokémon TCG sets & cards
-- Seeded from the pokemontcg.io API, refreshed periodically
-- ============================================================

-- =========================
-- 1. SETS TABLE
-- =========================
CREATE TABLE IF NOT EXISTS pokemon_sets (
    id TEXT PRIMARY KEY,                    -- API set ID (e.g., "swsh7", "base1", "sv3pt5")
    name TEXT NOT NULL,                     -- Display name (e.g., "Evolving Skies")
    series TEXT NOT NULL,                   -- Series group (e.g., "Sword & Shield", "Scarlet & Violet")
    printed_total INTEGER NOT NULL,         -- Number of cards in official print run
    total INTEGER NOT NULL,                 -- Total cards including secrets
    release_date DATE,                      -- Set release date
    symbol_url TEXT,                        -- Set symbol image URL
    logo_url TEXT,                          -- Set logo image URL
    ptcgo_code TEXT,                        -- Pokémon TCG Online code (e.g., "EVS")
    sort_order INTEGER DEFAULT 0,           -- For custom ordering (newest first)
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pokemon_sets_series ON pokemon_sets(series);
CREATE INDEX IF NOT EXISTS idx_pokemon_sets_release ON pokemon_sets(release_date DESC);
CREATE INDEX IF NOT EXISTS idx_pokemon_sets_name ON pokemon_sets USING gin(to_tsvector('english', name));

-- =========================
-- 2. CARDS TABLE
-- =========================
CREATE TABLE IF NOT EXISTS pokemon_cards (
    id TEXT PRIMARY KEY,                    -- API card ID (e.g., "swsh7-215")
    name TEXT NOT NULL,                     -- Card name (e.g., "Umbreon VMAX")
    supertype TEXT NOT NULL DEFAULT 'Pokémon', -- Pokémon, Trainer, Energy
    subtypes TEXT[],                        -- Array: ["Stage 2", "VMAX"], ["Item"], etc.
    hp TEXT,                                -- HP value (e.g., "320")
    types TEXT[],                           -- Pokémon types: ["Dark"], ["Fire", "Water"]
    set_id TEXT NOT NULL REFERENCES pokemon_sets(id) ON DELETE CASCADE,
    set_name TEXT NOT NULL,                 -- Denormalized for fast display
    number TEXT NOT NULL,                   -- Card number in set (e.g., "215", "TG30")
    printed_number TEXT,                    -- Printed number (e.g., "215/203")
    rarity TEXT,                            -- Rarity (e.g., "Illustration Rare", "Ultra Rare")
    artist TEXT,                            -- Card illustrator
    image_small TEXT,                       -- Small card image URL
    image_large TEXT,                       -- High-res card image URL
    tcgplayer_url TEXT,                     -- TCGPlayer product page
    tcgplayer_price_low DECIMAL(10,2),      -- TCGPlayer market low
    tcgplayer_price_mid DECIMAL(10,2),      -- TCGPlayer market mid
    tcgplayer_price_high DECIMAL(10,2),     -- TCGPlayer market high
    tcgplayer_price_market DECIMAL(10,2),   -- TCGPlayer market price
    cardmarket_price DECIMAL(10,2),         -- Cardmarket average sell price (EU)
    -- Full-text search column (auto-populated by trigger)
    search_vector tsvector,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for the queries the iOS app will make
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_set ON pokemon_cards(set_id);
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_name ON pokemon_cards USING gin(to_tsvector('english', name));
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_rarity ON pokemon_cards(rarity);
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_supertype ON pokemon_cards(supertype);
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_search ON pokemon_cards USING gin(search_vector);

-- Composite search vector: name (highest weight) + set_name + rarity + number
CREATE OR REPLACE FUNCTION pokemon_cards_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.set_name, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(NEW.rarity, '')), 'C') ||
        setweight(to_tsvector('simple', COALESCE(NEW.number, '')), 'D');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER pokemon_cards_search_update
    BEFORE INSERT OR UPDATE ON pokemon_cards
    FOR EACH ROW EXECUTE FUNCTION pokemon_cards_search_trigger();

-- =========================
-- 3. ROW-LEVEL SECURITY
-- =========================
ALTER TABLE pokemon_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE pokemon_cards ENABLE ROW LEVEL SECURITY;

-- Public read access (catalog is open data)
CREATE POLICY "Allow anonymous read pokemon_sets"
    ON pokemon_sets FOR SELECT USING (true);

CREATE POLICY "Allow anonymous read pokemon_cards"
    ON pokemon_cards FOR SELECT USING (true);

-- Only service_role (backend scripts) can write
CREATE POLICY "Allow service role write pokemon_sets"
    ON pokemon_sets FOR ALL USING (auth.role() = 'service_role');

CREATE POLICY "Allow service role write pokemon_cards"
    ON pokemon_cards FOR ALL USING (auth.role() = 'service_role');

-- =========================
-- 4. FUZZY AUTOCOMPLETE FUNCTION
-- =========================
-- Called from the iOS app for type-ahead search
-- Supports: "umbr" → Umbreon VMAX, "215" → card #215, "evolving" → Evolving Skies cards
CREATE OR REPLACE FUNCTION search_pokemon_cards(
    query_text TEXT,
    result_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    card_id TEXT,
    card_name TEXT,
    set_id TEXT,
    set_name TEXT,
    card_number TEXT,
    rarity TEXT,
    supertype TEXT,
    image_small TEXT,
    image_large TEXT,
    tcgplayer_price_market DECIMAL,
    rank REAL
) AS $$
BEGIN
    RETURN QUERY
    WITH scored AS (
        -- Strategy 1: Full-text search (handles stemming, multi-word)
        SELECT
            pc.id, pc.name, pc.set_id, pc.set_name, pc.number,
            pc.rarity, pc.supertype, pc.image_small, pc.image_large,
            pc.tcgplayer_price_market,
            ts_rank(pc.search_vector, websearch_to_tsquery('english', query_text)) AS fts_rank
        FROM pokemon_cards pc
        WHERE pc.search_vector @@ websearch_to_tsquery('english', query_text)

        UNION ALL

        -- Strategy 2: ILIKE prefix match (handles partial typing: "umbr", "char")
        SELECT
            pc.id, pc.name, pc.set_id, pc.set_name, pc.number,
            pc.rarity, pc.supertype, pc.image_small, pc.image_large,
            pc.tcgplayer_price_market,
            0.5::REAL AS fts_rank
        FROM pokemon_cards pc
        WHERE pc.name ILIKE (query_text || '%')
           OR pc.name ILIKE ('% ' || query_text || '%')
           OR pc.number = query_text

        UNION ALL

        -- Strategy 3: Exact card number match (handles "215/203", "TG30")
        SELECT
            pc.id, pc.name, pc.set_id, pc.set_name, pc.number,
            pc.rarity, pc.supertype, pc.image_small, pc.image_large,
            pc.tcgplayer_price_market,
            0.3::REAL AS fts_rank
        FROM pokemon_cards pc
        WHERE pc.printed_number = query_text
    )
    SELECT DISTINCT ON (s.id)
        s.id, s.name, s.set_id, s.set_name, s.number,
        s.rarity, s.supertype, s.image_small, s.image_large,
        s.tcgplayer_price_market,
        MAX(s.fts_rank) AS rank
    FROM scored s
    GROUP BY s.id, s.name, s.set_id, s.set_name, s.number,
             s.rarity, s.supertype, s.image_small, s.image_large,
             s.tcgplayer_price_market
    ORDER BY rank DESC, s.name ASC
    LIMIT result_limit;
END;
$$ LANGUAGE plpgsql;

-- =========================
-- 5. SET SEARCH FUNCTION
-- =========================
CREATE OR REPLACE FUNCTION search_pokemon_sets(
    query_text TEXT,
    result_limit INTEGER DEFAULT 20
)
RETURNS TABLE (
    set_id TEXT,
    set_name TEXT,
    series TEXT,
    total INTEGER,
    release_date DATE,
    symbol_url TEXT,
    logo_url TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        ps.id, ps.name, ps.series, ps.total,
        ps.release_date, ps.symbol_url, ps.logo_url
    FROM pokemon_sets ps
    WHERE ps.name ILIKE ('%' || query_text || '%')
       OR ps.series ILIKE ('%' || query_text || '%')
       OR ps.ptcgo_code ILIKE query_text
    ORDER BY ps.release_date DESC
    LIMIT result_limit;
END;
$$ LANGUAGE plpgsql;

-- =========================
-- 6. SEED TRACKING TABLE
-- =========================
CREATE TABLE IF NOT EXISTS pokemon_catalog_syncs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    sets_synced INTEGER DEFAULT 0,
    cards_synced INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running',       -- running, success, failed
    error_message TEXT
);

ALTER TABLE pokemon_catalog_syncs ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow anonymous read pokemon_catalog_syncs"
    ON pokemon_catalog_syncs FOR SELECT USING (true);

CREATE POLICY "Allow service role write pokemon_catalog_syncs"
    ON pokemon_catalog_syncs FOR ALL USING (auth.role() = 'service_role');
