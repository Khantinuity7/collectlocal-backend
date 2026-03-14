-- ============================================================
-- CollectLocal — Restock Alerts Database Schema
-- Run this in your Supabase SQL Editor
-- Creates tables for: products, stores, inventory, waitlist,
-- reports, scout rewards, device tokens, and restock events
-- ============================================================

-- Enable PostGIS for location queries (already enabled on most Supabase projects)
CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================
-- 1. RESTOCK PRODUCTS — TCG sealed products we track
-- ============================================================
CREATE TABLE IF NOT EXISTS restock_products (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    tcg TEXT NOT NULL,
    product_type TEXT NOT NULL,
    msrp DECIMAL(10,2) NOT NULL,
    upc TEXT,
    target_dpci TEXT,
    target_tcin TEXT,
    walmart_sku TEXT,
    walmart_url TEXT,
    image_url TEXT,
    packaging_keywords TEXT[] DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_restock_products_active ON restock_products(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_restock_products_tcg ON restock_products(tcg);

ALTER TABLE restock_products ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read restock products" ON restock_products FOR SELECT USING (true);
CREATE POLICY "Service role writes restock products" ON restock_products FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 2. RETAIL STORES — Target & Walmart locations
-- ============================================================
CREATE TABLE IF NOT EXISTS retail_stores (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    retailer TEXT NOT NULL CHECK (retailer IN ('target', 'walmart')),
    store_number TEXT NOT NULL,
    name TEXT NOT NULL,
    address TEXT NOT NULL,
    city TEXT NOT NULL,
    state TEXT NOT NULL,
    zip_code TEXT NOT NULL,
    lat DECIMAL(10,6) NOT NULL,
    lng DECIMAL(10,6) NOT NULL,
    phone TEXT,
    location GEOGRAPHY(Point, 4326),
    target_location_id TEXT,
    walmart_store_id TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(retailer, store_number)
);

-- Auto-populate PostGIS geography column from lat/lng
CREATE OR REPLACE FUNCTION update_store_location()
RETURNS TRIGGER AS $$
BEGIN
    NEW.location = ST_SetSRID(ST_MakePoint(NEW.lng, NEW.lat), 4326)::geography;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_store_location
    BEFORE INSERT OR UPDATE OF lat, lng ON retail_stores
    FOR EACH ROW EXECUTE FUNCTION update_store_location();

CREATE INDEX IF NOT EXISTS idx_retail_stores_location ON retail_stores USING GIST(location);
CREATE INDEX IF NOT EXISTS idx_retail_stores_retailer ON retail_stores(retailer);

ALTER TABLE retail_stores ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read retail stores" ON retail_stores FOR SELECT USING (true);
CREATE POLICY "Service role writes retail stores" ON retail_stores FOR ALL USING (auth.role() = 'service_role');

-- RPC function: get nearby stores within radius
CREATE OR REPLACE FUNCTION nearby_retail_stores(user_lat DOUBLE PRECISION, user_lng DOUBLE PRECISION, radius_miles DOUBLE PRECISION DEFAULT 25)
RETURNS TABLE (
    id UUID, retailer TEXT, store_number TEXT, name TEXT,
    address TEXT, city TEXT, state TEXT, zip_code TEXT,
    lat DECIMAL, lng DECIMAL, phone TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        rs.id, rs.retailer, rs.store_number, rs.name,
        rs.address, rs.city, rs.state, rs.zip_code,
        rs.lat, rs.lng, rs.phone
    FROM retail_stores rs
    WHERE ST_DWithin(
        rs.location,
        ST_SetSRID(ST_MakePoint(user_lng, user_lat), 4326)::geography,
        radius_miles * 1609.34
    )
    ORDER BY rs.location <-> ST_SetSRID(ST_MakePoint(user_lng, user_lat), 4326)::geography;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 3. STORE INVENTORY — per-store per-product stock status
-- ============================================================
CREATE TABLE IF NOT EXISTS store_inventory (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES retail_stores(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES restock_products(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'unknown' CHECK (status IN ('in_stock', 'low_stock', 'out_of_stock', 'unknown')),
    quantity INT,
    last_checked TIMESTAMPTZ DEFAULT NOW(),
    source TEXT NOT NULL DEFAULT 'api_poll' CHECK (source IN ('api_poll', 'community_report', 'web_monitor')),
    price DECIMAL(10,2),
    on_sale BOOLEAN DEFAULT FALSE,
    UNIQUE(store_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_store_inventory_store ON store_inventory(store_id);
CREATE INDEX IF NOT EXISTS idx_store_inventory_product ON store_inventory(product_id);
CREATE INDEX IF NOT EXISTS idx_store_inventory_status ON store_inventory(status);

ALTER TABLE store_inventory ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read store inventory" ON store_inventory FOR SELECT USING (true);
CREATE POLICY "Service role writes store inventory" ON store_inventory FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 4. RESTOCK EVENTS — logged when a product goes 0 to >0
-- Used to trigger push notifications
-- ============================================================
CREATE TABLE IF NOT EXISTS restock_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id UUID NOT NULL REFERENCES retail_stores(id),
    product_id UUID NOT NULL REFERENCES restock_products(id),
    previous_quantity INT DEFAULT 0,
    new_quantity INT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api_poll',
    notifications_sent INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_restock_events_created ON restock_events(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_restock_events_product ON restock_events(product_id);

ALTER TABLE restock_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read restock events" ON restock_events FOR SELECT USING (true);
CREATE POLICY "Service role writes restock events" ON restock_events FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 5. RESTOCK WAITLIST — user alert subscriptions
-- ============================================================
CREATE TABLE IF NOT EXISTS restock_waitlist (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    product_id UUID NOT NULL REFERENCES restock_products(id) ON DELETE CASCADE,
    radius_miles INT DEFAULT 25,
    retailers TEXT[] DEFAULT '{target,walmart}',
    notify_push BOOLEAN DEFAULT TRUE,
    notify_in_app BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, product_id)
);

CREATE INDEX IF NOT EXISTS idx_restock_waitlist_user ON restock_waitlist(user_id);
CREATE INDEX IF NOT EXISTS idx_restock_waitlist_product ON restock_waitlist(product_id);

ALTER TABLE restock_waitlist ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users read own waitlist" ON restock_waitlist FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Users insert own waitlist" ON restock_waitlist FOR INSERT WITH CHECK (auth.uid() = user_id);
CREATE POLICY "Users delete own waitlist" ON restock_waitlist FOR DELETE USING (auth.uid() = user_id);
CREATE POLICY "Service role manages waitlist" ON restock_waitlist FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 6. RESTOCK REPORTS — community sighting submissions
-- ============================================================
CREATE TABLE IF NOT EXISTS restock_reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id UUID NOT NULL REFERENCES auth.users(id),
    store_id UUID NOT NULL REFERENCES retail_stores(id),
    photo_url TEXT,
    photo_hash TEXT,
    photo_exif_ts TIMESTAMPTZ,
    photo_lat DECIMAL(10,6),
    photo_lng DECIMAL(10,6),
    ai_detections JSONB DEFAULT '[]'::jsonb,
    ai_corrections JSONB DEFAULT '[]'::jsonb,
    status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'rejected', 'expired')),
    trust_score DECIMAL(3,2) DEFAULT 0.50,
    upvotes INT DEFAULT 0,
    downvotes INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_restock_reports_store ON restock_reports(store_id);
CREATE INDEX IF NOT EXISTS idx_restock_reports_created ON restock_reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_restock_reports_reporter ON restock_reports(reporter_id);

ALTER TABLE restock_reports ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read reports" ON restock_reports FOR SELECT USING (true);
CREATE POLICY "Authenticated users create reports" ON restock_reports FOR INSERT WITH CHECK (auth.uid() = reporter_id);
CREATE POLICY "Service role manages reports" ON restock_reports FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 7. SCOUT PROFILES — gamification/rewards state
-- ============================================================
CREATE TABLE IF NOT EXISTS scout_profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES auth.users(id) ON DELETE CASCADE,
    total_points INT DEFAULT 0,
    available_points INT DEFAULT 0,
    lifetime_points INT DEFAULT 0,
    report_count INT DEFAULT 0,
    verified_count INT DEFAULT 0,
    current_streak INT DEFAULT 0,
    longest_streak INT DEFAULT 0,
    trust_score DECIMAL(3,2) DEFAULT 0.50,
    badges JSONB DEFAULT '[]'::jsonb,
    active_profile_icon TEXT,
    rank TEXT DEFAULT 'rookie',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE scout_profiles ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Anyone can read scout profiles" ON scout_profiles FOR SELECT USING (true);
CREATE POLICY "Users update own profile" ON scout_profiles FOR UPDATE USING (auth.uid() = user_id);
CREATE POLICY "Service role manages profiles" ON scout_profiles FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 8. SCOUT POINTS LEDGER — point history
-- ============================================================
CREATE TABLE IF NOT EXISTS scout_points_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    points INT NOT NULL,
    reference_id UUID,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scout_points_user ON scout_points_ledger(user_id);

ALTER TABLE scout_points_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users read own points" ON scout_points_ledger FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Service role manages points" ON scout_points_ledger FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 9. DEVICE TOKENS — for push notifications (FCM)
-- ============================================================
CREATE TABLE IF NOT EXISTS device_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    fcm_token TEXT NOT NULL,
    platform TEXT DEFAULT 'ios',
    lat DECIMAL(10,6),
    lng DECIMAL(10,6),
    location GEOGRAPHY(Point, 4326),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(user_id, fcm_token)
);

-- Auto-populate location
CREATE OR REPLACE FUNCTION update_device_location()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.lat IS NOT NULL AND NEW.lng IS NOT NULL THEN
        NEW.location = ST_SetSRID(ST_MakePoint(NEW.lng, NEW.lat), 4326)::geography;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trigger_update_device_location
    BEFORE INSERT OR UPDATE OF lat, lng ON device_tokens
    FOR EACH ROW EXECUTE FUNCTION update_device_location();

CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_device_tokens_location ON device_tokens USING GIST(location);

ALTER TABLE device_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users manage own tokens" ON device_tokens FOR ALL USING (auth.uid() = user_id);
CREATE POLICY "Service role manages tokens" ON device_tokens FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- 10. NOTIFICATION LOG — track what was sent
-- ============================================================
CREATE TABLE IF NOT EXISTS notification_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES auth.users(id),
    restock_event_id UUID REFERENCES restock_events(id),
    product_name TEXT,
    store_name TEXT,
    sent_at TIMESTAMPTZ DEFAULT NOW(),
    status TEXT DEFAULT 'sent'
);

CREATE INDEX IF NOT EXISTS idx_notification_log_user ON notification_log(user_id);

ALTER TABLE notification_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Users read own notifications" ON notification_log FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY "Service role writes notifications" ON notification_log FOR ALL USING (auth.role() = 'service_role');

-- ============================================================
-- HELPER: Supabase Storage bucket for restock photos
-- Run this in Supabase Dashboard > Storage > New Bucket
-- Name: restock-photos, Public: true
-- ============================================================
