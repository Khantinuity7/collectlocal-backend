-- ============================================================
-- CollectLocal — Native Listings Pivot Migration
-- Date: 2026-05-13
-- Purpose: Reshape the listings table from FB-scraped to native-only
--          per the pivot strategy memo. Unblocks the iOS feed switch
--          from ORDER BY scraped_at to ORDER BY created_at.
-- ============================================================
-- Companion file: wipe_legacy_scraped_listings.sql (manual run by Omar
-- post-merge to clear all FB-scraped rows before native listings flow in).
-- ============================================================

-- Drop the listings_with_lot_cards view first. It depends on columns
-- renamed/dropped below (scraped_at among them), so Postgres blocks the
-- ALTERs until the view is gone. Per the pivot, the view is intentionally
-- NOT recreated -- the Scan feature queries lot_analysis directly.
DROP VIEW IF EXISTS listings_with_lot_cards;

-- Rename marketplace -> source (still tags origin; default flips to 'native')
ALTER TABLE listings RENAME COLUMN marketplace TO source;
ALTER TABLE listings ALTER COLUMN source SET DEFAULT 'native';

-- Rename listing_url -> source_url (reserved for future eBay-only imports)
ALTER TABLE listings RENAME COLUMN listing_url TO source_url;

-- Drop FB-specific columns
ALTER TABLE listings DROP COLUMN scraped_at;
ALTER TABLE listings DROP COLUMN external_id;
ALTER TABLE listings DROP COLUMN seller;
ALTER TABLE listings DROP COLUMN seller_rating;
ALTER TABLE listings DROP COLUMN posted;
ALTER TABLE listings DROP COLUMN distance;

-- Swap feed-ordering index from scraped_at to created_at
DROP INDEX IF EXISTS idx_listings_scraped;
CREATE INDEX idx_listings_created_at ON listings(created_at DESC);
