-- ============================================================
-- Refer & Earn Program — Option B (card-on-file model)
-- ============================================================
-- Referral counts as "successful" when friend signs up for
-- free trial with a payment method on file.
-- Max 10 successful referrals per calendar year per user.
-- Rewards are non-transferable, expire 12 months after earning.
-- ============================================================

-- MARK: Referral codes — one per user
CREATE TABLE IF NOT EXISTS referral_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    code TEXT NOT NULL UNIQUE,
    share_url TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT unique_user_referral_code UNIQUE (user_id)
);

CREATE INDEX idx_referral_codes_user ON referral_codes(user_id);
CREATE INDEX idx_referral_codes_code ON referral_codes(code);

-- MARK: Individual referral tracking
CREATE TABLE IF NOT EXISTS referrals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    referrer_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    referee_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    referee_username TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'successful', 'converted', 'expired')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    -- Anti-abuse: one referral per referee
    CONSTRAINT unique_referee UNIQUE (referee_id),
    -- Prevent self-referrals
    CONSTRAINT no_self_referral CHECK (referrer_id != referee_id)
);

CREATE INDEX idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX idx_referrals_referee ON referrals(referee_id);
CREATE INDEX idx_referrals_status ON referrals(status);

-- MARK: Reward ledger — tracks earned free months
CREATE TABLE IF NOT EXISTS referral_rewards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    tier TEXT NOT NULL CHECK (tier IN ('bronze', 'silver', 'gold', 'platinum')),
    reward_months INT NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    earned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    redeemed_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + INTERVAL '12 months'),
    is_redeemed BOOLEAN NOT NULL DEFAULT false
);

CREATE INDEX idx_referral_rewards_user ON referral_rewards(user_id);

-- MARK: Anti-abuse tracking
CREATE TABLE IF NOT EXISTS referral_abuse_signals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    referral_id UUID NOT NULL REFERENCES referrals(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL CHECK (signal_type IN (
        'ip_match', 'device_match', 'payment_match', 'velocity', 'prepaid_card'
    )),
    details JSONB DEFAULT '{}',
    flagged_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved BOOLEAN NOT NULL DEFAULT false,
    resolved_at TIMESTAMPTZ
);

CREATE INDEX idx_abuse_signals_referral ON referral_abuse_signals(referral_id);

-- ============================================================
-- RLS Policies
-- ============================================================

ALTER TABLE referral_codes ENABLE ROW LEVEL SECURITY;
ALTER TABLE referrals ENABLE ROW LEVEL SECURITY;
ALTER TABLE referral_rewards ENABLE ROW LEVEL SECURITY;
ALTER TABLE referral_abuse_signals ENABLE ROW LEVEL SECURITY;

-- Referral codes: users can read their own, service_role can write
CREATE POLICY "Users can read own referral code"
    ON referral_codes FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Service role manages referral codes"
    ON referral_codes FOR ALL
    USING (auth.role() = 'service_role');

-- Referrals: users can read referrals where they are referrer
CREATE POLICY "Users can read own referrals"
    ON referrals FOR SELECT
    USING (auth.uid() = referrer_id);

CREATE POLICY "Service role manages referrals"
    ON referrals FOR ALL
    USING (auth.role() = 'service_role');

-- Authenticated users can insert referrals (when friend signs up with their code)
CREATE POLICY "Authenticated users can create referrals"
    ON referrals FOR INSERT
    WITH CHECK (auth.uid() = referrer_id);

-- Referral rewards: users can read their own
CREATE POLICY "Users can read own rewards"
    ON referral_rewards FOR SELECT
    USING (auth.uid() = user_id);

CREATE POLICY "Service role manages rewards"
    ON referral_rewards FOR ALL
    USING (auth.role() = 'service_role');

-- Abuse signals: service_role only
CREATE POLICY "Service role manages abuse signals"
    ON referral_abuse_signals FOR ALL
    USING (auth.role() = 'service_role');

-- ============================================================
-- Helper RPC: Count successful referrals for a user this year
-- ============================================================
CREATE OR REPLACE FUNCTION count_successful_referrals(p_user_id UUID)
RETURNS INT
LANGUAGE sql
STABLE
AS $$
    SELECT COUNT(*)::INT
    FROM referrals
    WHERE referrer_id = p_user_id
      AND status IN ('successful', 'converted')
      AND created_at >= date_trunc('year', now());
$$;

-- ============================================================
-- Helper RPC: Look up referrer by referral code
-- ============================================================
CREATE OR REPLACE FUNCTION lookup_referral_code(p_code TEXT)
RETURNS TABLE(user_id UUID, code TEXT)
LANGUAGE sql
STABLE
AS $$
    SELECT user_id, code
    FROM referral_codes
    WHERE code = p_code
    LIMIT 1;
$$;
