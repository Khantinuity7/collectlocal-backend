"""
CollectLocal — Catalog Price Updater
======================================
Updates tcgplayer_price_market for ALL cards in the card_catalog table
using the TCGTracking Open API (free, no auth, no rate limits).

Covers both Pokémon (category 3) and One Piece (category 73).

Usage:
    python update_catalog_prices.py                # Update all Pokemon + One Piece
    python update_catalog_prices.py --tcg pokemon  # Pokemon only
    python update_catalog_prices.py --tcg onepiece # One Piece only
    python update_catalog_prices.py --dry-run      # Preview without writing to DB

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import time
import json
import re
import argparse
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed

load_dotenv()

# ── Config ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
TCGTRACK_BASE = "https://tcgtracking.com/tcgapi/v1"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

SUPABASE_READ_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

CATEGORIES = {
    "pokemon": 3,
    "onepiece": 68,
}


# ── Helpers ─────────────────────────────────────────────────────

def api_get(url, retries=3):
    """GET with retries."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 502, 503, 504):
                wait = min(2 ** attempt, 10)
                print(f"    Retry {attempt+1}/{retries} ({resp.status_code}), waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"    API error: {resp.status_code} for {url}")
            return None
        except Exception as e:
            wait = min(2 ** attempt, 10)
            print(f"    Request error: {e}, retry in {wait}s...")
            time.sleep(wait)
    return None


def supabase_batch_update(rows, dry_run=False):
    """Update tcgplayer_price_market for a batch of card_catalog rows.

    Uses PATCH (not POST/upsert) because card_catalog has NOT NULL constraints
    on columns like 'name', and POST upsert tries to INSERT first which fails
    when only sending {id, tcgplayer_price_market, updated_at}.
    """
    if not rows or dry_run:
        return len(rows)

    patch_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=minimal",
    }

    def patch_one(row):
        card_id = row["id"]
        url = f"{SUPABASE_URL}/rest/v1/card_catalog?id=eq.{requests.utils.quote(str(card_id))}"
        payload = {
            "tcgplayer_price_market": row["tcgplayer_price_market"],
            "updated_at": row["updated_at"],
        }
        try:
            resp = requests.patch(url, headers=patch_headers, json=payload, timeout=15)
            return 1 if resp.status_code in (200, 204) else 0
        except Exception:
            return 0

    total = 0
    # Process 20 concurrent PATCH requests at a time
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(patch_one, row) for row in rows]
        for f in as_completed(futures):
            total += f.result()

    return total


