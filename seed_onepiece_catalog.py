"""
CollectLocal — One Piece Card Catalog Seeder
==============================================
Imports ALL One Piece TCG sets and cards into Supabase.

Data source: GitHub repo buhbbl/punk-records (static JSON files)
Structure:
  - english/packs.json  → dict of {pack_id: {id, raw_title, title_parts: {prefix, title, label}}}
  - english/cards/{pack_id}/{card_id}.json → individual card files

Usage:
    python seed_onepiece_catalog.py              # Full seed (all sets & cards)
    python seed_onepiece_catalog.py --sets-only  # Only sync sets
    python seed_onepiece_catalog.py --set ST-01  # Sync a specific set only

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

GITHUB_RAW_BASE = "https://raw.githubusercontent.com/buhbbl/punk-records/main/english"
GITHUB_API_BASE = "https://api.github.com/repos/buhbbl/punk-records/contents/english"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

TCG_NAME = "onepiece"


# ── Helpers ─────────────────────────────────────────────────────

def github_get_json(path):
    """Fetch a JSON file from the GitHub repo raw URL with retries."""
    url = f"{GITHUB_RAW_BASE}/{path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=60)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
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


def github_list_files(folder_path):
    """List files in a GitHub repo folder using the GitHub API."""
    url = f"{GITHUB_API_BASE}/{folder_path}"
    for attempt in range(5):
        try:
            resp = requests.get(url, timeout=60, headers={"Accept": "application/vnd.github.v3+json"})
            if resp.status_code == 200:
                items = resp.json()
                # Return only files (not directories), just the names
                return [item["name"] for item in items if item["type"] == "file"]
            if resp.status_code == 404:
                return []
            if resp.status_code in (403, 429):
                # Rate limited
                wait = min(2 ** attempt * 5, 60)
                print(f"  GitHub API rate limited ({resp.status_code}), waiting {wait}s...")
                time.sleep(wait)
                continue
            if resp.status_code in (502, 503, 504):
                wait = min(2 ** attempt, 30)
                print(f"  GitHub API error {resp.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except (requests.exceptions.ReadTimeout, requests.exceptions.ConnectionError) as e:
            wait = min(2 ** attempt, 30)
            print(f"  Connection error (attempt {attempt+1}/5), retrying in {wait}s...")
            time.sleep(wait)
            continue
    print(f"  ⚠️  Failed to list files in {folder_path} after 5 retries")
    return []


def supabase_upsert(table, rows, batch_size=500):
    """Upsert rows into Supabase in batches."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        resp = requests.post(url, headers=SUPABASE_HEADERS, json=batch, timeout=30)
        if resp.status_code not in (200, 201):
            print(f"  ERROR upserting {table}: {resp.status_code} {resp.text[:300]}")
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
    """Fetch all One Piece TCG sets from GitHub and upsert into Supabase."""
    print("Fetching One Piece sets from GitHub (punk-records)...")
    packs_dict = github_get_json("packs.json")

    if not packs_dict:
        print("  ⚠️  Could not fetch packs.json")
        return []

    # packs.json is a DICT keyed by pack_id, not an array
    print(f"  Found {len(packs_dict)} sets")

    rows = []
    for i, (pack_id, p) in enumerate(packs_dict.items()):
        # Extract name from title_parts
        title_parts = p.get("title_parts", {})
        pack_label = title_parts.get("label", pack_id)  # e.g., "ST-06", "OP-01"
        pack_title = title_parts.get("title", "")         # e.g., "Absolute Justice"
        pack_prefix = title_parts.get("prefix", "")       # e.g., "STARTER DECK", "BOOSTER PACK"

        # Build a nice display name
        display_name = pack_title if pack_title else p.get("raw_title", pack_label)

        rows.append({
            "id": f"op-{pack_id}",  # Prefix with op- to avoid ID collisions with pokemon
            "name": display_name,
            "series": pack_prefix or "One Piece",
            "printed_total": 0,  # We'll count cards later
            "total": 0,
            "release_date": None,
            "symbol_url": None,
            "logo_url": None,
            "ptcgo_code": pack_id,  # Store the original numeric pack ID for card fetching
            "sort_order": i,
            "tcg": TCG_NAME,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

    count = supabase_upsert("card_sets", rows)
    print(f"Sets sync complete: {count} upserted")
    return rows


# ── Sync Cards ──────────────────────────────────────────────────

def _parse_onepiece_card(c, set_id, set_name):
    """Convert a punk-records card JSON object into a database row dict."""
    card_id_raw = c.get("id", "")

    # Image URLs
    image_small = c.get("img_full_url") or c.get("img_url")
    image_large = c.get("img_full_url") or image_small

    # Fix relative image URLs
    if image_small and image_small.startswith("../"):
        image_small = f"https://en.onepiece-cardgame.com/{image_small.lstrip('../')}"
    if image_large and image_large.startswith("../"):
        image_large = f"https://en.onepiece-cardgame.com/{image_large.lstrip('../')}"

    rarity = c.get("rarity", "")
    # Extract the card number from the ID (e.g., "ST01-001" → "001")
    number = card_id_raw.split("-")[-1] if "-" in str(card_id_raw) else card_id_raw

    return {
        "id": f"op-{card_id_raw}" if not str(card_id_raw).startswith("op-") else card_id_raw,
        "name": c.get("name", "Unknown"),
        "supertype": c.get("category", "Character"),  # Leader/Character/Event/Stage/Don
        "subtypes": c.get("types", []) if isinstance(c.get("types"), list) else [c.get("types", "")] if c.get("types") else [],
        "hp": str(c.get("power", "")) if c.get("power") else None,
        "types": c.get("colors", []) if isinstance(c.get("colors"), list) else [c.get("colors", "")] if c.get("colors") else [],
        "set_id": set_id,
        "set_name": set_name,
        "number": number,
        "printed_number": card_id_raw,  # Full card ID like "ST01-001"
        "rarity": rarity,
        "artist": None,
        "image_small": image_small,
        "image_large": image_large,
        "tcgplayer_url": None,
        "tcgplayer_price_low": None,
        "tcgplayer_price_mid": None,
        "tcgplayer_price_high": None,
        "tcgplayer_price_market": None,
        "cardmarket_price": None,
        "tcg": TCG_NAME,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def sync_cards(set_filter=None, sets_list=None):
    """Fetch cards set by set from GitHub JSON files.

    Each set's cards are individual .json files inside english/cards/{pack_id}/
    """
    total_synced = 0

    if not sets_list:
        # Fetch from Supabase
        sb_headers = {
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        }
        url = f"{SUPABASE_URL}/rest/v1/card_sets?select=id,name,ptcgo_code,total&tcg=eq.{TCG_NAME}&order=sort_order"
        resp = requests.get(url, headers=sb_headers, timeout=30)
        if resp.status_code == 200:
            sets_list = resp.json()
        else:
            print(f"  ⚠️  Failed to fetch sets from Supabase: {resp.status_code} {resp.text[:200]}")
            return 0

    if set_filter:
        sets_list = [s for s in sets_list if set_filter in (s.get("ptcgo_code", ""), s.get("id", ""))]

    num_sets = len(sets_list)
    print(f"Fetching cards for {num_sets} One Piece sets from GitHub...")

    for i, s in enumerate(sets_list):
        sid = s.get("id", "")
        sname = s.get("name", sid)
        # The original numeric pack ID used in the GitHub repo
        pack_code = s.get("ptcgo_code", sid.replace("op-", ""))

        # List all card JSON files in this pack's folder
        card_files = github_list_files(f"cards/{pack_code}")

        if not card_files:
            print(f"  [{i+1}/{num_sets}] {sname} ({pack_code}): SKIPPED (no card files found)")
            continue

        # Filter to only .json files
        card_files = [f for f in card_files if f.endswith(".json")]

        # Fetch each card JSON
        rows = []
        for cf in card_files:
            card_data = github_get_json(f"cards/{pack_code}/{cf}")
            if card_data:
                rows.append(_parse_onepiece_card(card_data, sid, sname))

        if rows:
            count = supabase_upsert("card_catalog", rows)
            total_synced += count
            print(f"  [{i+1}/{num_sets}] {sname}: {count} cards  (running total: {total_synced})")
        else:
            print(f"  [{i+1}/{num_sets}] {sname}: 0 cards (all files empty)")

        # Brief pause every 5 sets to be kind to GitHub API rate limits
        if i % 5 == 4:
            time.sleep(1)

    print(f"Cards sync complete: {total_synced} upserted")
    return total_synced


# ── Main ────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Seed One Piece card catalog from GitHub data")
    parser.add_argument("--sets-only", action="store_true", help="Only sync sets, not cards")
    parser.add_argument("--cards-only", action="store_true", help="Skip sets, only sync cards")
    parser.add_argument("--set", type=str, help="Sync a specific set label (e.g., 'ST-01')")
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
        # Step 1: Sync sets
        sets = []
        if not args.cards_only:
            sets = sync_sets()
            if sync_id:
                log_sync(sync_id, sets_synced=len(sets))
        else:
            print("Skipping sets (--cards-only), reading set list from Supabase...")

        # Step 2: Sync cards
        cards_count = 0
        if not args.sets_only:
            cards_count = sync_cards(set_filter=args.set, sets_list=sets if sets else None)

        if sync_id:
            log_sync(sync_id,
                      finished_at=datetime.now(timezone.utc).isoformat(),
                      cards_synced=cards_count,
                      status="success")

        print(f"\n✅ One Piece catalog seed complete: {len(sets)} sets, {cards_count} cards")

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
