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


# ── AI Vision Card Identification (Gemini 2.0 Flash) ────────────
_vision_cache = {}
_vision_stats = {"calls": 0, "hits": 0, "errors": 0}


def identify_card_from_image(image_url: str, title_hint: str = "") -> dict | None:
    """
    Use Google Gemini 2.0 Flash vision to identify a Pokémon card from its listing photo.

    Sends the image to Gemini Flash (cheapest high-quality vision model) which reads:
    - Card name from the artwork/text on the card
    - Set name and symbol
    - Card number (e.g., "25/165")
    - Whether it's graded (PSA/BGS/CGC slab) and the grade
    - Whether it's a sealed product

    Cost: ~$0.0001 per image (Gemini 2.5 Flash Lite: $0.075/M input, ~1600 tokens per image)
    At 100 listings/day = ~$0.01/day = ~$0.30/month
    Google AI free tier: 1,000 requests/day = essentially $0/month at our volume.

    Returns: {"name": str, "set": str, "number": str, "grade": str, "card_type": str}
    or None if identification fails.
    """
    if not GEMINI_API_KEY:
        return None

    if not image_url:
        return None

    # Check cache (same image = same card)
    if image_url in _vision_cache:
        _vision_stats["hits"] += 1
        return _vision_cache[image_url]

    _vision_stats["calls"] += 1

    try:
        # Download the image and convert to base64
        # (Gemini API accepts inline base64 images — FB image URLs may be auth-gated)
        img_resp = requests.get(image_url, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (compatible; CollectLocal/1.0)"
        })
        if img_resp.status_code != 200:
            _vision_stats["errors"] += 1
            return None

        # Determine media type
        content_type = img_resp.headers.get("Content-Type", "image/jpeg")
        if "png" in content_type:
            media_type = "image/png"
        elif "webp" in content_type:
            media_type = "image/webp"
        elif "gif" in content_type:
            media_type = "image/gif"
        else:
            media_type = "image/jpeg"

        img_b64 = base64.b64encode(img_resp.content).decode("utf-8")

        # Build the prompt
        prompt = """Look at this image of a Pokémon card listing. Identify the card(s) shown.

Return ONLY a JSON object with these fields:
{
  "name": "Card name (e.g., 'Charizard VMAX', 'Pikachu', 'Umbreon VMAX Alt Art')",
  "set": "Set name if visible (e.g., 'Evolving Skies', 'Base Set', 'Obsidian Flames')",
  "number": "Card number if visible (e.g., '25/165', '4/102')",
  "grade": "Grade if in a slab (e.g., 'PSA 10', 'BGS 9.5', 'CGC 9') or 'Raw' if ungraded or 'Sealed' if sealed product",
  "card_type": "One of: single, lot, sealed, accessories, not_pokemon",
  "confidence": "high, medium, or low"
}

Rules:
- If multiple cards are shown, identify the most prominent/valuable one
- If it's a lot (multiple cards bundled), set card_type to "lot" and name to a description like "Mixed PSA Slabs Lot (5 cards)"
- If it's a sealed product (booster box, ETB, etc.), set card_type to "sealed" and name to the product name
- If you can't identify the specific card, use the best guess from visible text/artwork
- If it's not a Pokémon card at all, set card_type to "not_pokemon"
- Return ONLY the JSON, no other text"""

        if title_hint:
            prompt += f"\n\nThe listing title is: \"{title_hint}\" — use this as a hint but trust the image over the title."

        # Call Gemini 2.5 Flash Lite vision API
        resp = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={GEMINI_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [
                    {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": media_type,
                                    "data": img_b64,
                                }
                            },
                            {
                                "text": prompt,
                            },
                        ]
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": 300,
                    "temperature": 0.1,
                },
            },
            timeout=30,
        )

        if resp.status_code != 200:
            print(f"    ⚠️ Gemini API error: {resp.status_code} — {resp.text[:200]}")
            _vision_stats["errors"] += 1
            _vision_cache[image_url] = None
            return None

        # Parse Gemini's response
        resp_json = resp.json()
        candidates = resp_json.get("candidates", [])
        if not candidates:
            _vision_stats["errors"] += 1
            _vision_cache[image_url] = None
            return None

        response_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()

        # Extract JSON from response (Gemini sometimes wraps in ```json blocks)
        json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
        if not json_match:
            _vision_stats["errors"] += 1
            _vision_cache[image_url] = None
            return None

        card_data = json.loads(json_match.group())

        # Validate required fields
        if not card_data.get("name"):
            _vision_cache[image_url] = None
            return None

        result = {
            "name": card_data.get("name", "")[:100],
            "set": card_data.get("set", "")[:100],
            "number": card_data.get("number", "")[:20],
            "grade": card_data.get("grade", "Raw"),
            "card_type": card_data.get("card_type", "single"),
            "confidence": card_data.get("confidence", "low"),
        }

        _vision_cache[image_url] = result
        print(f"    🤖 AI identified: {result['name']} ({result['set']}) [{result['grade']}] — {result['confidence']} confidence")
        return result

    except json.JSONDecodeError as e:
        print(f"    ⚠️ AI vision JSON parse error: {e}")
        _vision_stats["errors"] += 1
    except requests.exceptions.Timeout:
        print(f"    ⏱️ AI vision timeout")
        _vision_stats["errors"] += 1
    except Exception as e:
        print(f"    ⚠️ AI vision error: {e}")
        _vision_stats["errors"] += 1

    _vision_cache[image_url] = None
    return None


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


