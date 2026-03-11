"""
CollectLocal — FB Marketplace Scraper Pipeline
================================================
Runs via GitHub Actions (free) on a cron schedule.
1. Calls Apify to scrape FB Marketplace for Pokémon card listings
2. Fetches each listing's detail page for description + exact location (free)
3. Enriches with market prices from the Pokémon TCG API (free)
4. Pushes to Supabase (free tier)

Cost: $0/month on Apify free tier ($5 credits = ~1,000 listings/month)
"""

import os
import re
import math
import json
import time
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
HOME_LAT = float(os.environ.get("HOME_LAT", "32.9700"))
HOME_LNG = float(os.environ.get("HOME_LNG", "-96.7500"))
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Dallas, TX")
SEARCH_RADIUS = int(os.environ.get("SEARCH_RADIUS_MILES", "40"))

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


# ── Geocoding ───────────────────────────────────────────────────
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
    "denton": (33.2148, -97.1331),
    "mesquite": (32.7668, -96.5992),
    "carrollton": (32.9537, -96.8903),
    "lewisville": (33.0462, -96.9942),
    "allen": (33.1032, -96.6706),
    "flower mound": (33.0146, -97.0969),
    "richardson": (32.9483, -96.7299),
    "mansfield": (32.5632, -97.1417),
    "rowlett": (32.9029, -96.5639),
    "the colony": (33.0901, -96.8861),
    "wylie": (33.0151, -96.5389),
    "rockwall": (32.9313, -96.4597),
    "sachse": (32.9762, -96.5953),
    "murphy": (33.0151, -96.6128),
    "prosper": (33.2362, -96.8011),
    "celina": (33.3246, -96.7847),
    "anna": (33.3490, -96.5486),
    "forney": (32.7482, -96.4719),
    "midlothian": (32.4824, -96.9945),
    "waxahachie": (32.3866, -96.8483),
    "weatherford": (32.7593, -97.7973),
    "burleson": (32.5421, -97.3208),
    "cleburne": (32.3476, -97.3867),
    "grapevine": (32.9343, -97.0781),
    "southlake": (32.9412, -97.1342),
    "keller": (32.9347, -97.2517),
    "colleyville": (32.8810, -97.1550),
    "bedford": (32.8440, -97.1350),
    "euless": (32.8371, -97.0820),
    "hurst": (32.8235, -97.1706),
    "north richland hills": (32.8343, -97.2289),
    "desoto": (32.5899, -96.8570),
    "cedar hill": (32.5885, -96.9561),
    "duncanville": (32.6518, -96.9083),
    "lancaster": (32.5921, -96.7561),
    "coppell": (32.9546, -97.0150),
    "farmers branch": (32.9263, -96.8961),
    "addison": (32.9612, -96.8292),
    "university park": (32.8503, -96.8000),
    "highland park": (32.8335, -96.7918),
    "heath": (32.8360, -96.4722),
    "terrell": (32.7360, -96.2753),
    "kaufman": (32.5893, -96.3092),
    "ennis": (32.3293, -96.6253),
    "corsicana": (32.0954, -96.4689),
    "tyler": (32.3513, -95.3011),
    "sherman": (33.6357, -96.6089),
    "denison": (33.7557, -96.5367),
    "gainesville": (33.6259, -97.1336),
    # Other major TX cities (for occasional far-away listings)
    "austin": (30.2672, -97.7431),
    "houston": (29.7604, -95.3698),
    "san antonio": (29.4241, -98.4936),
    "el paso": (31.7619, -106.4850),
    "lubbock": (33.5779, -101.8552),
    "amarillo": (35.2220, -101.8313),
    "waco": (31.5493, -97.1467),
    "killeen": (31.1171, -97.7278),
    "abilene": (32.4487, -99.7331),
    "corpus christi": (27.8006, -97.3964),
    "laredo": (27.5036, -99.5076),
    "brownsville": (25.9017, -97.4975),
    "college station": (30.6280, -96.3344),
    "beaumont": (30.0802, -94.1266),
    "odessa": (31.8457, -102.3676),
    "midland": (31.9973, -102.0779),
    "san angelo": (31.4638, -100.4370),
    "oklahoma city": (35.4676, -97.5164),
    "norman": (35.2226, -97.4395),
}

_geocode_cache = {}


