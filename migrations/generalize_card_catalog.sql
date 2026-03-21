-- ============================================================
-- CollectLocal — Generalize Card Catalog for Multi-TCG Support
-- Renames pokemon-specific tables to generic ones and adds
-- a 'tcg' column to distinguish between card games.
-- ============================================================

-- =========================
-- 1. ADD TCG COLUMN TO SETS
-- =========================
ALTER TABLE pokemon_sets ADD COLUMN IF NOT EXISTS tcg TEXT NOT NULL DEFAULT 'pokemon';
CREATE INDEX IF NOT EXISTS idx_pokemon_sets_tcg ON pokemon_sets(tcg);

-- =========================
-- 2. ADD TCG COLUMN TO CARDS
-- =========================
ALTER TABLE pokemon_cards ADD COLUMN IF NOT EXISTS tcg TEXT NOT NULL DEFAULT 'pokemon';
CREATE INDEX IF NOT EXISTS idx_pokemon_cards_tcg ON pokemon_cards(tcg);

-- =========================
-- 3. RENAME TABLES
-- =========================
ALTER TABLE pokemon_sets RENAME TO card_sets;
ALTER TABLE pokemon_cards RENAME TO card_catalog;
ALTER TABLE pokemon_catalog_syncs RENAME TO card_catalog_syncs;

-- =========================
-- 4. UPDATE SEARCH FUNCTIONS
-- =========================

-- Drop old functions
DROP FUNCTION IF EXISTS search_pokemon_cards(TEXT, INTEGER);
DROP FUNCTION IF EXISTS search_pokemon_sets(TEXT, INTEGER);

-- Recreate as generic search_cards with optional TCG filter
CREATE OR REPLACE FUNCTION search_cards(
    query TEXT,
    tcg_filter TEXT DEFAULT NULL,
    max_results INTEGER DEFAULT 15
)
RETURNS TABLE (
    id TEXT,
    name TEXT,
    set_id TEXT,
    set_name TEXT,
    number TEXT,
    printed_number TEXT,
    rarity TEXT,
    image_small TEXT,
    tcgplayer_price_market NUMERIC,
    tcg TEXT,
    rank REAL
) LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH

    -- Strategy 1: Full-text search (best for multi-word queries)
    fts AS (
        SELECT c.id, c.name, c.set_id, c.set_name, c.number,
               c.printed_number, c.rarity, c.image_small,
               c.tcgplayer_price_market, c.tcg,
               ts_rank(c.search_vector, websearch_to_tsquery('english', query)) AS rank
        FROM card_catalog c
        WHERE c.search_vector @@ websearch_to_tsquery('english', query)
          AND (tcg_filter IS NULL OR c.tcg = tcg_filter)
        ORDER BY rank DESC
        LIMIT max_results
    ),

    -- Strategy 2: ILIKE prefix match (best for partial typing: "umbr" → "Umbreon")
    prefix AS (
        SELECT c.id, c.name, c.set_id, c.set_name, c.number,
               c.printed_number, c.rarity, c.image_small,
               c.tcgplayer_price_market, c.tcg,
               CASE
                   WHEN LOWER(c.name) = LOWER(query) THEN 1.0
                   WHEN LOWER(c.name) LIKE LOWER(query) || '%' THEN 0.8
                   ELSE 0.5
               END::REAL AS rank
        FROM card_catalog c
        WHERE (LOWER(c.name) LIKE LOWER(query) || '%'
               OR LOWER(c.name) LIKE '% ' || LOWER(query) || '%')
          AND (tcg_filter IS NULL OR c.tcg = tcg_filter)
        ORDER BY rank DESC, c.name
        LIMIT max_results
    ),

    -- Strategy 3: Exact card number match (e.g., "25/102" or just "25")
    numcheck AS (
        SELECT c.id, c.name, c.set_id, c.set_name, c.number,
               c.printed_number, c.rarity, c.image_small,
               c.tcgplayer_price_market, c.tcg,
               0.7::REAL AS rank
        FROM card_catalog c
        WHERE (c.number = query OR c.printed_number = query)
          AND (tcg_filter IS NULL OR c.tcg = tcg_filter)
        LIMIT max_results
    ),

    -- Combine all strategies, deduplicate, take the best rank per card
    combined AS (
        SELECT * FROM fts
        UNION ALL
        SELECT * FROM prefix
        UNION ALL
        SELECT * FROM numcheck
    )

    SELECT DISTINCT ON (combined.id)
           combined.id, combined.name, combined.set_id, combined.set_name,
           combined.number, combined.printed_number, combined.rarity,
           combined.image_small, combined.tcgplayer_price_market, combined.tcg,
           MAX(combined.rank) AS rank
    FROM combined
    GROUP BY combined.id, combined.name, combined.set_id, combined.set_name,
             combined.number, combined.printed_number, combined.rarity,
             combined.image_small, combined.tcgplayer_price_market, combined.tcg
    ORDER BY combined.id, rank DESC
    LIMIT max_results;