def is_listing_recent(item: dict) -> bool:
    """
    Check if a listing was posted within MAX_LISTING_AGE_HOURS.

    Uses the raw timestamp from Apify when available (most accurate).
    Falls back to parsing relative time strings like "2 hours ago" from
    the Apify data if no ISO timestamp is found.

    Returns True if the listing is recent enough to keep, False to skip.
    """
    max_age_minutes = MAX_LISTING_AGE_HOURS * 60

    # ── Try raw ISO timestamp first ──
    raw_ts = item.get("timestamp") or item.get("date") or item.get("postedAt") or ""
    if raw_ts:
        try:
            posted = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
            delta = datetime.now(timezone.utc) - posted
            age_minutes = delta.total_seconds() / 60
            return age_minutes <= max_age_minutes
        except Exception:
            pass

    # ── Fallback: parse relative time strings from Apify ──
    # Apify sometimes returns "2 hours ago", "3 days ago", etc.
    relative = (
        item.get("time") or item.get("postedTime") or item.get("relativeTime") or ""
    )
    if relative:
        relative_lower = relative.lower().strip()
        try:
            # "just now", "a moment ago"
            if "just" in relative_lower or "moment" in relative_lower or "now" in relative_lower:
                return True
            # "X minutes/min ago"
            m = re.search(r"(\d+)\s*(?:min|minute)", relative_lower)
            if m:
                return int(m.group(1)) <= max_age_minutes
            # "X hours/hr ago"  or  "an hour ago"
            m = re.search(r"(\d+)\s*(?:hr|hour)", relative_lower)
            if m:
                return int(m.group(1)) * 60 <= max_age_minutes
            if "an hour" in relative_lower or "1 hour" in relative_lower:
                return 60 <= max_age_minutes
            # "X days/day ago"
            m = re.search(r"(\d+)\s*day", relative_lower)
            if m:
                return int(m.group(1)) * 1440 <= max_age_minutes
            if "yesterday" in relative_lower or "a day" in relative_lower:
                return 1440 <= max_age_minutes
            # "X weeks/week ago"
            m = re.search(r"(\d+)\s*week", relative_lower)
            if m:
                return int(m.group(1)) * 10080 <= max_age_minutes
            if "a week" in relative_lower:
                return 10080 <= max_age_minutes
        except Exception:
            pass

    # If we can't determine age, keep the listing (benefit of the doubt)
    return True


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
    result = {"description": "", "latitude": 0, "longitude": 0, "location_text": "", "photos": []}

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

        # ── Method 5: Extract all listing photos from embedded data ──
        # FB embeds photo URIs in the page JSON data in several formats
        photos = []
        seen_urls = set()

        # Pattern: "uri":"https://scontent..." inside listing photo objects
        # Look for image URIs near "listing_photo" context
        photo_uri_matches = re.findall(
            r'"uri"\s*:\s*"(https://(?:scontent|external)[^"]+)"', html
        )
        for uri in photo_uri_matches:
            # Unescape JSON-encoded URLs (e.g., \/ -> /)
            clean_uri = uri.replace("\\/", "/")
            if clean_uri not in seen_urls and len(photos) < 10:
                seen_urls.add(clean_uri)
                photos.append(clean_uri)

        # Also look for og:image meta tags (may have additional photos)
        og_img_matches = re.findall(
            r'<meta\s+(?:property="og:image"\s+content|content)="(https://[^"]+)"[^>]*(?:property="og:image")?',
            html, re.IGNORECASE
        )
        for uri in og_img_matches:
            if uri not in seen_urls and len(photos) < 10:
                seen_urls.add(uri)
                photos.append(uri)

        result["photos"] = photos

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

        # Merge detail page photos into listing_photos (dedup)
        if details.get("photos"):
            existing = set(listing.get("listing_photos", []))
            for photo_url in details["photos"]:
                if photo_url not in existing and len(listing.get("listing_photos", [])) < 10:
                    listing.setdefault("listing_photos", []).append(photo_url)
                    existing.add(photo_url)

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


