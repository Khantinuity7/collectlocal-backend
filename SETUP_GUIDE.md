# CollectLocal — Backend Setup Guide

## Architecture (Total Cost: $0/month)

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐     ┌────────────┐
│   GitHub     │────▶│    Apify     │────▶│   Pokémon    │     │   iOS App  │
│   Actions    │     │  FB Scraper  │     │   TCG API    │     │            │
│  (free cron) │     │ (free $5/mo) │     │   (free)     │     │            │
└──────┬───────┘     └──────────────┘     └──────────────┘     └─────┬──────┘
       │                                                              │
       │              ┌──────────────┐                                │
       └─────────────▶│   Supabase   │◀───────────────────────────────┘
                      │  (free tier) │
                      │  Postgres DB │
                      │  + REST API  │
                      └──────────────┘
```

**Free tier limits (more than enough to start):**
- Apify: $5 free credits/mo = ~1,000 listings scraped
- Supabase: 500MB DB, 50K rows, 2GB bandwidth
- GitHub Actions: 2,000 min/month (private), unlimited (public)
- Pokémon TCG API: Free, no auth required

---

## Step 1: Create a Supabase Project (5 min)

1. Go to [supabase.com](https://supabase.com) and sign up (free)
2. Click **New Project** → name it `collectlocal`
3. Set a database password (save it somewhere safe)
4. Select your region (closest to your users)
5. Wait for the project to be created

**Get your keys:**
- Go to **Settings → API**
- Copy the **Project URL** (e.g., `https://abc123.supabase.co`)
- Copy the **anon/public** key (for the iOS app)
- Copy the **service_role** key (for the backend scraper — keep this SECRET)

**Create the database tables:**
- Go to **SQL Editor** in the Supabase dashboard
- Paste the contents of `setup_supabase.sql` and click **Run**

---

## Step 2: Create an Apify Account (3 min)

1. Go to [apify.com](https://apify.com) and sign up (free)
2. You get **$5 free credits/month** — enough for ~1,000 listings
3. Go to **Settings → Integrations** → copy your **API Token**
4. Optional: visit the [FB Marketplace Scraper](https://apify.com/apify/facebook-marketplace-scraper) page to test it manually

---

## Step 3: Set Up the Backend Repo (5 min)

1. Create a new **private** GitHub repository (e.g., `collectlocal-backend`)
2. Push the `CollectLocal-Backend/` folder contents to it
3. Go to **Settings → Secrets and variables → Actions**
4. Add these repository secrets:

| Secret Name | Value |
|---|---|
| `APIFY_TOKEN` | Your Apify API token |
| `SUPABASE_URL` | `https://your-project-id.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Your Supabase service_role key |
| `HOME_LAT` | Your home latitude (e.g., `32.9700`) |
| `HOME_LNG` | Your home longitude (e.g., `-96.7500`) |
| `SEARCH_LOCATION` | Your city (e.g., `Dallas, TX`) |
| `SEARCH_RADIUS_MILES` | Search radius (e.g., `40`) |

5. Go to **Actions** tab → click **Scrape Marketplace** → **Run workflow** to test it

The scraper will automatically run every 6 hours via the cron schedule.

---

## Step 4: Connect the iOS App (2 min)

Open `CollectLocal/Services/SupabaseConfig.swift` and replace the placeholder values:

```swift
enum SupabaseConfig {
    static let projectURL = "https://YOUR_ACTUAL_PROJECT_ID.supabase.co"
    static let anonKey = "YOUR_ACTUAL_ANON_KEY"
}
```

Use the **anon/public** key here (NOT the service_role key). The anon key is safe
to embed in the iOS app — Row Level Security on the database ensures it can only
read, never write.

That's it. The app will:
1. Try to fetch live listings from Supabase on launch
2. Fall back to MockData if the API isn't configured or is unreachable
3. Show a "Live" or "Demo" badge based on the data source

---

## Step 5: Test the Full Pipeline

1. Run the GitHub Action manually (**Actions → Run workflow**)
2. Check the Supabase **Table Editor** to see listings appear
3. Build and run the iOS app — it should show live data

---

## Cost Breakdown at Scale

| Monthly Users | Apify | Supabase | GitHub Actions | Total |
|---|---|---|---|---|
| 0–500 (launch) | $0 (free tier) | $0 (free tier) | $0 (free tier) | **$0/mo** |
| 500–2,000 | $49 (Starter) | $0 (free tier) | $0 | **$49/mo** |
| 2,000–10,000 | $49 | $25 (Pro) | $0 | **$74/mo** |
| 10,000+ | $149 | $25 | $0 | **$174/mo** |

With AdMob at ~$1-3 eCPM for a utility app, you'd need roughly 50-175 daily active
users viewing a few ads to cover the $0 tier. The app should be cashflow positive
from day one since costs are literally zero.

---

## Customizing Search Queries

Edit `SEARCH_QUERIES` in `scraper.py` to target your niche:

```python
SEARCH_QUERIES = [
    "PSA 10 pokemon",
    "pokemon booster box sealed",
    # Add more as needed
]
```

More queries = more Apify credits used. With the free $5/month:
- 9 queries × 25 results × 4 runs/day × 30 days = ~1,000 unique listings/month
- This is roughly 33 new listings per day in your area

---

## Monitoring

Check scraper health in Supabase:
```sql
SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 10;
```

Check listing freshness:
```sql
SELECT COUNT(*), marketplace, is_active
FROM listings
GROUP BY marketplace, is_active;
```
