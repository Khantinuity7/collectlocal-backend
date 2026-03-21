-- ============================================================
-- CollectLocal — Trust & Verification System
-- Meetup Codes, Bidirectional Reviews, Reports
-- Run this in Supabase SQL Editor
-- ============================================================

-- ============================================================
-- 1. ADD TRUST COLUMNS TO PROFILES
-- ============================================================
ALTER TABLE profiles
    ADD COLUMN IF NOT EXISTS phone_verified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS id_verified BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS response_rate DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS completed_transactions INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS seller_rating DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS seller_review_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS buyer_rating DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS buyer_review_count INTEGER NOT NULL DEFAULT 0;


-- ============================================================
-- 2. MEETUPS TABLE
-- Tracks meetup verification codes between buyer/seller
-- ============================================================
CREATE TABLE IF NOT EXISTS meetups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    listing_id INTEGER,
    buyer_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    seller_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    verification_code TEXT NOT NULL,           -- 4-digit code
    status TEXT NOT NULL DEFAULT 'pending',    -- pending, confirmed, expired
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '24 hours'),
    CONSTRAINT meetup_different_users CHECK (buyer_id != seller_id)
);

CREATE INDEX IF NOT EXISTS idx_meetups_conversation ON meetups(conversation_id);
CREATE INDEX IF NOT EXISTS idx_meetups_status ON meetups(status) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_meetups_buyer ON meetups(buyer_id);
CREATE INDEX IF NOT EXISTS idx_meetups_seller ON meetups(seller_id);

-- RLS: Only participants can see/create their meetups
ALTER TABLE meetups ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Participants can view meetups" ON meetups;
CREATE POLICY "Participants can view meetups"
    ON meetups FOR SELECT
    USING (auth.uid() = buyer_id OR auth.uid() = seller_id);

DROP POLICY IF EXISTS "Participants can create meetups" ON meetups;
CREATE POLICY "Participants can create meetups"
    ON meetups FOR INSERT
    WITH CHECK (auth.uid() = buyer_id OR auth.uid() = seller_id);

DROP POLICY IF EXISTS "Participants can update meetups" ON meetups;
CREATE POLICY "Participants can update meetups"
    ON meetups FOR UPDATE
    USING (auth.uid() = buyer_id OR auth.uid() = seller_id);


-- ============================================================
-- 3. REVIEWS TABLE
-- Bidirectional ratings after meetup confirmation
-- ============================================================
CREATE TABLE IF NOT EXISTS reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    meetup_id UUID NOT NULL REFERENCES meetups(id) ON DELETE CASCADE,
    reviewer_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reviewed_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    listing_id INTEGER,
    reviewer_role TEXT NOT NULL CHECK (reviewer_role IN ('buyer', 'seller')),
    rating INTEGER NOT NULL CHECK (rating >= 1 AND rating <= 5),
    review_text TEXT DEFAULT '',
    is_visible BOOLEAN NOT NULL DEFAULT false, -- blind until both submit or 48h
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT review_different_users CHECK (reviewer_id != reviewed_id),
    CONSTRAINT one_review_per_meetup_role UNIQUE (meetup_id, reviewer_role)
);

CREATE INDEX IF NOT EXISTS idx_reviews_meetup ON reviews(meetup_id);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewed ON reviews(reviewed_id, is_visible);
CREATE INDEX IF NOT EXISTS idx_reviews_reviewer ON reviews(reviewer_id);

-- RLS: Public can read visible reviews, participants can create
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Anyone can read visible reviews" ON reviews;
CREATE POLICY "Anyone can read visible reviews"
    ON reviews FOR SELECT
    USING (is_visible = true OR auth.uid() = reviewer_id);

DROP POLICY IF EXISTS "Participants can create reviews" ON reviews;
CREATE POLICY "Participants can create reviews"
    ON reviews FOR INSERT
    WITH CHECK (auth.uid() = reviewer_id);


-- ============================================================
-- 4. REPORTS TABLE
-- Report/block bad actors
-- ============================================================
CREATE TABLE IF NOT EXISTS reports (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reporter_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reported_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,                     -- scam, no_show, inappropriate, other
    details TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',   -- pending, reviewed, resolved, dismissed
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT report_different_users CHECK (reporter_id != reported_id)
);

CREATE INDEX IF NOT EXISTS idx_reports_reported ON reports(reported_id);
CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status) WHERE status = 'pending';

ALTER TABLE reports ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can create reports" ON reports;
CREATE POLICY "Users can create reports"
    ON reports FOR INSERT
    WITH CHECK (auth.uid() = reporter_id);

DROP POLICY IF EXISTS "Users can view own reports" ON reports;
CREATE POLICY "Users can view own reports"
    ON reports FOR SELECT
    USING (auth.uid() = reporter_id);