def geocode_location(location_text: str) -> dict:
    """
    Convert a location string (e.g., "Dallas, TX") to lat/lng coordinates.

    Strategy (fastest to slowest):
    1. Local lookup table for known DFW-area cities (instant)
    2. In-memory cache for previously geocoded locations (instant)
    3. Nominatim API as fallback for unknown locations (1 req/sec)

    Returns: {"lat": float, "lng": float} or {"lat": 0, "lng": 0} on failure.
    """
    if not location_text:
        return {"lat": HOME_LAT, "lng": HOME_LNG}

    # Normalize: "Dallas, TX" -> "dallas"
    city_key = location_text.split(",")[0].strip().lower()

    # 1. Check local lookup table (instant)
    if city_key in CITY_COORDS:
        lat, lng = CITY_COORDS[city_key]
        return {"lat": lat, "lng": lng}

    # 2. Check cache
    cache_key = location_text.strip().lower()
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # 3. Nominatim API fallback (free, 1 req/sec limit)
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": location_text,
                "format": "json",
                "limit": 1,
                "countrycodes": "us",
            },
            headers={"User-Agent": "CollectLocal/1.0 (pokemon-card-tracker)"},
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            if data:
                result = {
                    "lat": float(data[0]["lat"]),
                    "lng": float(data[0]["lon"]),
                }
                _geocode_cache[cache_key] = result
                print(f"    📍 Geocoded '{location_text}' → {result['lat']:.4f}, {result['lng']:.4f}")
                time.sleep(1.1)  # Nominatim rate limit
                return result

    except Exception as e:
        print(f"    ⚠️ Geocode failed for '{location_text}': {e}")

    # Cache the failure so we don't retry
    result = {"lat": 0, "lng": 0}
    _geocode_cache[cache_key] = result
    return result


