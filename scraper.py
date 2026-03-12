"""
CollectLocal — FB Marketplace Scraper Pipeline
================================================
Runs via GitHub Actions (free) on a cron schedule.
1. Calls Apify to scrape FB Marketplace for Pokémon card listings
2. Uses AI vision (Gemini Flash) to identify exact card from listing photos
3. Fetches each listing's detail page for description + exact location (free)
4. Enriches with market prices from the Pokémon TCG API (free)
5. Pushes to Supabase (free tier)

Cost: ~$0/month (Apify free tier + Gemini 2.5 Flash Lite free tier 1,000 req/day)
"""

import os
import re
import math
import json
import time
import base64
import requests
from datetime import datetime, timezone
from urllib.parse import quote
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
HOME_LAT = float(os.environ.get("HOME_LAT", "32.9700"))
HOME_LNG = float(os.environ.get("HOME_LNG", "-96.7500"))
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Dallas, TX")
SEARCH_RADIUS = int(os.environ.get("SEARCH_RADIUS_MILES", "40"))
MAX_LISTING_AGE_HOURS = int(os.environ.get("MAX_LISTING_AGE_HOURS", "24"))

# Build city slug for FB Marketplace URLs (e.g., "Dallas, TX" -> "dallas")
CITY_SLUG = SEARCH_LOCATION.split(",")[0].strip().lower().replace(" ", "")

# Apify actor for FB Marketplace scraping (free $5/month credits)
# Official actor: cheapest at $2.60/1K listings, returns location but no description
# We supplement with a DIY detail page fetcher for descriptions (free)
# NOTE: Use tilde (~) not slash (/) in actor ID for the API URL
APIFY_ACTOR_ID = "apify~facebook-marketplace-scraper"

# Search terms that cover the Pokémon card market
SEARCH_QUERIES = [
        "PSA 10 pokemon",
        "PSA 9 pokemon",
        "BGS pokemon card",
        "pokemon slab",
        "pokemon booster box sealed",
        "pokemon ETB sealed",
        "charizard card",
        "umbreon card",
        "pokemon alt art",
]

# Supabase REST headers (service role for writes)
SUPABASE_HEADERS = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
}

# Headers for fetching FB listing detail pages
FB_HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
}


