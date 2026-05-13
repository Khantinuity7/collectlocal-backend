-- ============================================================
-- CollectLocal — Wipe Legacy FB-Scraped Listings
-- Date: 2026-05-13
-- Run MANUALLY in Supabase SQL Editor after pivot_native_listings.sql
-- has been applied. All current rows are FB-scraped; native listings
-- start empty.
-- ============================================================

DELETE FROM listings;