def haversine_miles(lat1, lng1, lat2, lng2):
    """Calculate distance between two coordinates in miles."""
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def parse_grade(title: str) -> str:
    """Extract grading info from listing title."""
    title_upper = title.upper()

    sealed_keywords = ["BOOSTER BOX", "ETB", "ELITE TRAINER", "SEALED", "BOOSTER BUNDLE", "COLLECTION BOX"]
    if any(kw in title_upper for kw in sealed_keywords):
        return "Sealed"

    grade_patterns = [
        r"PSA\s*(\d+(?:\.\d+)?)",
        r"BGS\s*(\d+(?:\.\d+)?)",
        r"CGC\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in grade_patterns:
        match = re.search(pattern, title_upper)
        if match:
            company = pattern[:3].upper()
            return f"{company} {match.group(1)}"

    return "Raw"


def parse_card_name(title: str) -> dict:
    """Try to extract card name, set, and number from the listing title."""
    clean = re.sub(r"\b(PSA|BGS|CGC)\s*\d+(\.\d+)?\b", "", title, flags=re.IGNORECASE)
    clean = re.sub(r"\b(GEM MINT|MINT|NM|LP|MP|HP)\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(POKEMON|POKÉMON|CARD|TCG|SLAB)\b", "", clean, flags=re.IGNORECASE)
    clean = clean.strip(" -–—·|/,")

    number_match = re.search(r"(\d{1,3}/\d{1,3})", clean)
    number = number_match.group(1) if number_match else ""
    if number:
        clean = clean.replace(number, "").strip()

    return {
        "name": clean.strip() or title[:50],
        "number": number,
    }


def time_ago(timestamp_str: str) -> str:
    """Convert ISO timestamp to human-readable 'X ago' format."""
    try:
        posted = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - posted
        minutes = int(delta.total_seconds() / 60)
        if minutes < 60:
            return f"{minutes} min ago"
        hours = minutes // 60
        if hours < 24:
            return f"{hours} hr{'s' if hours > 1 else ''} ago"
        days = hours // 24
        return f"{days} day{'s' if days > 1 else ''} ago"
    except Exception:
        return "Recently"


# ── Detail Page Fetcher (free, no Apify credits) ────────────────

def fetch_listing_details(listing_url: str) -> dict:
    """
    Fetch a FB Marketplace listing page and extract description + location details.

    Facebook embeds listing data in the page as JSON within script tags.
    We look for:
    1. og:description meta tag — contains the listing description
    2. JSON-LD or embedded relay data — contains lat/lng and full description
    3. meta tags for location text

    Returns: {"description": str, "latitude": float, "longitude": float, "location_text": str}
    """
    result = {"description": "", "latitude": 0, "longitude": 0, "location_text": ""}

    if not listing_url:
        return result

    try:
        resp = requests.get(listing_url, headers=FB_HEADERS, timeout=15, allow_redirects=True)

        if resp.status_code != 200:
            return result

        html = resp.text

        # ── Method 1: Extract og:description meta tag ──
        # FB puts listing description in <meta property="og:description" content="...">
        og_desc_match = re.search(
            r'<meta\s+property="og:description"\s+content="([^"]*)"',
            html, re.IGNORECASE
        )
        if not og_desc_match:
            og_desc_match = re.search(
                r'<meta\s+content="([^"]*)"\s+property="og:description"',
                html, re.IGNORECASE
            )
        if og_desc_match:
            desc = og_desc_match.group(1)
            # Unescape HTML entities
            desc = desc.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            desc = desc.replace("&#x27;", "'").replace("&quot;", '"')
            result["description"] = desc.strip()

        # ── Method 2: Extract from embedded JSON data ──
        # FB embeds structured data in script tags containing listing details
        # Look for patterns like "marketplace_listing_title" nearby "description"
        json_patterns = [
            # Pattern: "redacted_description":{"text":"..."}
            r'"redacted_description"\s*:\s*\{\s*"text"\s*:\s*"([^"]*)"',
            # Pattern: "description":{"text":"..."}
            r'"description"\s*:\s*\{\s*"text"\s*:\s*"([^"]*)"',
            # Pattern: plain "description":"..."
            r'"description"\s*:\s*"([^"]{10,500})"',
        ]

        for pattern in json_patterns:
            match = re.search(pattern, html)
            if match:
                desc = match.group(1)
                # Unescape JSON unicode sequences
                try:
                    desc = desc.encode().decode('unicode_escape')
                except Exception:
                    pass
                if len(desc) > len(result["description"]):
                    result["description"] = desc.strip()
                break

        # ── Method 3: Extract latitude/longitude from embedded data ──
        lat_match = re.search(r'"latitude"\s*:\s*(-?\d+\.?\d*)', html)
        lng_match = re.search(r'"longitude"\s*:\s*(-?\d+\.?\d*)', html)
        if lat_match and lng_match:
            try:
                result["latitude"] = float(lat_match.group(1))
                result["longitude"] = float(lng_match.group(1))
            except (ValueError, TypeError):
                pass

        # ── Method 4: Extract location_text from embedded data ──
        loc_match = re.search(r'"location_text"\s*:\s*"([^"]*)"', html)
        if loc_match:
            result["location_text"] = loc_match.group(1).strip()

    except requests.exceptions.Timeout:
        print(f"    ⏱️ Timeout fetching detail page")
    except Exception as e:
        print(f"    ⚠️ Detail fetch failed: {e}")

    return result


def enrich_listings_with_details(listings: list[dict]) -> list[dict]:
    """
    Enrich a batch of listings by fetching their detail pages.
    Adds description, lat/lng, and refined location text where available.
    Includes rate limiting to avoid getting blocked by Facebook.
    """
    enriched_count = 0
    total = len(listings)

    print(f"\n  📄 Enriching {total} listings with detail page data...")

    for i, listing in enumerate(listings):
        listing_url = listing.get("listing_url", "")
        if not listing_url:
            continue

        # Skip if we already have a description (from backfill or previous run)
        if listing.get("description"):
            continue

        details = fetch_listing_details(listing_url)

        # Update description if we got one
        if details["description"]:
            listing["description"] = details["description"][:500]
            enriched_count += 1

        # Update lat/lng if we got coordinates and didn't have them
        if details["latitude"] and details["longitude"]:
            if not listing.get("lat") or listing["lat"] == 0:
                listing["lat"] = details["latitude"]
                listing["lng"] = details["longitude"]
                # Recalculate distance with actual coordinates
                listing["distance"] = int(haversine_miles(
                    HOME_LAT, HOME_LNG, details["latitude"], details["longitude"]
                ))

        # Update location text if we got a better one
        if details["location_text"] and listing.get("location") == SEARCH_LOCATION:
            listing["location"] = details["location_text"][:100]

        # Rate limit: 1-2 second delay between requests to avoid blocking
        if i < total - 1:
            time.sleep(1.5)

        # Progress update every 10 listings
        if (i + 1) % 10 == 0:
            print(f"    📄 Enriched {i + 1}/{total} listings ({enriched_count} descriptions found)")

    print(f"  ✅ Enrichment complete: {enriched_count}/{total} descriptions found")
    return listings


# ── Pokémon TCG API (free, no auth) ────────────────────────────

TCG_API_BASE = "https://api.pokemontcg.io/v2"
_tcg_cache = {}


def lookup_market_price(card_name: str, set_name: str = "") -> dict | None:
    """
    Look up market price from the Pokémon TCG API (completely free).
    Returns {"market_price": float, "market_source": str, "image_url": str, "set": str, "number": str}
    """
    cache_key = f"{card_name}|{set_name}"
    if cache_key in _tcg_cache:
        return _tcg_cache[cache_key]

    try:
        q_parts = [f'name:"{card_name}"']
        if set_name:
            q_parts.append(f'set.name:"{set_name}"')

        resp = requests.get(
            f"{TCG_API_BASE}/cards",
            params={"q": " ".join(q_parts), "pageSize": 1, "orderBy": "-set.releaseDate"},
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json().get("data", [])
            if data:
                card = data[0]
                prices = card.get("tcgplayer", {}).get("prices", {})

                market_price = None
                for price_type in ["holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil"]:
                    if price_type in prices and prices[price_type].get("market"):
                        market_price = prices[price_type]["market"]
                        break

                if market_price:
                    result = {
                        "market_price": market_price,
                        "market_source": "tcgplayer",
                        "image_url": card.get("images", {}).get("large", ""),
                        "set": card.get("set", {}).get("name", ""),
                        "number": f"{card.get('number', '')}/{card.get('set', {}).get('printedTotal', '')}",
                    }
                    _tcg_cache[cache_key] = result
                    return result

    except Exception as e:
        print(f"  ⚠️ TCG API lookup failed for '{card_name}': {e}")

    _tcg_cache[cache_key] = None
    return None


# ── eBay Browse API (active listings for market price) ─────────

_ebay_token = {"access_token": "", "expires_at": 0}
_ebay_cache = {}


def get_ebay_token() -> str:
    """
    Get an eBay OAuth application access token using client credentials grant.
    Tokens last ~2 hours (7200s). We cache and refresh as needed.
    """
    if not EBAY_CLIENT_ID or not EBAY_CLIENT_SECRET:
        return ""

    now = time.time()
    if _ebay_token["access_token"] and now < _ebay_token["expires_at"] - 60:
        return _ebay_token["access_token"]

    try:
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            },
            auth=(EBAY_CLIENT_ID, EBAY_CLIENT_SECRET),
            timeout=10,
        )

        if resp.status_code == 200:
            data = resp.json()
            _ebay_token["access_token"] = data["access_token"]
            _ebay_token["expires_at"] = now + data.get("expires_in", 7200)
            print("  🔑 eBay OAuth token acquired")
            return _ebay_token["access_token"]
        else:
            print(f"  ❌ eBay token request failed: {resp.status_code} — {resp.text[:200]}")
            return ""

    except Exception as e:
        print(f"  ❌ eBay token error: {e}")
        return ""


