"""
CollectLocal — FB Marketplace Scraper Pipeline
================================================
Runs via GitHub Actions (free) on a cron schedule.
1. Calls Apify to scrape FB Marketplace for Pokémon card listings
2. Enriches with market prices from the Pokémon TCG API (free)
3. Pushes to Supabase (free tier)

Cost: $0/month on Apify free tier ($5 credits = ~1,000 listings/month)
"""

import os
import re
import math
import json
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# Config
APIFY_TOKEN = os.environ["APIFY_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HOME_LAT = float(os.environ.get("HOME_LAT", "32.9700"))
HOME_LNG = float(os.environ.get("HOME_LNG", "-96.7500"))
SEARCH_LOCATION = os.environ.get("SEARCH_LOCATION", "Dallas, TX")
SEARCH_RADIUS = int(os.environ.get("SEARCH_RADIUS_MILES", "40"))

APIFY_ACTOR_ID = "apify/facebook-marketplace-scraper"

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

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}


def haversine_miles(lat1, lng1, lat2, lng2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def parse_grade(title):
    title_upper = title.upper()
    sealed_keywords = ["BOOSTER BOX", "ETB", "ELITE TRAINER", "SEALED", "BOOSTER BUNDLE", "COLLECTION BOX"]
    if any(kw in title_upper for kw in sealed_keywords):
        return "Sealed"
    grade_patterns = [r"PSA\s*(\d+(?:\.\d+)?)", r"BGS\s*(\d+(?:\.\d+)?)", r"CGC\s*(\d+(?:\.\d+)?)"]
    for pattern in grade_patterns:
        match = re.search(pattern, title_upper)
        if match:
            company = pattern[:3].upper()
            return f"{company} {match.group(1)}"
    return "Raw"


def parse_card_name(title):
    clean = re.sub(r"\b(PSA|BGS|CGC)\s*\d+(\.\d+)?\b", "", title, flags=re.IGNORECASE)
    clean = re.sub(r"\b(GEM MINT|MINT|NM|LP|MP|HP)\b", "", clean, flags=re.IGNORECASE)
    clean = re.sub(r"\b(POKEMON|CARD|TCG|SLAB)\b", "", clean, flags=re.IGNORECASE)
    clean = clean.strip(" -|/,")
    number_match = re.search(r"(\d{1,3}/\d{1,3})", clean)
    number = number_match.group(1) if number_match else ""
    if number:
        clean = clean.replace(number, "").strip()
    return {"name": clean.strip() or title[:50], "number": number}


def time_ago(timestamp_str):
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


TCG_API_BASE = "https://api.pokemontcg.io/v2"
_tcg_cache = {}


def lookup_market_price(card_name, set_name=""):
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
        print(f"  TCG API lookup failed for '{card_name}': {e}")
    _tcg_cache[cache_key] = None
    return None


def run_apify_scraper(search_query, max_items=50):
    print(f"  Scraping FB Marketplace for: '{search_query}'")
    url = f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    params = {"token": APIFY_TOKEN}
    payload = {
        "searchQuery": search_query,
        "location": SEARCH_LOCATION,
        "maxItems": max_items,
        "sortBy": "creation_time_descend",
    }
    try:
        resp = requests.post(url, json=payload, params=params, timeout=180)
        if resp.status_code == 201:
            items = resp.json()
            print(f"  Got {len(items)} results")
            return items
        else:
            print(f"  Apify returned {resp.status_code}: {resp.text[:200]}")
            return []
    except Exception as e:
        print(f"  Apify error: {e}")
        return []


def process_apify_result(item):
    try:
        title = item.get("title", "").strip()
        price_str = item.get("price", "0")
        price_clean = re.sub(r"[^\d.]", "", str(price_str))
        price = float(price_clean) if price_clean else 0
        if price < 5 or price > 50000:
            return None
        if not title:
            return None
        card_info = parse_card_name(title)
        grade = parse_grade(title)
        location_text = item.get("location", "")
        item_lat = item.get("latitude", 0) or 0
        item_lng = item.get("longitude", 0) or 0
        distance = 0
        if item_lat and item_lng:
            distance = int(haversine_miles(HOME_LAT, HOME_LNG, item_lat, item_lng))
        if distance > SEARCH_RADIUS:
            return None
        market_data = lookup_market_price(card_info["name"])
        market_price = market_data["market_price"] if market_data else price
        market_source = market_data["market_source"] if market_data else "tcgplayer"
        image_url = market_data["image_url"] if market_data else (item.get("image", "") or "")
        set_name = market_data["set"] if market_data else ""
        card_number = market_data["number"] if market_data else card_info["number"]
        seller_name = item.get("sellerName", item.get("seller", {}).get("name", "Unknown"))
        seller_rating = 0
        try:
            seller_rating = float(item.get("sellerRating", 0) or 0)
        except (ValueError, TypeError):
            pass
        external_id = item.get("id") or item.get("url", f"{title}-{price}")
        return {
            "external_id": str(external_id),
            "name": card_info["name"][:100],
            "card_set": set_name[:100],
            "card_number": card_number[:20],
            "grade": grade,
            "price": price,
            "market_price": market_price,
            "market_source": market_source,
            "image_url": image_url,
            "marketplace": "facebook",
            "location": location_text[:100] or SEARCH_LOCATION,
            "distance": distance,
            "posted": time_ago(item.get("timestamp", "")),
            "seller": seller_name[:50],
            "seller_rating": seller_rating,
            "lat": item_lat,
            "lng": item_lng,
            "description": (item.get("description", "") or "")[:500],
            "listing_url": item.get("url", ""),
            "is_active": True,
        }
    except Exception as e:
        print(f"  Failed to process item: {e}")
        return None


def upsert_listings(listings):
    if not listings:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/listings"
    total = 0
    for i in range(0, len(listings), 50):
        batch = listings[i : i + 50]
        resp = requests.post(
            url, json=batch,
            headers={**SUPABASE_HEADERS, "Prefer": "resolution=merge-duplicates"},
        )
        if resp.status_code in (200, 201):
            total += len(batch)
        else:
            print(f"  Supabase upsert failed: {resp.status_code}")
    return total


def log_scrape_run(listings_found, listings_new, status, error=""):
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


def main():
    print("CollectLocal Scraper Starting...")
    print(f"   Location: {SEARCH_LOCATION} (radius: {SEARCH_RADIUS} mi)")
    print(f"   Queries: {len(SEARCH_QUERIES)}")
    print()
    all_listings = []
    seen_ids = set()
    for query in SEARCH_QUERIES:
        raw_items = run_apify_scraper(query, max_items=25)
        for item in raw_items:
            processed = process_apify_result(item)
            if processed and processed["external_id"] not in seen_ids:
                seen_ids.add(processed["external_id"])
                all_listings.append(processed)
        time.sleep(2)
    print(f"Processed {len(all_listings)} unique listings")
    if all_listings:
        upserted = upsert_listings(all_listings)
        print(f"Upserted {upserted} listings to Supabase")
        log_scrape_run(len(all_listings), upserted, "success")
    else:
        print("No listings to upsert")
        log_scrape_run(0, 0, "success", "No listings found")
    print("Done!")


if __name__ == "__main__":
    main()