def normalize_name(name):
    """Normalize a card name for matching."""
    if not name:
        return ""
    s = name.lower().strip()
    # Remove card number suffixes like " - 125/197"
    s = re.sub(r'\s*-\s*\d+/\d+$', '', s)
    # Remove special characters but keep spaces
    s = re.sub(r'[^\w\s]', '', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_number(product):
    """Extract card number from a TCGTracking product."""
    num = product.get("number")
    if num:
        # Could be "125/197" → extract "125"
        if "/" in str(num):
            return str(num).split("/")[0].strip()
        return str(num).strip()
    # Try extracting from name like "Charizard ex - 125/197"
    name = product.get("name", "")
    match = re.search(r'-\s*(\d+)/\d+$', name)
    if match:
        return match.group(1)
    return None


def get_best_market_price(prices_for_product):
    """Extract the best market price from TCGTracking pricing data.

    pricing structure: { "tcg": { "Normal": { "low": X, "market": Y }, "Holofoil": { ... } } }
    Returns the highest market price across subtypes (Normal, Holofoil, etc.)
    """
    if not prices_for_product:
        return None

    tcg_prices = prices_for_product.get("tcg", {})
    if not tcg_prices:
        return None

    best_price = None
    # Prefer Normal > Holofoil > Reverse Holofoil > 1st Edition
    for subtype in ["Normal", "Holofoil", "Reverse Holofoil", "1st Edition Normal", "1st Edition Holofoil"]:
        sub = tcg_prices.get(subtype, {})
        market = sub.get("market")
        if market and market > 0:
            if best_price is None or subtype == "Normal":
                best_price = market
            break  # Take the first available

    # If none found, take any available
    if best_price is None:
        for subtype, sub in tcg_prices.items():
            market = sub.get("market")
            if market and market > 0:
                best_price = market
                break

    return best_price


# ── Catalog card loading from Supabase ──────────────────────────

def load_catalog_sets(tcg_filter):
    """Load all sets for a TCG from Supabase card_sets table."""
    url = f"{SUPABASE_URL}/rest/v1/card_sets?select=id,name,ptcgo_code,total&tcg=eq.{tcg_filter}&order=sort_order&limit=1000"
    resp = requests.get(url, headers=SUPABASE_READ_HEADERS, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    print(f"  ⚠️  Failed to load sets from Supabase: {resp.status_code}")
    return []


def load_catalog_cards(set_id):
    """Load all cards for a set from Supabase card_catalog table."""
    url = f"{SUPABASE_URL}/rest/v1/card_catalog?select=id,name,number,printed_number,tcgplayer_price_market&set_id=eq.{set_id}&limit=500"
    resp = requests.get(url, headers=SUPABASE_READ_HEADERS, timeout=30)
    if resp.status_code == 200:
        return resp.json()
    return []


# ── Set matching (catalog set → TCGTracking set) ────────────────

def normalize_code(code):
    """Normalize a set code by removing dashes and lowering. 'OP-01' → 'op01'."""
    if not code:
        return ""
    return re.sub(r'[-\s]', '', code.lower().strip())


def strip_set_prefix(name):
    """Remove set code prefix from TCGTracking names like 'OP01: Romance Dawn' → 'romance dawn'."""
    s = name.lower().strip()
    # Remove patterns like "OP01: ", "ST-29: ", "EB-03: ", "Starter Deck 22: "
    s = re.sub(r'^[A-Za-z]+-?\d+\s*:\s*', '', s)
    s = re.sub(r'^starter deck \d+:\s*', '', s)
    return s.strip()


def match_sets(catalog_sets, tcgtrack_sets):
    """Match catalog sets to TCGTracking sets by code and name.

    Returns list of (catalog_set, tcgtrack_set) pairs.
    """
    matches = []
    used_tcg_ids = set()

    # Build indexes for TCGTracking sets
    tcg_by_code = {}   # normalized abbreviation → set
    tcg_by_name = {}   # lowered name → set
    tcg_by_stripped = {}  # name with prefix stripped → set

    for ts in tcgtrack_sets:
        abbr = normalize_code(ts.get("abbreviation", ""))
        if abbr:
            tcg_by_code[abbr] = ts
        ts_name = ts["name"].lower().strip()
        tcg_by_name[ts_name] = ts
        stripped = strip_set_prefix(ts["name"])
        if stripped:
            tcg_by_stripped[stripped] = ts

    for cs in catalog_sets:
        cs_name = cs["name"].lower().strip()
        cs_code = normalize_code(cs.get("ptcgo_code", ""))

        matched = None

        # 1. Match by set code/abbreviation (most reliable)
        if cs_code and cs_code in tcg_by_code:
            matched = tcg_by_code[cs_code]

        # 2. Exact name match
        if not matched and cs_name in tcg_by_name:
            matched = tcg_by_name[cs_name]

        # 3. Match catalog name to TCGTracking stripped name (e.g., "romance dawn" matches "OP01: Romance Dawn")
        if not matched and cs_name in tcg_by_stripped:
            matched = tcg_by_stripped[cs_name]

        # 4. Match stripped catalog name in TCGTracking stripped names
        if not matched:
            cs_stripped = strip_set_prefix(cs["name"])
            if cs_stripped in tcg_by_stripped:
                matched = tcg_by_stripped[cs_stripped]

        # 5. Fuzzy: check containment
        if not matched:
            for ts in tcgtrack_sets:
                if ts["id"] in used_tcg_ids:
                    continue
                ts_stripped = strip_set_prefix(ts["name"])
                if cs_name and ts_stripped and (cs_name in ts_stripped or ts_stripped in cs_name):
                    matched = ts
                    break

        # 6. Word overlap (at least 2 meaningful words)
        if not matched:
            stop_words = {"the", "of", "and", "a", "an", "-", "&", "deck", "starter", "booster", "pack"}
            cs_words = set(cs_name.split()) - stop_words
            best_overlap = 0
            best_ts = None
            for ts in tcgtrack_sets:
                if ts["id"] in used_tcg_ids:
                    continue
                ts_words = set(ts["name"].lower().split()) - stop_words
                overlap = len(cs_words & ts_words)
                if overlap > best_overlap and overlap >= 2:
                    best_overlap = overlap
                    best_ts = ts
            if best_ts:
                matched = best_ts

        if matched:
            matches.append((cs, matched))
            used_tcg_ids.add(matched["id"])

    return matches


# ── Card matching (catalog card → TCGTracking product) ──────────

def match_and_price_cards(catalog_cards, tcg_products, tcg_prices):
    """Match catalog cards to TCGTracking products and extract prices.

    Returns list of { "id": catalog_card_id, "tcgplayer_price_market": price, "updated_at": ... }
    """
    if not catalog_cards or not tcg_products:
        return []

    # Build product indexes
    prod_by_number = {}  # number → product
    prod_by_name = {}    # normalized_name → product

    for p in tcg_products:
        num = extract_number(p)
        if num:
            prod_by_number[num] = p
        nname = normalize_name(p.get("name", ""))
        if nname:
            prod_by_name[nname] = p

    updates = []
    now = datetime.now(timezone.utc).isoformat()

    for card in catalog_cards:
        card_id = card["id"]
        card_name = card.get("name", "")
        card_number = str(card.get("number", "")).strip()

        # Try to find matching product
        matched_product = None

        # 1. Match by card number (most reliable)
        if card_number and card_number in prod_by_number:
            matched_product = prod_by_number[card_number]

        # 2. Match by normalized name
        if not matched_product:
            norm = normalize_name(card_name)
            if norm in prod_by_name:
                matched_product = prod_by_name[norm]

        # 3. Fuzzy name matching within products
        if not matched_product:
            norm = normalize_name(card_name)
            for pname, p in prod_by_name.items():
                if norm and pname and (norm in pname or pname in norm):
                    # Verify number matches if both have one
                    pnum = extract_number(p)
                    if card_number and pnum and card_number != pnum:
                        continue  # Number mismatch, skip
                    matched_product = p
                    break

        if not matched_product:
            continue

        # Get price
        product_id = str(matched_product.get("id", ""))
        price_data = tcg_prices.get(product_id)
        market_price = get_best_market_price(price_data)

        if market_price and market_price > 0:
            updates.append({
                "id": card_id,
                "tcgplayer_price_market": round(market_price, 2),
                "updated_at": now,
            })

    return updates


# ── Main processing ─────────────────────────────────────────────

def process_tcg(tcg_name, dry_run=False):
    """Process all sets for a given TCG."""
    category_id = CATEGORIES[tcg_name]
    print(f"\n{'='*60}")
    print(f"  Processing {tcg_name.upper()} (category {category_id})")
    print(f"{'='*60}")

    # 1. Load TCGTracking sets
    print(f"\n  Fetching TCGTracking sets...")
    data = api_get(f"{TCGTRACK_BASE}/{category_id}/sets")
    if not data:
        print(f"  ❌ Failed to fetch TCGTracking sets")
        return 0
    tcgtrack_sets = data.get("sets", [])
    print(f"  Found {len(tcgtrack_sets)} TCGTracking sets")

    # 2. Load catalog sets from Supabase
    print(f"  Loading catalog sets from Supabase...")
    catalog_sets = load_catalog_sets(tcg_name)
    print(f"  Found {len(catalog_sets)} catalog sets")

    # 3. Match sets
    print(f"  Matching sets...")
    set_pairs = match_sets(catalog_sets, tcgtrack_sets)
    print(f"  Matched {len(set_pairs)} / {len(catalog_sets)} catalog sets to TCGTracking")

    # 4. Process each matched set
    total_updated = 0
    total_cards = 0

    for i, (cs, ts) in enumerate(set_pairs):
        cs_name = cs["name"]
        ts_id = ts["id"]
        ts_name = ts["name"]

        # Load catalog cards for this set
        catalog_cards = load_catalog_cards(cs["id"])
        if not catalog_cards:
            continue

        total_cards += len(catalog_cards)

        # Load TCGTracking products + pricing
        prod_data = api_get(f"{TCGTRACK_BASE}/{category_id}/sets/{ts_id}")
        price_data = api_get(f"{TCGTRACK_BASE}/{category_id}/sets/{ts_id}/pricing")

        if not prod_data or not price_data:
            print(f"  [{i+1}/{len(set_pairs)}] {cs_name}: SKIPPED (API error)")
            continue

        products = prod_data.get("products", [])
        prices = price_data.get("prices", {})

        # Match cards and get prices
        updates = match_and_price_cards(catalog_cards, products, prices)

        if updates:
            count = supabase_batch_update(updates, dry_run=dry_run)
            total_updated += count
            prefix = "[DRY RUN] " if dry_run else ""
            print(f"  [{i+1}/{len(set_pairs)}] {cs_name} → {ts_name}: {prefix}{count}/{len(catalog_cards)} cards priced")
        else:
            print(f"  [{i+1}/{len(set_pairs)}] {cs_name} → {ts_name}: 0/{len(catalog_cards)} cards matched")

        # Small delay every 10 sets to be gentle
        if i % 10 == 9:
            time.sleep(0.3)

    print(f"\n  ✅ {tcg_name.upper()} complete: {total_updated}/{total_cards} cards updated with prices")
    return total_updated


def main():
    parser = argparse.ArgumentParser(description="Update catalog prices from TCGTracking API")
    parser.add_argument("--tcg", choices=["pokemon", "onepiece"], help="Only update one TCG (default: both)")
    parser.add_argument("--dry-run", action="store_true", help="Preview matches without writing to DB")
    args = parser.parse_args()

    start = time.time()
    total = 0

    tcgs = [args.tcg] if args.tcg else ["pokemon", "onepiece"]

    for tcg in tcgs:
        total += process_tcg(tcg, dry_run=args.dry_run)

    elapsed = time.time() - start
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"\n{'='*60}")
    print(f"  {prefix}TOTAL: {total} cards updated with prices in {elapsed:.1f}s")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