def lookup_ebay_prices(search_term: str, category_id: str = "183454") -> dict | None:
    """
    Search eBay Browse API for the lowest Buy It Now listing price.
    Uses the lowest BIN as the real-time market price — the actual floor
    price a buyer can purchase the card for right now on eBay.

    Also returns the URL of the lowest listing and a search URL so users
    can tap through to see all matching eBay listings.

    eBay category 183454 = Pokémon Individual Cards (CCG)
    eBay category 183456 = Pokémon Sealed Products (CCG)

    Returns: {"ebay_price": float, "ebay_listing_url": str, "ebay_search_url": str,
              "ebay_listing_title": str, "ebay_num_results": int}
    or None if no results.
    """
    cache_key = f"{search_term}|{category_id}"
    if cache_key in _ebay_cache:
        return _ebay_cache[cache_key]

    token = get_ebay_token()
    if not token:
        return None

    try:
        # Detect if this is a sealed product search
        sealed_keywords = ["booster box", "etb", "elite trainer", "sealed", "booster bundle"]
        if any(kw in search_term.lower() for kw in sealed_keywords):
            category_id = "183456"  # Sealed products

        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item_summary/search",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
                "Content-Type": "application/json",
            },
            params={
                "q": search_term,
                "category_ids": category_id,
                "filter": "buyingOptions:{FIXED_PRICE},conditions:{NEW}",
                "sort": "price",
                "limit": 5,
            },
            timeout=15,
        )

        if resp.status_code == 200:
            data = resp.json()
            items = data.get("itemSummaries", [])
            total = data.get("total", 0)

            if not items:
                _ebay_cache[cache_key] = None
                return None

            # Find the lowest priced item (results are sorted by price asc)
            lowest_item = None
            lowest_price = None
            for item in items:
                price_obj = item.get("price", {})
                try:
                    p = float(price_obj.get("value", 0))
                    if p > 0 and (lowest_price is None or p < lowest_price):
                        lowest_price = p
                        lowest_item = item
                except (ValueError, TypeError):
                    pass

            if not lowest_price or not lowest_item:
                _ebay_cache[cache_key] = None
                return None

            # Build the eBay web search URL so users can tap through
            encoded_q = quote(search_term)
            ebay_search_url = (
                f"https://www.ebay.com/sch/i.html?_nkw={encoded_q}"
                f"&_sacat={category_id}&LH_BIN=1&_sop=15"
            )

            # Get the listing URL from the API response
            listing_url = lowest_item.get("itemWebUrl", "")
            listing_title = lowest_item.get("title", "")[:100]

            result = {
                "ebay_price": round(lowest_price, 2),
                "ebay_listing_url": listing_url,
                "ebay_search_url": ebay_search_url,
                "ebay_listing_title": listing_title,
                "ebay_num_results": total,
            }
            _ebay_cache[cache_key] = result
            return result

        elif resp.status_code == 429:
            print("  ⏳ eBay rate limited, skipping...")
            return None
        else:
            print(f"  ⚠️ eBay search failed: {resp.status_code} — {resp.text[:200]}")
            return None

    except Exception as e:
        print(f"  ⚠️ eBay lookup failed for '{search_term}': {e}")
        return None