# ── Geocoding ──────────────────────────────────────────────────────
# Local lookup table for DFW-area cities (instant, free, 100% reliable)
# Covers the most common cities within the search radius.
# Nominatim API is used as a fallback for unknown locations.
CITY_COORDS = {
        # Dallas-Fort Worth Metroplex
        "dallas": (32.7767, -96.7970),
        "fort worth": (32.7555, -97.3308),
        "arlington": (32.7357, -97.1081),
        "plano": (33.0198, -96.6989),
        "irving": (32.8140, -96.9489),
        "garland": (32.9126, -96.6389),
        "frisc"o""":
         C(o3l3l.e1c5t0L7o,c a-l9 6— .F8B2 3M6a)r,k
         e t p l a"cmec kSicnrnaepye"r:  P(i3p3e.l1i9n7e2
         ,= =-=9=6=.=6=3=9=7=)=,=
         = = = = ="=g=r=a=n=d= =p=r=a=i=r=i=e="=:= =(=3=2=.=7=4=6=0=,= =-=9=6=.=9
         9R7u8n)s, 
         v i a   G"idteHnutbo nA"c:t i(o3n3s. 2(1f4r8e,e )- 9o7n. 1a3 3c0r)o,n schedule.
         1. Calls Apify to scrape FB Marketplace for Pokémon card listings
         2. Uses AI vision (Gemini Flash) to identify exact card from listing photos
         3. Fetches each listing's detail page for description + exact location (free)
         4. Enriches with market prices from the Pokémon TCG API (free)
         5. Pushes to Supabase (free tier)
         
         Cost: ~$0/month (Apify free tier + Gemini 2.5 Flash Lite free tier 1,000 req/day)
         """
    
    import os
    import re
    import math
    import json
    import time
    import base64
    import requests
    from datetime import datetime, timezone
    from urllib.parse import quote
    from dotenv import load_dotenv
    
    load_dotenv()
    
    # ── Config ──────────────────────────────────────────────────────
    APIFY_TOKEN = os.environ["APIFY_TOKEN"]
    SUPABASE_URL = os.environ["SUPABASE_URL"]
E_SERVICE_KEY"]
EBAY_CLIENT_ID = os.environ.get("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.environ.get("EBAY_CLIENT_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
HOME_LAT = float(os.environ.get("HOME_LAT", "32.9700"))
HOME_LNG = float(os.environ.get("HOME_LNG", "-96.7500"))
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Dallas, TX")
SEARCH_RADIUS = int(os.environ.get("SEARCH_RADIUS_MILES", "40"))
MAX_LISTING_AGE_HOURS = int(os.environ.get("MAX_LISTING_AGE_HOURS", "24"))

# Build city slug for FB Marketplace URLs (e.g., "Dallas, TX" -> "dallas")
CITY_SLUG = SEARCH_LOCATION.split(",")[0].strip().lower().replace(" ", "")

# Apify actor for FB Marketplace scraping (free $5/month credits)
# Official actor: cheapest at $2.60/1K listings, returns location but no description
# We supplement with a DIY detail page fetcher for descriptions (free)
# NOTE: Use tilde (~) not slash (/) in actor ID for the API URL
APIFY_ACTOR_ID = "apify~facebook-marketplace-scraper"

# Search terms that cover the Pokémon card market
SEARCH_QUERIES = [
    "PSA 10 pokemon",
        "PSA 9 pokemon",
            "BGS pokemon card",
                "pokemon slab",
                    "pokemon booster box sealed",
                        "pokemon ETB sealed",
                            "charizard card",
                                "umbreon card",
                                    "pokemon alt art",
                                    ]
                                    
                                    # Supabase REST headers (service role for writes)
                                    SUPABASE_HEADERS = {
                                        "apikey": SUPABASE_KEY,
                                            "Authorization": f"Bearer {SUPABASE_KEY}",
                                                "Content-Type": "application/json",
                                                    "Prefer": "resolution=merge-duplicates",
                                                    }
                                                    
                                                    # Headers for fetching FB listing detail pages
                                                    FB_HEADERS = {
                                                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                                                            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                                                "Accept-Language": "en-US,en;q=0.9",
                                                                    "Accept-Encoding": "gzip, deflate, br",
                                                                        "Cache-Control": "no-cache",
                                                                        }
                                                                        
                                                                        
                                                                        # ── Geocoding ──SUPABASE_KEY = os.environ["SUPABAS"" CollectLocal — FB Marketplace Scraper Pipeline ================================================ Runs via GitHub Actions (free) on a cron schedule. 1. Calls Apify to scrape FB Marketplace for Pokémon card listings 2. Uses AI vision (Gemini Flash) to identify exact card from listing photos 3. Fetches each listing's detail page for description + exact location (free) 4. Enriches with market prices from the Pokémon TCG API (free) 5. Pushes to Supabase (free tier)────────────────────────────────────────────────────
    # Local lookup table for DFW-area cities (instant, free, 100% reliable)
    # Covers the most common cities within the search radius.
    # Nominatim API is used as a fallback for unknown locations.
    CITY_COORDS = {
        # Dallas-Fort Worth Metroplex
        "dallas": (32.7767, -96.7970),
        "fort worth": (32.7555, -97.3308),
        "arlington": (32.7357, -97.1081),
        "plano": (33.0198, -96.6989),
        "irving": (32.8140, -96.9489),
        "garland": (32.9126, -96.6389),
        "frisco": (33.1507, -96.8236),
        "mckinney": (33.1972, -96.6397),
        "grand prairie": (32.7460, -96.9978),
        "denton": (33.2148, -97.1330),