# ── TCGTracking Open API (free TCGPlayer + Manapool pricing, no auth) ──

TCGTRACK_BASE = "https://tcgtracking.com/tcgapi/v1"
# TCGPlayer category IDs (same across TCGTracking & TCGPlayer)
TCGTRACK_CATEGORIES = {
    "pokemon": 3,
    "onepiece": 73,      # One Piece Card Game
    "lorcana": 86,
    "yugioh": 2,
    "magic": 1,
}
_tcgtrack_sets_cache = {}      # {category_id: {set_name_lower: set_data}}
_tcgtrack_products_cache = {}  # {(category_id, set_id): {clean_name_lower: product}}
_tcgtrack_pricing_cache = {}   # {(category_id, set_id): {product_id: {subtype: price_data}}}
_tcgtrack_skus_cache = {}      # {(category_id, set_id): {product_id: {sku_id: sku_data}}}


def _tcgtrack_load_sets(category_id: int) -> dict:
    """Load all sets for a category. Returns {set_name_lower: set_data}."""
    if category_id in _tcgtrack_sets_cache:
        return _tcgtrack_sets_cache[category_id]

    try:
        resp = requests.get(f"{TCGTRACK_BASE}/{category_id}/sets", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            sets = {}
            for s in data.get("sets", []):
                sets[s["name"].lower()] = s
            _tcgtrack_sets_cache[category_id] = sets
            print(f"  📦 TCGTracking: Loaded {len(sets)} sets for category {category_id}")
            return sets
    except Exception as e:
        print(f"  ⚠️ TCGTracking sets load failed for category {category_id}: {e}")

    _tcgtrack_sets_cache[category_id] = {}
    return {}


def _tcgtrack_load_products(category_id: int, set_id: int) -> dict:
    """Load all products for a set. Returns {clean_name_lower: product}."""
    cache_key = (category_id, set_id)
    if cache_key in _tcgtrack_products_cache:
        return _tcgtrack_products_cache[cache_key]

    try:
        resp = requests.get(f"{TCGTRACK_BASE}/{category_id}/sets/{set_id}", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            products = {}
            for p in data.get("products", []):
                clean = (p.get("clean_name") or p["name"]).lower()
                products[clean] = p
                products[p["name"].lower()] = p
            _tcgtrack_products_cache[cache_key] = products
            print(f"  🃏 TCGTracking: Loaded {len(data.get('products', []))} products in set {set_id}")
            return products
    except Exception as e:
        print(f"  ⚠️ TCGTracking products load failed for set {set_id}: {e}")

    _tcgtrack_products_cache[cache_key] = {}
    return {}


def _tcgtrack_load_pricing(category_id: int, set_id: int) -> dict:
    """Load pricing for all products in a set. Returns {product_id_str: {"tcg": {...}, "manapool": {...}}}."""
    cache_key = (category_id, set_id)
    if cache_key in _tcgtrack_pricing_cache:
        return _tcgtrack_pricing_cache[cache_key]

    try:
        resp = requests.get(f"{TCGTRACK_BASE}/{category_id}/sets/{set_id}/pricing", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            prices = data.get("prices", {})
            _tcgtrack_pricing_cache[cache_key] = prices
            print(f"  💰 TCGTracking: Loaded pricing for {len(prices)} products in set {set_id}")
            return prices
    except Exception as e:
        print(f"  ⚠️ TCGTracking pricing load failed for set {set_id}: {e}")

    _tcgtrack_pricing_cache[cache_key] = {}
    return {}


def _tcgtrack_load_skus(category_id: int, set_id: int) -> dict:
    """Load SKU-level pricing (by condition/variant/language). Returns {product_id_str: {sku_id: sku}}."""
    cache_key = (category_id, set_id)
    if cache_key in _tcgtrack_skus_cache:
        return _tcgtrack_skus_cache[cache_key]

    try:
        resp = requests.get(f"{TCGTRACK_BASE}/{category_id}/sets/{set_id}/skus", timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            products = data.get("products", {})
            _tcgtrack_skus_cache[cache_key] = products
            sku_count = data.get("sku_count", 0)
            print(f"  🏷️ TCGTracking: Loaded {sku_count} SKUs for set {set_id}")
            return products
    except Exception as e:
        print(f"  ⚠️ TCGTracking SKUs load failed for set {set_id}: {e}")

    _tcgtrack_skus_cache[cache_key] = {}
    return {}


def _tcgtrack_search_sets(category_id: int, query: str) -> list:
    """Search sets by name within a category. Returns list of matching sets."""
    try:
        resp = requests.get(
            f"{TCGTRACK_BASE}/{category_id}/search",
            params={"q": query},
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("sets", data.get("results", []))
    except Exception as e:
        print(f"  ⚠️ TCGTracking search failed for '{query}': {e}")
    return []


def lookup_tcgtrack_price(card_name: str, set_name: str = "", tcg: str = "pokemon") -> dict | None:
    """
    Look up market price from TCGTracking Open API (free, no auth, Cloudflare CDN).
    Returns {"market_price": float, "low_price": float, "market_source": str,
             "tcgplayer_url": str, "sub_type": str, "skus": [...]}

    TCGTracking serves TCGPlayer + Manapool pricing data, updated daily at 8 AM EST.
    Includes SKU-level pricing by condition (NM/LP/MP/HP/DMG), variant, and language.
    55 games, 423K+ products, 6.9M+ SKUs. No rate limits. CORS enabled.
    """
    category_id = TCGTRACK_CATEGORIES.get(tcg.lower())
    if not category_id:
        return None

    # 1. Find the matching set
    sets = _tcgtrack_load_sets(category_id)
    if not sets:
        return None

    set_data = None
    set_id = None

    if set_name:
        set_lower = set_name.lower()
        # Try exact match first
        if set_lower in sets:
            set_data = sets[set_lower]
        else:
            # Substring match
            for sname, sdata in sets.items():
                if set_lower in sname or sname in set_lower:
                    set_data = sdata
                    break

        # Last resort: use search endpoint
        if not set_data:
            search_results = _tcgtrack_search_sets(category_id, set_name)
            if search_results:
                set_data = search_results[0]

    if not set_data:
        print(f"  ℹ️ TCGTracking: No matching set for '{set_name}' in {tcg}")
        return None

    set_id = set_data.get("id") or set_data.get("set_id") or set_data.get("groupId")
    if not set_id:
        return None

    # 2. Load products and find the card
    products = _tcgtrack_load_products(category_id, set_id)
    if not products:
        return None

    card_lower = card_name.lower()
    product = products.get(card_lower)

    # Fuzzy match
    if not product:
        for pname, pdata in products.items():
            if card_lower in pname or pname in card_lower:
                product = pdata
                break

    if not product:
        print(f"  ℹ️ TCGTracking: No product match for '{card_name}' in set '{set_name}'")
        return None

    product_id = product["id"]
    product_id_str = str(product_id)

    # 3. Get pricing for this product
    pricing = _tcgtrack_load_pricing(category_id, set_id)
    product_pricing = pricing.get(product_id_str, {})
    tcg_prices = product_pricing.get("tcg", {})

    if not tcg_prices:
        return None

    # Pick the best subtype price (prefer Holofoil > Reverse Holofoil > Normal > 1st Edition)
    preferred_order = ["Holofoil", "Reverse Holofoil", "Normal", "1st Edition Holofoil",
                       "1st Edition Normal", "Unlimited Holofoil", "Unlimited Normal", "Foil"]
    best_subtype = None
    best_price_data = None

    for pref in preferred_order:
        if pref in tcg_prices and tcg_prices[pref].get("market"):
            best_subtype = pref
            best_price_data = tcg_prices[pref]
            break

    # Fallback: any subtype with a market price
    if not best_price_data:
        for subtype, price_data in tcg_prices.items():
            if price_data.get("market"):
                best_subtype = subtype
                best_price_data = price_data
                break

    if not best_price_data:
        return None

    # 4. Get SKU-level pricing (condition breakdown) if available
    skus_data = _tcgtrack_load_skus(category_id, set_id)
    product_skus = skus_data.get(product_id_str, {})
    sku_list = []
    for sku_id, sku in product_skus.items():
        sku_list.append({
            "sku_id": sku_id,
            "condition": sku.get("cnd", ""),       # NM, LP, MP, HP, DMG
            "variant": sku.get("var", ""),          # N=Normal, F=Foil
            "language": sku.get("lng", ""),          # EN, JP, etc.
            "market_price": sku.get("mkt"),
            "low_price": sku.get("low"),
            "high_price": sku.get("hi"),
            "listing_count": sku.get("cnt", 0),
        })

    # Get Manapool pricing as additional data
    manapool = product_pricing.get("manapool", {})

    result = {
        "market_price": best_price_data["market"],
        "low_price": best_price_data.get("low"),
        "market_source": "tcgplayer",
        "sub_type": best_subtype,
        "tcgplayer_url": product.get("tcgplayer_url", product.get("url", "")),
        "manapool_url": product.get("manapool_url", ""),
        "image_url": product.get("image_url", product.get("imageUrl", "")),
        "set": set_data.get("name", set_name),
        "number": product.get("number", ""),
        "rarity": product.get("rarity", ""),
        # All subtype prices (e.g. Normal, Holofoil, Reverse Holofoil)
        "all_subtypes": {st: {"market": p.get("market"), "low": p.get("low")} for st, p in tcg_prices.items()},
        # Manapool prices (alternative marketplace)
        "manapool_prices": manapool,
        # SKU-level pricing by condition/variant/language
        "skus": sku_list,
    }

    print(f"  ✅ TCGTracking: {card_name} = ${best_price_data['market']:.2f} ({best_subtype})"
          f" | {len(sku_list)} SKUs")
    return result


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

def lookup_combined_market_price(card_name: str, grade: str = "", set_name: str = "", card_number: str = "", tcg: str = "pokemon") -> dict:
    """
    Look up market price from TCGTracking (primary), Pokémon TCG API (fallback), and eBay.
    Priority: TCGTracking → pokemontcg.io → eBay lowest BIN.

    TCGTracking provides real TCGPlayer + Manapool prices for free (no API key needed).
    Updated daily 8 AM EST. 55 games, 423K+ products, 6.9M+ SKUs. No rate limits.

    card_number: e.g. "209/187" — included in eBay search for precise matching.
    tcg: which card game — "pokemon", "onepiece", "lorcana", "yugioh", "magic"
    """
    # 1. Try TCGTracking first (free, no auth, real TCGPlayer + Manapool data, SKU-level)
    tcgtrack_data = lookup_tcgtrack_price(card_name, set_name, tcg=tcg)

    # 2. Fallback: Try Pokémon TCG API (only works for Pokemon)
    tcg_data = None
    if not tcgtrack_data and tcg == "pokemon":
        tcg_data = lookup_market_price(card_name, set_name)

    # 3. Try eBay Browse API — lowest BIN as secondary price
    ebay_data = None
    if EBAY_CLIENT_ID:
        ebay_search = card_name
        if card_number:
            ebay_search = f"{card_name} {card_number}"
        if grade and grade not in ("Raw", "Sealed"):
            ebay_search = f"{grade} {ebay_search}"
        elif grade == "Sealed":
            ebay_search = card_name

        ebay_data = lookup_ebay_prices(ebay_search)

    # 4. Combine results — TCGCSV is primary, eBay is secondary
    result = {
        "market_price": None,
        "market_source": "",
        "low_price": None,
        "mid_price": None,
        "high_price": None,
        "image_url": "",
        "set": "",
        "number": "",
        "tcgplayer_url": "",
        "ebay_price": None,
        "ebay_url": None,
        "ebay_listing_url": None,
        "ebay_listing_title": None,
        "ebay_num_results": 0,
    }

    # TCGTracking has the best data (real TCGPlayer prices + SKU-level condition pricing)
    if tcgtrack_data:
        result["market_price"] = tcgtrack_data["market_price"]
        result["market_source"] = "tcgplayer"
        result["low_price"] = tcgtrack_data.get("low_price")
        result["image_url"] = tcgtrack_data.get("image_url", "")
        result["set"] = tcgtrack_data.get("set", "")
        result["number"] = tcgtrack_data.get("number", "")
        result["tcgplayer_url"] = tcgtrack_data.get("tcgplayer_url", "")
        result["sub_type"] = tcgtrack_data.get("sub_type", "")
        result["rarity"] = tcgtrack_data.get("rarity", "")
        result["all_subtypes"] = tcgtrack_data.get("all_subtypes", {})
        result["manapool_prices"] = tcgtrack_data.get("manapool_prices", {})
        result["skus"] = tcgtrack_data.get("skus", [])
    elif tcg_data:
        # Fallback to pokemontcg.io
        result["market_price"] = tcg_data["market_price"]
        result["market_source"] = "tcgplayer"
        result["image_url"] = tcg_data["image_url"]
        result["set"] = tcg_data["set"]
        result["number"] = tcg_data["number"]

    if ebay_data:
        result["ebay_price"] = ebay_data["ebay_price"]
        result["ebay_url"] = ebay_data["ebay_search_url"]
        result["ebay_listing_url"] = ebay_data.get("ebay_listing_url")
        result["ebay_listing_title"] = ebay_data.get("ebay_listing_title")
        result["ebay_num_results"] = ebay_data.get("ebay_num_results", 0)

        # If no TCG price at all, use eBay lowest BIN as primary
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

        # ── Parse card info from title (fallback) ──
        card_info = parse_card_name(title)
        grade = parse_grade(title)

        # ── Images ── (extract ALL listing photos, not just the primary)
        image_url = ""
        listing_photos = []

        # 1. Primary listing photo (always grab first)
        photo_obj = item.get("primary_listing_photo")
        if isinstance(photo_obj, dict):
            image_obj = photo_obj.get("image", {})
            if isinstance(image_obj, dict):
                image_url = image_obj.get("uri", "")
            if not image_url:
                image_url = photo_obj.get("photo_image_url", "")
        if not image_url:
            image_url = item.get("image") or item.get("imageUrl") or ""

        if image_url:
            listing_photos.append(image_url)

        # 2. All listing photos from Apify (listing_photos / photos array)
        #    Apify FB Marketplace actor returns these as arrays of photo objects
        for photos_key in ("listing_photos", "photos", "images", "marketplace_listing_photos"):
            photos_arr = item.get(photos_key)
            if isinstance(photos_arr, list):
                for photo in photos_arr:
                    photo_uri = ""
                    if isinstance(photo, dict):
                        # Nested: {image: {uri: "..."}} or {photo_image_url: "..."}
                        img_inner = photo.get("image", {})
                        if isinstance(img_inner, dict):
                            photo_uri = img_inner.get("uri", "")
                        if not photo_uri:
                            photo_uri = (
                                photo.get("photo_image_url")
                                or photo.get("uri")
                                or photo.get("url")
                                or photo.get("src")
                                or ""
                            )
                    elif isinstance(photo, str) and photo.startswith("http"):
                        photo_uri = photo
                    if photo_uri and photo_uri not in listing_photos:
                        listing_photos.append(photo_uri)

        # Cap at 10 photos to avoid bloat
        listing_photos = listing_photos[:10]

        # ── AI Vision Card Identification ──
        # Send the listing photo to Gemini Flash to identify the actual card.
        # This replaces the unreliable parse_card_name() for titles like
        # "PSA Slabs for Sale" where the title gives zero useful info.
        ai_card = identify_card_from_image(image_url, title_hint=title)
        if ai_card and ai_card.get("card_type") != "not_pokemon":
            # AI successfully identified the card — use its data
            if ai_card["confidence"] in ("high", "medium"):
                card_info["name"] = ai_card["name"]
                if ai_card.get("number"):
                    card_info["number"] = ai_card["number"]
            # AI grade overrides title-parsed grade if it saw a slab
            if ai_card.get("grade") and ai_card["grade"] not in ("Raw", ""):
                grade = ai_card["grade"]
            elif ai_card.get("card_type") == "sealed":
                grade = "Sealed"
            # Use AI-detected set name
            ai_set_name = ai_card.get("set", "")
        else:
            ai_set_name = ""

        # Skip non-Pokemon items that AI detected
        if ai_card and ai_card.get("card_type") == "not_pokemon":
            return None

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

        # ── Market price lookup (TCGPlayer + eBay lowest BIN) ──
        # Pass AI-detected set name if available for more accurate lookups
        market_data = lookup_combined_market_price(
            card_info["name"], grade, set_name=ai_set_name
        )
        market_price = market_data["market_price"] or price
        market_source = market_data["market_source"] or "tcgplayer"
        tcg_image = market_data["image_url"] or ""
        set_name = market_data["set"] or ai_set_name or ""
        card_number = market_data["number"] or card_info["number"]
        ebay_price = market_data["ebay_price"]
        ebay_url = market_data["ebay_url"]
        ebay_listing_url = market_data.get("ebay_listing_url")
        ebay_listing_title = market_data.get("ebay_listing_title")

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
            "listing_photos": listing_photos,  # All FB listing photos (JSONB array)
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
            "ebay_listing_url": ebay_listing_url,
            "ebay_listing_title": ebay_listing_title,
            "card_type": ai_card.get("card_type", "single") if ai_card else "single",
        }

    except Exception as e:
        print(f"  ⚠️ Failed to process item: {e}")
        return None


# ── Supabase Upsert ───────────────────────────────────────────

def upsert_listings(listings: list[dict]) -> tuple[int, list[dict]]:
    """Upsert listings to Supabase. Returns (count, upserted_rows_with_ids)."""
    if not listings:
        return 0, []

    url = f"{SUPABASE_URL}/rest/v1/listings"

    total = 0
    all_rows = []
    for i in range(0, len(listings), 50):
        batch = listings[i : i + 50]
        resp = requests.post(
            url,
            json=batch,
            headers={
                **SUPABASE_HEADERS,
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
        )
        if resp.status_code in (200, 201):
            total += len(batch)
            try:
                all_rows.extend(resp.json())
            except Exception:
                pass
        else:
            print(f"  ❌ Supabase upsert failed: {resp.status_code} — {resp.text[:200]}")

    return total, all_rows


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

        upserted, _ = upsert_listings(all_listings)
        print(f"✅ Upserted {upserted} listings to Supabase")
        log_scrape_run(len(all_listings), upserted, "success", f"Backfill from dataset {dataset_id}")
    else:
        print("⚠️ No valid listings found in dataset")


# ── Main Pipeline ──────────────────────────────────────────────

def main():
    # Check for backfill mode
    backfill_ds = os.environ.get("BACKFILL_DATASET", "")
    if backfill_ds:
        backfill_from_dataset(backfill_ds)
        return

    # Whether to enrich listings with detail page data (default: true)
    enrich_detail = os.environ.get("ENRICH_DETAILS", "true").lower() == "true"

    print("🚀 CollectLocal Scraper Starting...")
    print(f"   Location: {SEARCH_LOCATION} (radius: {SEARCH_RADIUS} mi)")
    print(f"   City slug: {CITY_SLUG}")
    print(f"   Actor ID: {APIFY_ACTOR_ID}")
    print(f"   Queries: {len(SEARCH_QUERIES)}")
    print(f"   Max listing age: {MAX_LISTING_AGE_HOURS}h (set MAX_LISTING_AGE_HOURS to change)")
    print(f"   Detail enrichment: {'ON' if enrich_detail else 'OFF'}")
    print(f"   eBay pricing: {'ON' if EBAY_CLIENT_ID else 'OFF (set EBAY_CLIENT_ID to enable)'}")
    print(f"   AI vision: {'ON' if GEMINI_API_KEY else 'OFF (set GEMINI_API_KEY to enable)'}")
    print()

    all_listings = []
    seen_ids = set()

    skipped_old = 0

    for query in SEARCH_QUERIES:
        raw_items = run_apify_scraper(query, max_items=25)

        for item in raw_items:
            # Skip listings older than MAX_LISTING_AGE_HOURS before
            # doing any expensive enrichment (market prices, detail pages)
            if not is_listing_recent(item):
                skipped_old += 1
                continue

            processed = process_apify_result(item)
            if processed and processed["external_id"] not in seen_ids:
                seen_ids.add(processed["external_id"])
                all_listings.append(processed)

        # Small delay between queries to be polite
        time.sleep(2)

    print(f"\n📊 Processed {len(all_listings)} unique listings")
    if skipped_old:
        print(f"   ⏭️  Skipped {skipped_old} listings older than {MAX_LISTING_AGE_HOURS}h")
    if GEMINI_API_KEY:
        print(f"   🤖 AI Vision: {_vision_stats['calls']} calls, {_vision_stats['hits']} cache hits, {_vision_stats['errors']} errors")

    # Enrich with detail page data (descriptions + lat/lng) — free!
    if all_listings and enrich_detail:
        all_listings = enrich_listings_with_details(all_listings)

    if all_listings:
        upserted, upserted_rows = upsert_listings(all_listings)
        print(f"✅ Upserted {upserted} listings to Supabase")

        # ── Lot Analysis Pass ──────────────────────────────────
        # After upsert, analyze any lot listings to identify individual cards
        if GEMINI_API_KEY and upserted_rows:
            from lot_analyzer import is_lot_listing, process_lot_listing
            lot_count = 0
            for row in upserted_rows:
                row_id = row.get("id")
                title = row.get("name", "")
                desc = row.get("description", "")
                img = row.get("image_url", "")
                card_type = row.get("card_type", "")

                if not row_id:
                    continue

                if is_lot_listing(title, desc, card_type):
                    # Use all listing photos for lot analysis (better card identification)
                    image_urls = row.get("listing_photos", [])
                    if not image_urls and img:
                        image_urls = [img]
                    result = process_lot_listing(
                        listing_id=row_id,
                        image_urls=image_urls,
                        title=title,
                        description=desc,
                        lookup_fn=lookup_combined_market_price,
                    )
                    if result:
                        lot_count += 1

            if lot_count:
                print(f"🧩 Analyzed {lot_count} lot listings")

        log_scrape_run(len(all_listings), upserted, "success")
    else:
        print("⚠️ No listings to upsert")
        log_scrape_run(0, 0, "success", "No listings found")

    print("🏁 Done!")


if __name__ == "__main__":
    main()