# ── Combined Market Price Lookup ───────────────────────────────

def lookup_combined_market_price(card_name: str, grade: str = "", set_name: str = "") -> dict:
    """
    Look up market price from both Pokémon TCG API and eBay Browse API.
    eBay price = lowest current Buy It Now listing (real floor price).
    TCGPlayer price = market average from pokemontcg.io API.
    """
    # 1. Try Pokémon TCG API first (free, fast, reliable)
    tcg_data = lookup_market_price(card_name, set_name)

    # 2. Try eBay Browse API — lowest BIN as market price
    ebay_data = None
    if EBAY_CLIENT_ID:
        # Build a search term that includes grade for more accurate pricing
        ebay_search = card_name
        if grade and grade not in ("Raw", "Sealed"):
            ebay_search = f"{grade} {card_name}"
        elif grade == "Sealed":
            ebay_search = card_name  # Category will switch to sealed

        ebay_data = lookup_ebay_prices(ebay_search)

    # 3. Combine results
    result = {
        "market_price": None,
        "market_source": "",
        "image_url": "",
        "set": "",
        "number": "",
        "ebay_price": None,
        "ebay_url": None,
    }

    if tcg_data:
        result["market_price"] = tcg_data["market_price"]
        result["market_source"] = "tcgplayer"
        result["image_url"] = tcg_data["image_url"]
        result["set"] = tcg_data["set"]
        result["number"] = tcg_data["number"]

    if ebay_data:
        result["ebay_price"] = ebay_data["ebay_price"]
        result["ebay_url"] = ebay_data["ebay_search_url"]

        # If no TCG price, use eBay lowest BIN as primary market price
        if not result["market_price"]:
            result["market_price"] = ebay_data["ebay_price"]
            result["market_source"] = "ebay"

    return result


# ── Apify Scraper ──────────────────────────────────────────────

def run_apify_scraper(search_query: str, max_items: int = 25) -> list:
    """
    Run Apify Facebook Marketplace scraper for a single search query.
    Uses startUrls with actual FB Marketplace search URLs.
    Free tier: ~$0.005/result, $5 free credits/month = ~1,000 results.
    """
    print(f"  🔍 Scraping FB Marketplace for: '{search_query}'")

    encoded_query = quote(search_query)
    fb_url = f"https://www.facebook.com/marketplace/{CITY_SLUG}/search?query={encoded_query}"
    print(f"  📎 FB URL: {fb_url}")

    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN}
    payload = {
        "startUrls": [{"url": fb_url}],
        "maxItems": max_items,
    }

    try:
        resp = requests.post(url, json=payload, params=params, timeout=300)
        print(f"  📡 Apify response status: {resp.status_code}")

        if resp.status_code in (200, 201):
            items = resp.json()
            if isinstance(items, list):
                print(f"  ✅ Got {len(items)} results")
                if items:
                    print(f"  🔑 First item keys: {list(items[0].keys())[:10]}")
                return items
            else:
                print(f"  ⚠️ Unexpected response type: {type(items)}")
                print(f"  ⚠️ Response preview: {str(items)[:300]}")
                return []
        else:
            print(f"  ❌ Apify returned {resp.status_code}: {resp.text[:300]}")
            return []
    except Exception as e:
        print(f"  ❌ Apify error: {e}")
        return []


