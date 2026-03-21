-- Migration: Add direct eBay BIN listing URL columns
-- Date: 2026-03-12
-- Purpose: Store the direct link to the cheapest eBay Buy It Now listing
--          (ebay_listing_url) alongside the existing search fallback (ebay_url).
--          Also stores the matched listing title for UI display.

-- 1. Add new columns to listings table
ALTER TABLE listings
ADD COLUMN IF NOT EXISTS ebay_listing_url TEXT,
ADD COLUMN IF NOT EXISTS ebay_listing_title TEXT;

-- 2. Add comment for documentation
COMMENT ON COLUMN listings.ebay_listing_url IS 'Direct URL to cheapest eBay BIN listing (from Browse API itemWebUrl). NULL if no exact match found.';
COMMENT ON COLUMN listings.ebay_listing_title IS 'Title of the matched eBay BIN listing. NULL if no exact match found.';

-- 3. Index for quick lookups on listings that have direct BIN links
CREATE INDEX IF NOT EXISTS idx_listings_has_ebay_listing
ON listings (ebay_listing_url)
WHERE ebay_listing_url IS NOT NULL;

-- 4. Update the lot_listings_with_cards view to expose the new columns
-- (Only needed if you have a view — adjust to match your actual view name)
-- DROP VIEW IF EXISTS lot_listings_with_cards;
-- CREATE OR REPLACE VIEW lot_listings_with_cards AS
-- SELECT l.*, ... FROM listings l LEFT JOIN ...;

-- 5. Grant RLS access (if using row-level security, the new columns
--    inherit the existing policy on the listings table — no changes needed)
