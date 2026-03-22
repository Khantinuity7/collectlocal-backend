-- Add is_sharing_location column to profiles table
-- Controls whether a user's approximate location is visible to other collectors on the map

ALTER TABLE profiles
ADD COLUMN IF NOT EXISTS is_sharing_location BOOLEAN NOT NULL DEFAULT true;

-- Optional: index for filtering discoverable collectors
CREATE INDEX IF NOT EXISTS idx_profiles_sharing_location
ON profiles (is_sharing_location) WHERE is_sharing_location = true;