def process_apify_result(item: dict) -> dict | None:
    """
    Convert a raw Apify result into our listing format.

    Official Apify FB Marketplace scraper returns fields like:
    - marketplace_listing_title (not "title")
    - listing_price: {amount, formatted_amount} (nested object)
    - location: {reverse_geocode: {city, state}} (nested)
    - primary_listing_photo: {image: {uri}} (nested)
    - listingUrl, id, is_sold, is_live
    - marketplace_listing_seller (may be null)

    Description and lat/lng are NOT returned by the official actor.
    These are enriched later by fetch_listing_details().
    """
    try:
        # ── Title ──
        title = (
            item.get("marketplace_listing_title")
            or item.get("title")
            or item.get("name")
            or ""
        ).strip()

        if not title:
            return None

        # ── Skip sold/inactive items ──
        if item.get("is_sold"):
            return None

        # ── Price ──
        price = 0
        listing_price = item.get("listing_price")
        if isinstance(listing_price, dict):
            price_str = listing_price.get("amount", "0")
            try:
                price = float(price_str)
            except (ValueError, TypeError):
                price = 0
        else:
            price_str = str(item.get("price", "0"))
            price_clean = re.sub(r"[^\d.]", "", price_str)
            price = float(price_clean) if price_clean else 0

        # $1 is common on FB Marketplace (= "message for price")
        if price <= 0 or price > 50000:
            return None

        # ── Parse card info from title ──
        card_info = parse_card_name(title)
        grade = parse_grade(title)

        # ── Location (from Apify search results) ──
        location_text = ""
        location_obj = item.get("location")
        if isinstance(location_obj, dict):
            geo = location_obj.get("reverse_geocode", {})
            city = geo.get("city", "")
            state = geo.get("state", "")
            if city and state:
                location_text = f"{city}, {state}"
            elif city:
                location_text = city
            if not location_text:
                city_page = geo.get("city_page", {})
                location_text = city_page.get("display_name", "")
        elif isinstance(location_obj, str):
            location_text = location_obj

        if not location_text:
            location_text = item.get("address", SEARCH_LOCATION)

        # Geocode city/state to get lat/lng for distance calculation
        coords = geocode_location(location_text)
        item_lat = coords["lat"]
        item_lng = coords["lng"]

        # Calculate distance from user's home location
        distance = 0
        if item_lat and item_lng:
            distance = int(haversine_miles(HOME_LAT, HOME_LNG, item_lat, item_lng))
            # Filter out listings too far away (3x search radius)
            if distance > SEARCH_RADIUS * 3:
                return None

        # ── Image ──
        image_url = ""
        photo_obj = item.get("primary_listing_photo")
        if isinstance(photo_obj, dict):
            image_obj = photo_obj.get("image", {})
            if isinstance(image_obj, dict):
                image_url = image_obj.get("uri", "")
            if not image_url:
                image_url = photo_obj.get("photo_image_url", "")
        if not image_url:
            image_url = item.get("image") or item.get("imageUrl") or ""

        # ── Market price lookup (TCGPlayer + eBay lowest BIN) ──
        market_data = lookup_combined_market_price(card_info["name"], grade)
        market_price = market_data["market_price"] or price
        market_source = market_data["market_source"] or "tcgplayer"
        tcg_image = market_data["image_url"] or ""
        set_name = market_data["set"] or ""
        card_number = market_data["number"] or card_info["number"]
        ebay_price = market_data["ebay_price"]
        ebay_url = market_data["ebay_url"]

        # Prefer TCG API image (higher quality, consistent) over FB photo
        if tcg_image:
            image_url = tcg_image

        # ── Seller ──
        seller_name = "Unknown"
        seller_obj = item.get("marketplace_listing_seller")
        if isinstance(seller_obj, dict):
            seller_name = seller_obj.get("name", "Unknown")
        elif isinstance(seller_obj, str) and seller_obj:
            seller_name = seller_obj
        if seller_name == "Unknown":
            s = item.get("sellerName") or item.get("seller")
            if isinstance(s, dict):
                seller_name = s.get("name", "Unknown")
            elif isinstance(s, str) and s:
                seller_name = s

        seller_rating = 0

        # ── External ID + URL ──
        external_id = str(item.get("id") or item.get("url") or item.get("listingUrl") or f"{title}-{price}")
        listing_url = item.get("listingUrl") or item.get("url") or item.get("link") or ""

        # ── Posted time ──
        posted_raw = item.get("timestamp") or item.get("date") or item.get("postedAt") or ""
        posted = time_ago(posted_raw) if posted_raw else "Recently"

        return {
            "external_id": external_id[:200],
            "name": card_info["name"][:100],
            "card_set": set_name[:100],
            "card_number": card_number[:20],
            "grade": grade,
            "price": price,
            "market_price": market_price,
            "market_source": market_source,
            "image_url": image_url,
            "marketplace": "facebook",
            "location": (location_text[:100]) or SEARCH_LOCATION,
            "distance": distance,
            "posted": posted,
            "seller": seller_name[:50],
            "seller_rating": seller_rating,
            "lat": item_lat,
            "lng": item_lng,
            "description": "",  # Enriched later by detail fetcher
            "listing_url": listing_url,
            "is_active": True,
            "ebay_price": ebay_price,
            "ebay_url": ebay_url,
        }

    except Exception as e:
        print(f"  ⚠️ Failed to process item: {e}")
        return None


