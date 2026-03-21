"""
CollectLocal — Pokémon Card Catalog Seeder
=============================================
Imports ALL Pokémon TCG sets and cards into Supabase.

Data source: GitHub repo PokemonTCG/pokemon-tcg-data (static JSON files)
This avoids the pokemontcg.io API which has rate limits and frequent 504 errors.

Usage:
    python seed_pokemon_catalog.py              # Full seed (all sets & cards)
    python seed_pokemon_catalog.py --sets-only  # Only sync sets
    python seed_pokemon_catalog.py --set sv7    # Sync a specific set only
    python seed_pokemon_catalog.py --cards-only # Skip sets, only sync cards

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import time
import json
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/PokemonTCG/pokemon-tcg-data/master"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",   # Upsert behavior
}


# ── Helpers ─────────────────────────────────────────────────────

def github_get_json(path):
    """Fetch a JSON file from the GitHub repo with retries."""
    url = f"{GITHUB_RAW_BASE}/{path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 502, 503, 504):
                wait = min(2 ** attempt, 30)
                print(f"  GitHub error {resp.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            wait = min(2 ** attempt, 30)
            print(f"  Connection error (attempt {attempt+1}/5), retrying in {wait}s...")
            time.sleep(wait)
            continue
    raise Exception(f"Failed after 5 retries: {url}")


def supabase_upsert(table, rows, batch_size=500):
    """Upsert rows into Supabase in batches."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        resp = requests.post(url, headers=SUPABASE_HEADERS, json=batch, timeout=30)
        if resp.status_code not in (200, 201):
            print(f"  ERROR upserting {table}: {resp.status_code} {resp.text[:300]}")
            # Try one-by-one for the failing batch
            for row in batch:
                r = requests.post(url, headers=SUPABASE_HEADERS, json=[row], timeout=30)
                if r.status_code in (200, 201):
                    total += 1
                else:
                    print(f"    Skipped row {row.get('id', '?')}: {r.text[:200]}")
        else:
            total += len(batch)
    return total


def log_sync(sync_id, **kwargs):
    """Update the sync tracking record."""
    url = f"{SUPABASE_URL}/rest/v1/card_catalog_syncs?id=eq.{sync_id}"
    headers = {**SUPABASE_HEADERS, "Prefer": "return=minimal"}
    requests.patch(url, headers=headers, json=kwargs, timeout=10)


# ── Sync Sets ───────────────────────────────────────────────────