-- ============================================================
-- 5. BLOCKED USERS TABLE
-- ============================================================
CREATE TABLE IF NOT EXISTS blocked_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    blocker_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    blocked_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT block_different_users CHECK (blocker_id != blocked_id),
    CONSTRAINT unique_block UNIQUE (blocker_id, blocked_id)
);

ALTER TABLE blocked_users ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "Users can manage blocks" ON blocked_users;
CREATE POLICY "Users can manage blocks"
    ON blocked_users FOR ALL
    USING (auth.uid() = blocker_id);


-- ============================================================
-- 6. AUTO-REVEAL REVIEWS (both submitted or 48h elapsed)
-- Trigger: when a review is inserted, check if both exist
-- ============================================================
CREATE OR REPLACE FUNCTION reveal_reviews_if_complete()
RETURNS TRIGGER AS $$
DECLARE
    review_count INTEGER;
BEGIN
    -- Count reviews for this meetup
    SELECT COUNT(*) INTO review_count
    FROM reviews WHERE meetup_id = NEW.meetup_id;

    -- If both buyer and seller have submitted, reveal all
    IF review_count >= 2 THEN
        UPDATE reviews
        SET is_visible = true
        WHERE meetup_id = NEW.meetup_id AND is_visible = false;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_review_submitted ON reviews;
CREATE TRIGGER on_review_submitted
    AFTER INSERT ON reviews
    FOR EACH ROW EXECUTE FUNCTION reveal_reviews_if_complete();


-- ============================================================
-- 7. UPDATE PROFILE RATINGS ON REVIEW REVEAL
-- Recalculates seller/buyer averages when reviews become visible
-- ============================================================
CREATE OR REPLACE FUNCTION update_profile_ratings()
RETURNS TRIGGER AS $$
BEGIN
    -- Only fire when is_visible changes to true
    IF NEW.is_visible = true AND (OLD.is_visible = false OR OLD.is_visible IS NULL) THEN
        -- Update the reviewed user's ratings based on the reviewer's role
        IF NEW.reviewer_role = 'buyer' THEN
            -- Buyer reviewed seller → update seller's seller_rating
            UPDATE profiles SET
                seller_rating = COALESCE((
                    SELECT AVG(rating)::DOUBLE PRECISION FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND reviewer_role = 'buyer' AND is_visible = true
                ), 0),
                seller_review_count = (
                    SELECT COUNT(*) FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND reviewer_role = 'buyer' AND is_visible = true
                ),
                -- Also update legacy combined rating
                rating = COALESCE((
                    SELECT AVG(rating)::DOUBLE PRECISION FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND is_visible = true
                ), 0),
                reviews_count = (
                    SELECT COUNT(*) FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND is_visible = true
                )
            WHERE id = NEW.reviewed_id;
        ELSIF NEW.reviewer_role = 'seller' THEN
            -- Seller reviewed buyer → update buyer's buyer_rating
            UPDATE profiles SET
                buyer_rating = COALESCE((
                    SELECT AVG(rating)::DOUBLE PRECISION FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND reviewer_role = 'seller' AND is_visible = true
                ), 0),
                buyer_review_count = (
                    SELECT COUNT(*) FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND reviewer_role = 'seller' AND is_visible = true
                ),
                rating = COALESCE((
                    SELECT AVG(rating)::DOUBLE PRECISION FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND is_visible = true
                ), 0),
                reviews_count = (
                    SELECT COUNT(*) FROM reviews
                    WHERE reviewed_id = NEW.reviewed_id AND is_visible = true
                )
            WHERE id = NEW.reviewed_id;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_review_revealed ON reviews;
CREATE TRIGGER on_review_revealed
    AFTER UPDATE OF is_visible ON reviews
    FOR EACH ROW EXECUTE FUNCTION update_profile_ratings();


-- ============================================================
-- 8. UPDATE COMPLETED TRANSACTIONS ON MEETUP CONFIRM
-- ============================================================
CREATE OR REPLACE FUNCTION update_completed_transactions()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.status = 'confirmed' AND OLD.status = 'pending' THEN
        UPDATE profiles SET completed_transactions = completed_transactions + 1
        WHERE id = NEW.buyer_id OR id = NEW.seller_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS on_meetup_confirmed ON meetups;
CREATE TRIGGER on_meetup_confirmed
    AFTER UPDATE OF status ON meetups
    FOR EACH ROW EXECUTE FUNCTION update_completed_transactions();


-- ============================================================
-- 9. ENABLE REALTIME FOR MEETUPS
-- ============================================================
ALTER PUBLICATION supabase_realtime ADD TABLE meetups;
ALTER PUBLICATION supabase_realtime ADD TABLE reviews;