# ── Supabase Dedup Check ──────────────────────────────────────

def fetch_existing_ids() -> set:
    """
    Fetch all external_ids from Supabase that are still active.
    Used to skip re-processing listings we already have.
    This saves Apify credits, eBay API calls, and TCG API calls.
    """
    url = f"{SUPABASE_URL}/rest/v1/listings"
    params = {
        "select": "external_id",
        "is_active": "eq.true",
        "limit": 5000,
    }
    try:
        resp = requests.get(url, params=params, headers=SUPABASE_HEADERS, timeout=30)
        if resp.status_code == 200:
            rows = resp.json()
            ids = {r["external_id"] for r in rows if r.get("external_id")}
            print(f"  📋 Loaded {len(ids)} existing listing IDs from Supabase")
            return ids
        else:
            print(f"  ⚠️ Could not fetch existing IDs: {resp.status_code}")
            return set()
    except Exception as e:
        print(f"  ⚠️ Could not fetch existing IDs: {e}")
        return set()


# ── Supabase Upsert ───────────────────────────────────────────

def upsert_listings(listings: list[dict]) -> int:
    """Upsert listings to Supabase. Returns count of upserted rows."""
    if not listings:
        return 0

    url = f"{SUPABASE_URL}/rest/v1/listings"

    total = 0
    for i in range(0, len(listings), 50):
        batch = listings[i : i + 50]
        resp = requests.post(
            url,
            json=batch,
            headers={
                **SUPABASE_HEADERS,
                "Prefer": "resolution=merge-duplicates",
            },
        )
        if resp.status_code in (200, 201):
            total += len(batch)
        else:
            print(f"  ❌ Supabase upsert failed: {resp.status_code} — {resp.text[:200]}")

    return total


def log_scrape_run(listings_found: int, listings_new: int, status: str, error: str = ""):
    """Log scrape run to scrape_runs table for monitoring."""
    url = f"{SUPABASE_URL}/rest/v1/scrape_runs"
    requests.post(
        url,
        json={
            "listings_found": listings_found,
            "listings_new": listings_new,
            "status": status,
            "error_message": error[:500] if error else None,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        },
        headers=SUPABASE_HEADERS,
    )


# ── Backfill from Apify Dataset ──────────────────────────────

def backfill_from_dataset(dataset_id: str):
    """
    One-time: pull items from an existing Apify dataset and push to Supabase.
    Useful when scraper ran but process_apify_result had bugs.
    Usage: BACKFILL_DATASET=<id> python scraper.py
    """
    print(f"📥 Backfilling from Apify dataset: {dataset_id}")

    offset = 0
    limit = 100
    all_listings = []
    seen_ids = set()

    while True:
        url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
        resp = requests.get(
            url,
            params={"token": APIFY_TOKEN, "limit": limit, "offset": offset, "format": "json"},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"  ❌ Dataset fetch failed: {resp.status_code}")
            break

        items = resp.json()
        if not items:
            break

        print(f"  📦 Got {len(items)} items (offset={offset})")

        for item in items:
            processed = process_apify_result(item)
            if processed and processed["external_id"] not in seen_ids:
                seen_ids.add(processed["external_id"])
                all_listings.append(processed)

        offset += limit
        if len(items) < limit:
            break

    print(f"\n📊 Processed {len(all_listings)} unique listings from dataset")

    # Enrich with detail page data (descriptions + lat/lng)
    if all_listings:
        enrich_detail = os.environ.get("ENRICH_DETAILS", "true").lower() == "true"
        if enrich_detail:
            all_listings = enrich_listings_with_details(all_listings)

        upserted = upsert_listings(all_listings)
        print(f"✅ Upserted {upserted} listings to Supabase")
        log_scrape_run(len(all_listings), upserted, "success", f"Backfill from dataset {dataset_id}")
    else:
        print("⚠️ No valid listings found in dataset")


# ── Main Pipeline ──────────────────────────────────────────────