def sync_sets():
    """Fetch all Pokémon TCG sets from GitHub and upsert into Supabase."""
    print("Fetching sets from GitHub (pokemon-tcg-data)...")
    api_sets = github_get_json("sets/en.json")
    print(f"  Found {len(api_sets)} sets")

    rows = []
    # Sort by release date descending for sort_order
    api_sets_sorted = sorted(api_sets, key=lambda s: s.get("releaseDate", ""), reverse=True)

    for i, s in enumerate(api_sets_sorted):
        rows.append({
            "id": s["id"],
            "name": s["name"],
            "series": s.get("series", "Unknown"),
            "printed_total": s.get("printedTotal", s.get("total", 0)),
            "total": s.get("total", 0),
            "release_date": s.get("releaseDate"),
            "symbol_url": s.get("images", {}).get("symbol"),
            "logo_url": s.get("images", {}).get("logo"),
            "ptcgo_code": s.get("ptcgoCode"),
            "sort_order": i,
            "tcg": "pokemon",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    count = supabase_upsert("card_sets", rows)
    print(f"Sets sync complete: {count} upserted")
    return rows


# ── Sync Cards ──────────────────────────────────────────────────

def _parse_github_card(c, set_id, set_name, set_printed_total):
    """Convert a GitHub JSON card object into a database row dict."""
    prices = {}
    tcgp = c.get("tcgplayer", {})
    if tcgp:
        for price_type in ["holofoil", "normal", "reverseHolofoil", "1stEditionHolofoil", "1stEditionNormal"]:
            if price_type in tcgp.get("prices", {}):
                prices = tcgp["prices"][price_type]
                break

    cardmarket = c.get("cardmarket", {}).get("prices", {})

    return {
        "id": c["id"],
        "name": c["name"],
        "supertype": c.get("supertype", "Pokémon"),
        "subtypes": c.get("subtypes", []),
        "hp": c.get("hp"),
        "types": c.get("types", []),
        "set_id": set_id,
        "set_name": set_name,
        "number": c.get("number", ""),
        "printed_number": f"{c.get('number', '')}/{set_printed_total}",
        "rarity": c.get("rarity"),
        "artist": c.get("artist"),
        "image_small": c.get("images", {}).get("small"),
        "image_large": c.get("images", {}).get("large"),
        "tcgplayer_url": tcgp.get("url"),
        "tcgplayer_price_low": prices.get("low"),
        "tcgplayer_price_mid": prices.get("mid"),
        "tcgplayer_price_high": prices.get("high"),
        "tcgplayer_price_market": prices.get("market"),
        "cardmarket_price": cardmarket.get("averageSellPrice"),
        "tcg": "pokemon",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def sync_cards(set_filter=None, sets_list=None):
    """
    Fetch cards SET BY SET from GitHub JSON files and upsert into Supabase.
    Each set has its own JSON file at cards/en/{set_id}.json
    """
    total_synced = 0

    if not sets_list:
        # Fetch set list from Supabase
        sb_headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/card_sets?select=id,name,printed_total,total&order=release_date.desc"
        resp = requests.get(url, headers=sb_headers, timeout=30)
        if resp.status_code == 200:
            sets_list = resp.json()
        else:
            print(f"  ⚠️  Failed to fetch sets from Supabase: {resp.status_code} {resp.text[:200]}")
            return 0

    if set_filter:
        sets_list = [s for s in sets_list if s.get("id") == set_filter]
        if not sets_list:
            print(f"  Set '{set_filter}' not found in database. Run without --cards-only first.")
            return 0

    num_sets = len(sets_list)
    print(f"Fetching cards for {num_sets} sets from GitHub...")

    for i, s in enumerate(sets_list):
        sid = s.get("id", "")
        sname = s.get("name", sid)
        sprinted = s.get("printed_total") or s.get("printedTotal") or s.get("total", "?")

        try:
            cards_json = github_get_json(f"cards/en/{sid}.json")
        except Exception as e:
            print(f"  [{i+1}/{num_sets}] {sname}: SKIPPED ({e})")
            continue

        if not cards_json:
            print(f"  [{i+1}/{num_sets}] {sname}: 0 cards (empty file)")
            continue

        rows = [_parse_github_card(c, sid, sname, sprinted) for c in cards_json]
        count = supabase_upsert("card_catalog", rows)
        total_synced += count
        print(f"  [{i+1}/{num_sets}] {sname}: {count} cards  (running total: {total_synced})")

        # Small delay to avoid hammering GitHub/Supabase
        if i % 10 == 9:
            time.sleep(0.5)

    print(f"Cards sync complete: {total_synced} upserted")
    return total_synced


# ── Main ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Seed Pokémon card catalog from GitHub data")
    parser.add_argument("--sets-only", action="store_true", help="Only sync sets, not cards")
    parser.add_argument("--cards-only", action="store_true", help="Skip sets (use sets already in Supabase), only sync cards")
    parser.add_argument("--set", type=str, help="Sync a specific set ID only (e.g., 'sv7', 'swsh7')")
    args = parser.parse_args()

    # Create sync tracking record
    resp = requests.post(
        f"{SUPABASE_URL}/rest/v1/card_catalog_syncs",
        headers={**SUPABASE_HEADERS, "Prefer": "return=representation"},
        json={"status": "running"},
        timeout=10,
    )
    sync_id = resp.json()[0]["id"] if resp.status_code in (200, 201) else None

    try:
        # Step 1: Sync sets (skip if --cards-only)
        sets = []
        if not args.cards_only:
            sets = sync_sets()
            if sync_id:
                log_sync(sync_id, sets_synced=len(sets))
        else:
            print("Skipping sets (--cards-only), reading set list from Supabase...")

        # Step 2: Sync cards (unless --sets-only)
        cards_count = 0
        if not args.sets_only:
            cards_count = sync_cards(set_filter=args.set, sets_list=sets if sets else None)

        # Mark success
        if sync_id:
            log_sync(sync_id,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      cards_synced=cards_count,
                      status="success")

        print(f"\n✅ Catalog seed complete: {len(sets)} sets, {cards_count} cards")

    except Exception as e:
        print(f"\n❌ Seed failed: {e}")
        if sync_id:
            log_sync(sync_id,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      status="failed",
                      error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    main()