END;
$$;

-- Generic search_sets with optional TCG filter
CREATE OR REPLACE FUNCTION search_sets(
    query TEXT,
    tcg_filter TEXT DEFAULT NULL,
    max_results INTEGER DEFAULT 20
)
RETURNS TABLE (
    id TEXT,
    name TEXT,
    series TEXT,
    total INTEGER,
    release_date DATE,
    logo_url TEXT,
    tcg TEXT
) LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    SELECT s.id, s.name, s.series, s.total, s.release_date, s.logo_url, s.tcg
    FROM card_sets s
    WHERE (LOWER(s.name) LIKE '%' || LOWER(query) || '%'
           OR LOWER(s.series) LIKE '%' || LOWER(query) || '%')
      AND (tcg_filter IS NULL OR s.tcg = tcg_filter)
    ORDER BY s.release_date DESC NULLS LAST
    LIMIT max_results;
END;
$$;

-- =========================
-- 5. UPDATE SEARCH TRIGGER
-- =========================
-- Update the trigger function name and table reference
CREATE OR REPLACE FUNCTION card_catalog_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', COALESCE(NEW.name, '')), 'A') ||
        setweight(to_tsvector('english', COALESCE(NEW.set_name, '')), 'B') ||
        setweight(to_tsvector('english', COALESCE(NEW.rarity, '')), 'C') ||
        setweight(to_tsvector('english', COALESCE(NEW.artist, '')), 'D') ||
        setweight(to_tsvector('english', COALESCE(NEW.number, '')), 'B');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Drop old trigger and create new one
DROP TRIGGER IF EXISTS pokemon_cards_search_update ON card_catalog;
DROP TRIGGER IF EXISTS card_catalog_search_update ON card_catalog;
CREATE TRIGGER card_catalog_search_update
    BEFORE INSERT OR UPDATE ON card_catalog
    FOR EACH ROW EXECUTE FUNCTION card_catalog_search_trigger();

-- =========================
-- 6. UPDATE RLS POLICIES
-- =========================
-- Drop old policies (they reference old table names but Postgres keeps them after rename)
-- Re-create if needed
DO $$
BEGIN
    -- card_sets policies
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'card_sets' AND policyname = 'card_sets_public_read') THEN
        EXECUTE 'CREATE POLICY card_sets_public_read ON card_sets FOR SELECT USING (true)';
    END IF;

    -- card_catalog policies
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'card_catalog' AND policyname = 'card_catalog_public_read') THEN
        EXECUTE 'CREATE POLICY card_catalog_public_read ON card_catalog FOR SELECT USING (true)';
    END IF;

    -- card_catalog_syncs policies
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename = 'card_catalog_syncs' AND policyname = 'card_catalog_syncs_public_read') THEN
        EXECUTE 'CREATE POLICY card_catalog_syncs_public_read ON card_catalog_syncs FOR SELECT USING (true)';
    END IF;
END $$;
