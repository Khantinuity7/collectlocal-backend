"""
CollectLocal — Seed Restock Products & Target Stores
=====================================================
Populates the restock_products table with popular Pokemon and One Piece TCG
products, plus seeds nearby Target stores using Target's store locator API.

Run once to set up, then periodically to add new products as sets release.

Usage: python seed_restock_products.py
"""

import os
import json
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=representation"
}

# ── TCG Products to Track ───────────────────────────────────────
# target_tcin: the number after "A-" in any Target.com product URL
# target_dpci: Target's internal Department-Class-Item number
# walmart_sku: Walmart's product ID
# walmart_url: Direct product page URL for scraping

PRODUCTS = [
    # ==================== POKEMON TCG ====================

    # --- Scarlet & Violet: Prismatic Evolutions ---
    {
        "name": "Prismatic Evolutions Elite Trainer Box",
        "tcg": "pokemon",
        "product_type": "etb",
        "msrp": 49.99,
        "upc": "820650854750",
        "target_dpci": "087-16-2847",
        "target_tcin": "91508218",
        "walmart_sku": "631747281",
        "walmart_url": "https://www.walmart.com/ip/631747281",
        "packaging_keywords": ["prismatic", "evolutions", "elite", "trainer", "box"],
    },
    {
        "name": "Prismatic Evolutions Booster Bundle",
        "tcg": "pokemon",
        "product_type": "booster_bundle",
        "msrp": 24.99,
        "upc": "820650854767",
        "target_dpci": "087-16-2848",
        "target_tcin": "91508219",
        "walmart_sku": "631747282",
        "walmart_url": "https://www.walmart.com/ip/631747282",
        "packaging_keywords": ["prismatic", "evolutions", "booster", "bundle"],
    },
    {
        "name": "Prismatic Evolutions Binder Collection",
        "tcg": "pokemon",
        "product_type": "collection_box",
        "msrp": 39.99,
        "target_tcin": "91508220",
        "packaging_keywords": ["prismatic", "evolutions", "binder", "collection"],
    },

    # --- Scarlet & Violet: Surging Sparks ---
    {
        "name": "Surging Sparks Booster Box (36 Packs)",
        "tcg": "pokemon",
        "product_type": "booster_box",
        "msrp": 143.64,
        "upc": "820650853920",
        "target_dpci": "087-16-2901",
        "target_tcin": "91234567",
        "walmart_sku": "631747350",
        "walmart_url": "https://www.walmart.com/ip/631747350",
        "packaging_keywords": ["surging", "sparks", "booster", "box"],
    },
    {
        "name": "Surging Sparks Elite Trainer Box",
        "tcg": "pokemon",
        "product_type": "etb",
        "msrp": 49.99,
        "target_tcin": "91234568",
        "packaging_keywords": ["surging", "sparks", "elite", "trainer"],
    },

    # --- Scarlet & Violet: Shrouded Fable ---
    {
        "name": "Shrouded Fable Elite Trainer Box",
        "tcg": "pokemon",
        "product_type": "etb",
        "msrp": 49.99,
        "target_tcin": "91345678",
        "packaging_keywords": ["shrouded", "fable", "elite", "trainer"],
    },

    # --- Scarlet & Violet: Twilight Masquerade ---
    {
        "name": "Twilight Masquerade Booster Box",
        "tcg": "pokemon",
        "product_type": "booster_box",
        "msrp": 143.64,
        "target_tcin": "91456789",
        "packaging_keywords": ["twilight", "masquerade", "booster", "box"],
    },
    {
        "name": "Twilight Masquerade Elite Trainer Box",
        "tcg": "pokemon",
        "product_type": "etb",
        "msrp": 49.99,
        "target_tcin": "91456790",
        "packaging_keywords": ["twilight", "masquerade", "elite", "trainer"],
    },

    # --- Pokemon: Misc High-Demand ---
    {
        "name": "Pokemon TCG 3-Pack Blister (Latest Set)",
        "tcg": "pokemon",
        "product_type": "blister",
        "msrp": 14.99,
        "target_tcin": "91567890",
        "packaging_keywords": ["pokemon", "blister", "3-pack"],
    },

    # ==================== ONE PIECE TCG ====================

    # --- OP-09: Four Emperors ---
    {
        "name": "One Piece TCG: Four Emperors Booster Box (OP-09)",
        "tcg": "one_piece",
        "product_type": "booster_box",
        "msrp": 143.64,
        "target_tcin": "92100001",
        "walmart_url": "https://www.walmart.com/search?q=one+piece+tcg+op-09+booster+box",
        "packaging_keywords": ["one piece", "four emperors", "op-09", "booster", "box"],
    },

    # --- OP-08: Two Legends ---
    {
        "name": "One Piece TCG: Two Legends Booster Box (OP-08)",
        "tcg": "one_piece",
        "product_type": "booster_box",
        "msrp": 143.64,
        "target_tcin": "92100002",
        "packaging_keywords": ["one piece", "two legends", "op-08", "booster", "box"],
    },

    # --- OP-07: 500 Years in the Future ---
    {
        "name": "One Piece TCG: 500 Years in the Future Booster Box (OP-07)",
        "tcg": "one_piece",
        "product_type": "booster_box",
        "msrp": 143.64,
        "target_tcin": "92100003",
        "packaging_keywords": ["one piece", "500 years", "op-07", "booster", "box"],
    },

    # --- One Piece Starter Decks ---
    {
        "name": "One Piece TCG: Starter Deck — Monkey D. Luffy (ST-08)",
        "tcg": "one_piece",
        "product_type": "starter_deck",
        "msrp": 17.99,
        "target_tcin": "92100010",
        "packaging_keywords": ["one piece", "starter", "luffy", "st-08"],
    },
    {
        "name": "One Piece TCG: Starter Deck — Yamato (ST-09)",
        "tcg": "one_piece",
        "product_type": "starter_deck",
        "msrp": 17.99,
        "target_tcin": "92100011",
        "packaging_keywords": ["one piece", "starter", "yamato", "st-09"],
    },
]