def extract_external_id(item: dict) -> str | None:
    """
    Quickly extract the external_id from a raw Apify item WITHOUT
    doing any expensive processing (no eBay/TCG API calls).
    Returns None if the item is clearly invalid.
    """
    title = (
        item.get("marketplace_listing_title")
        or item.get("title")
        or item.get("name")
        or ""
    ).strip()
    if not title:
        return None
    if item.get("is_sold"):
        return None

    price = 0
    listing_price = item.get("listing_price")
    if isinstance(listing_price, dict):
        try:
            price = float(listing_price.get("amount", "0"))
        except (ValueError, TypeError):
            price = 0
    else:
        price_str = str(item.get("price", "0"))
        price_clean = re.sub(r"[^\d.]", "", price_str)
        price = float(price_clean) if price_clean else 0

    if price <= 0 or price > 50000:
        return None

    ext_id = str(item.get("id") or item.get("url") or item.get("listingUrl") or f"{title}-{price}")
    return ext_id[:200]


def select_queries_for_run(queries: list[str], max_per_run: int = 3) -> list[str]:
    """
    Rotate which queries run on each invocation.
    Uses the current date to deterministically pick a subset,
    so each day a different set of queries runs.
    Over 3 days all 9 queries will have run.
    """
    import hashlib
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_hash = int(hashlib.md5(today.encode()).hexdigest(), 16)
    start = (day_hash % len(queries))

    selected = []
    for i in range(max_per_run):
        idx = (start + i) % len(queries)
        selected.append(queries[idx])

    return selected


def main():
    # Check for backfill mode
    backfill_ds = os.environ.get("BACKFILL_DATASET", "")
    if backfill_ds:
        backfill_from_dataset(backfill_ds)
        return

    # Whether to enrich listings with detail page data (default: true)
    enrich_detail = os.environ.get("ENRICH_DETAILS", "true").lower() == "true"

    # Query rotation: run a subset per invocation to save Apify credits
    # Set RUN_ALL_QUERIES=true to override and run all 9
    run_all = os.environ.get("RUN_ALL_QUERIES", "false").lower() == "true"
    queries_per_run = int(os.environ.get("QUERIES_PER_RUN", "3"))

    if run_all:
        active_queries = SEARCH_QUERIES
    else:
        active_queries = select_queries_for_run(SEARCH_QUERIES, max_per_run=queries_per_run)

    print("🚀 CollectLocal Scraper Starting...")
    print(f"   Location: {SEARCH_LOCATION} (radius: {SEARCH_RADIUS} mi)")
    print(f"   City slug: {CITY_SLUG}")
    print(f"   Actor ID: {APIFY_ACTOR_ID}")
    print(f"   Queries this run: {len(active_queries)} of {len(SEARCH_QUERIES)}")
    print(f"   Active queries: {active_queries}")
    print(f"   Detail enrichment: {'ON' if enrich_detail else 'OFF'}")
    print(f"   eBay pricing: {'ON' if EBAY_CLIENT_ID else 'OFF (set EBAY_CLIENT_ID to enable)'}")
    print()

    # ── Pre-fetch existing IDs to skip duplicates ──
    existing_ids = fetch_existing_ids()

    all_listings = []
    seen_ids = set()
    skipped_existing = 0

    for query in active_queries:
        raw_items = run_apify_scraper(query, max_items=25)

        for item in raw_items:
            # Quick ID extraction (no API calls) to check for duplicates
            ext_id = extract_external_id(item)
            if ext_id is None:
                continue
            if ext_id in seen_ids:
                continue
            if ext_id in existing_ids:
                skipped_existing += 1
                seen_ids.add(ext_id)
                continue

            # Only now do the expensive processing (eBay + TCG API calls)
            processed = process_apify_result(item)
            if processed:
                seen_ids.add(processed["external_id"])
                all_listings.append(processed)

        # Small delay between queries to be polite
        time.sleep(2)

    print(f"\n📊 Processed {len(all_listings)} new listings")
    print(f"   ⏭️  Skipped {skipped_existing} already-in-Supabase listings (saved API calls)")

    # Enrich with detail page data (descriptions + lat/lng) — free!
    if all_listings and enrich_detail:
        all_listings = enrich_listings_with_details(all_listings)

    if all_listings:
        upserted = upsert_listings(all_listings)
        print(f"✅ Upserted {upserted} listings to Supabase")
        log_scrape_run(len(all_listings), upserted, "success")
    else:
        print("⚠️ No new listings to upsert")
        log_scrape_run(0, 0, "success", f"No new listings (skipped {skipped_existing} existing)")

    print("🏁 Done!")


if __name__ == "__main__":
    main()
