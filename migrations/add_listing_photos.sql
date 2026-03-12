-- Migration: Add listing_photos column to store all FB Marketplace photos
-- Run this in Supabase SQL Editor

-- Add JSONB array column for all listing photos
ALTER TABLE listings
ADD COLUMN IF NOT EXISTS listing_photos JSONB DEFAULT '[]'::jsonb;

-- Backfill: copy existing image_url into listing_photos for rows that have one
UPDATE listings
SET listing_photos = jsonb_build_array(image_url)
WHERE image_url IS NOT NULL
  AND image_url != ''
  AND (listing_photos IS NULL OR listing_photos = '[]'::jsonb);