def seed_products():
    """Upsert products into Supabase."""
    print(f"Seeding {len(PRODUCTS)} restock products...")

    # Add defaults
    for p in PRODUCTS:
        p.setdefault("is_active", True)
        p.setdefault("image_url", None)
        p.setdefault("upc", None)
        p.setdefault("target_dpci", None)
        p.setdefault("target_tcin", None)
        p.setdefault("walmart_sku", None)
        p.setdefault("walmart_url", None)

    url = f"{SUPABASE_URL}/rest/v1/restock_products"
    resp = requests.post(url, headers=HEADERS, json=PRODUCTS)

    if resp.status_code < 300:
        print(f"   {len(PRODUCTS)} products upserted")
    else:
        print(f"   Error: {resp.status_code} {resp.text[:300]}")


# ── Target Store Locator ────────────────────────────────────────

def fetch_target_stores(zip_code="75080", radius_miles=50):
    """
    Fetch Target store locations using Target's store locator API.
    This is the same API Target.com uses for "Find a store."
    """
    print(f"\nFetching Target stores near {zip_code} ({radius_miles} mi)...")

    url = "https://redsky.target.com/redsky_aggregations/v1/web/store_location_v1"
    params = {
        "key": "ff457966e64d5e877fdbad070f276d18ecec4a01",
        "place": zip_code,
        "within": str(radius_miles),
        "limit": "50",
    }

    try:
        resp = requests.get(url, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
        })
        resp.raise_for_status()
        data = resp.json()

        locations = data.get("data", {}).get("nearby_stores", {}).get("locations", [])
        stores = []

        for loc in locations:
            store = loc.get("store", {})
            address = store.get("mailing_address", {})

            stores.append({
                "retailer": "target",
                "store_number": f"T-{store.get('store_id', '')}",
                "name": f"Target {store.get('store_name', '')}",
                "address": address.get("address_line1", ""),
                "city": address.get("city", ""),
                "state": address.get("state", ""),
                "zip_code": address.get("postal_code", "")[:5],
                "lat": float(store.get("geographic_specifications", {}).get("latitude", 0)),
                "lng": float(store.get("geographic_specifications", {}).get("longitude", 0)),
                "phone": store.get("telephone_number", None),
                "target_location_id": str(store.get("location_id", store.get("store_id", ""))),
            })

        print(f"   Found {len(stores)} Target stores")
        return stores

    except Exception as e:
        print(f"   Target store locator error: {e}")
        return []


def seed_stores(zip_code="75080"):
    """Seed nearby Target stores into Supabase."""
    stores = fetch_target_stores(zip_code)

    if not stores:
        print("   No stores to seed")
        return

    # Also add a few manual Walmart entries
    walmart_stores = [
        {
            "retailer": "walmart",
            "store_number": "W-5840",
            "name": "Walmart Supercenter Richardson",
            "address": "501 S Plano Rd",
            "city": "Richardson",
            "state": "TX",
            "zip_code": "75081",
            "lat": 32.9393,
            "lng": -96.7130,
            "phone": "(972) 238-9091",
            "walmart_store_id": "5840",
        },
        {
            "retailer": "walmart",
            "store_number": "W-3550",
            "name": "Walmart Supercenter Plano",
            "address": "6001 N Central Expy",
            "city": "Plano",
            "state": "TX",
            "zip_code": "75023",
            "lat": 33.0500,
            "lng": -96.7500,
            "phone": "(972) 509-0029",
            "walmart_store_id": "3550",
        },
    ]

    all_stores = stores + walmart_stores

    print(f"\nSeeding {len(all_stores)} stores ({len(stores)} Target + {len(walmart_stores)} Walmart)...")

    url = f"{SUPABASE_URL}/rest/v1/retail_stores"
    resp = requests.post(url, headers=HEADERS, json=all_stores)

    if resp.status_code < 300:
        print(f"   {len(all_stores)} stores upserted")
    else:
        print(f"   Error: {resp.status_code} {resp.text[:300]}")


# ── Main ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("CollectLocal — Restock Seed Script")
    print("=" * 60)

    seed_products()

    # Get zip code from env or use default (Dallas area)
    zip_code = os.environ.get("SEED_ZIP_CODE", "75080")
    seed_stores(zip_code)

    print(f"\n{'=' * 60}")
    print("Seeding complete!")
    print("   Next steps:")
    print("   1. Run the SQL migration in Supabase SQL Editor (if not done)")
    print("   2. Verify products: visit your Supabase dashboard > Table Editor > restock_products")
    print("   3. Verify stores: visit your Supabase dashboard > Table Editor > retail_stores")
    print("   4. Test the checker: python restock_checker.py")
    print(f"{'=' * 60}\n")
