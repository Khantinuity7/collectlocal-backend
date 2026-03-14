"""
CollectLocal — TCG Product Auto-Discovery
==========================================
Automatically discovers ALL Pokémon TCG and One Piece TCG sealed products
at Target and Walmart. No more manually hardcoding SKUs.

Strategy (3 sources, cross-referenced):

1. TARGET SEARCH API — Searches Target's Redsky plp_search endpoint for
   TCG keywords, extracts every TCIN + DPCI + price + image.

2. WALMART SEARCH — Scrapes Walmart.com search results for TCG products,
   extracts product IDs, URLs, prices.

3. DISTRIBUTOR CATALOGS — Uses the Pokémon TCG API + Bandai's product pages
   to get the canonical list of sets/products, then cross-references against
   Target and Walmart to fill in retailer-specific IDs.

Run weekly via GitHub Actions to catch new releases automatically.
Also runs on-demand: python discover_products.py

Cost: $0 (all free APIs + public search endpoints)
"""

import os
import re
import json
import time
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SUPA = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates,return=representation"
}

# Target Redsky API key (public, same one Target.com uses)
TARGET_API_KEY = "ff457966e64d5e877fdbad070f276d18ecec4a01"

# ── Shared Config ───────────────────────────────────────────────

# Search queries that cover all sealed TCG products at retail
POKEMON_QUERIES = [
    "pokemon trading card elite trainer box",
    "pokemon trading card booster box",
    "pokemon trading card booster bundle",
    "pokemon trading card collection box",
    "pokemon trading card blister pack",
    "pokemon trading card tin",
    "pokemon trading card premium collection",
    "pokemon tcg booster",
    "pokemon tcg etb",
    "pokemon scarlet violet",
    "pokemon prismatic evolutions",
]

ONE_PIECE_QUERIES = [
    "one piece trading card booster box",
    "one piece card game booster",
    "one piece tcg starter deck",
    "one piece card game starter",
    "one piece tcg booster box",
    "one piece card game collection",
]

# Product type detection from name
PRODUCT_TYPE_PATTERNS = [
    (r"elite trainer box|etb", "etb"),
    (r"booster box|booster display|\b36[\s-]?pack", "booster_box"),
    (r"booster bundle|6[\s-]?pack bundle", "booster_bundle"),
    (r"blister|3[\s-]?pack|single pack", "blister"),
    (r"collection box|premium collection", "collection_box"),
    (r"premium collection", "premium_collection"),
    (r"tin\b", "tin"),
    (r"starter deck|start deck", "starter_deck"),
    (r"binder", "binder_collection"),
]

# TCG game detection from name
TCG_PATTERNS = [
    (r"pok[eé]mon|pikachu|charizard|scarlet.*violet|prismatic|surging|twilight|shrouded", "pokemon"),
    (r"one piece|luffy|zoro|op-\d{2}|bandai.*card game", "one_piece"),
    (r"magic.*gathering|mtg", "mtg"),
    (r"yu-?gi-?oh|yugioh", "yugioh"),
    (r"dragon ball|dbz|dbs", "dragon_ball"),
    (r"lorcana|disney.*card", "lorcana"),
]

# Filter out non-sealed products (single cards, accessories, etc.)
EXCLUDE_PATTERNS = [
    r"card sleeve",
    r"card binder(?!.*collection)",
    r"deck box\b",
    r"playmat",
    r"card protector",
    r"top ?loader",
    r"graded card",
    r"single card",
    r"loose pack",
    r"used\b",
    r"custom\b",
    r"lot of\b",
    r"mystery\b.*(?:grab|box)",
]


def detect_product_type(name):
    """Detect product type from the product name."""
    name_lower = name.lower()
    for pattern, ptype in PRODUCT_TYPE_PATTERNS:
        if re.search(pattern, name_lower):
            return ptype
    return "other"


def detect_tcg(name):
    """Detect which TCG game a product belongs to."""
    name_lower = name.lower()
    for pattern, tcg in TCG_PATTERNS:
        if re.search(pattern, name_lower):
            return tcg
    return "unknown"


def is_sealed_product(name):
    """Check if this looks like a sealed TCG product (not accessories)."""
    name_lower = name.lower()
    for pattern in EXCLUDE_PATTERNS:
        if re.search(pattern, name_lower):
            return False
    # Must contain some TCG indicator
    return bool(detect_tcg(name) != "unknown")


def generate_keywords(name):
    """Generate packaging keywords from product name for AI shelf matching."""
    # Remove common filler words
    stop_words = {"the", "a", "an", "and", "or", "of", "for", "in", "with", "new", "trading", "card", "game", "cards", "games"}
    words = re.findall(r'[a-z]+', name.lower())
    keywords = [w for w in words if w not in stop_words and len(w) > 2]
    return list(dict.fromkeys(keywords))[:10]  # Dedupe, max 10


# ══════════════════════════════════════════════════════════════════
# SOURCE 1: TARGET SEARCH API
# ══════════════════════════════════════════════════════════════════

def search_target(keyword, count=24, offset=0):
    """
    Search Target's Redsky API for products matching a keyword.
    Returns list of product dicts with tcin, name, price, image, dpci.

    FIRST-PARTY FILTER: Only includes products sold by Target directly.
    Target's API includes a relationship_type_code field in the item data:
      - "SA" (Standard Assortment) = Sold by Target
      - "TAP" (Target Plus Partner) = Third-party marketplace seller
      - "TPCL" (Target Plus Clearance) = Third-party clearance

    We also check for is_marketplace and seller_name fields as fallbacks.
    Products with a DPCI (Department-Class-Item) code are almost always
    first-party Target items — marketplace items rarely have DPCIs.
    """
    url = "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v1"
    params = {
        "key": TARGET_API_KEY,
        "channel": "WEB",
        "keyword": keyword,
        "count": str(count),
        "offset": str(offset),
        "default_purchasability_filter": "true",
        "pricing_store_id": "3991",  # Default store for pricing
    }

    try:
        resp = requests.get(url, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })

        if resp.status_code == 429:
            print(f"    Rate limited on Target search. Waiting 60s...")
            time.sleep(60)
            return search_target(keyword, count, offset)

        if resp.status_code != 200:
            print(f"    Target search returned {resp.status_code}")
            return []

        data = resp.json()
        search_data = data.get("data", {}).get("search", {})
        products_raw = search_data.get("products", [])
        total = search_data.get("search_response", {}).get("typed_metadata", {}).get("total_results", 0)

        products = []
        for p in products_raw:
            item = p.get("item", {})
            price_data = p.get("price", {})

            tcin = item.get("tcin", "")
            name = item.get("product_description", {}).get("title", "")
            dpci = item.get("dpci", "")

            # FIRST-PARTY SELLER FILTER
            # Method 1: Check relationship_type_code
            relationship = item.get("relationship_type_code", "")
            if relationship and relationship in ("TAP", "TPCL"):
                continue  # Skip third-party marketplace sellers

            # Method 2: Check is_marketplace flag
            if item.get("is_marketplace") is True:
                continue

            # Method 3: Check for marketplace/seller info in fulfillment data
            fulfillment = p.get("fulfillment", {})
            seller = fulfillment.get("seller_name", "") or ""
            if seller and seller.lower() not in ("target", "target corporation", ""):
                continue  # Third-party seller

            # Method 4: Check for "Target Plus" or "Sold by" in product labels
            product_labels = item.get("product_description", {}).get("soft_bullets", {}).get("bullets", [])
            is_third_party = False
            for label in product_labels:
                label_lower = str(label).lower()
                if "target plus" in label_lower or "sold by " in label_lower:
                    if "sold by target" not in label_lower:
                        is_third_party = True
                        break
            if is_third_party:
                continue

            # Method 5: Price sanity check
            current_price = price_data.get("formatted_current_price", "")
            price = None
            if current_price:
                match = re.search(r'[\d.]+', current_price)
                if match:
                    price = float(match.group())

            if price and price > 400:
                print(f"    Skipping (price ${price:.2f} too high, likely 3P): {name[:50]}")
                continue

            # Get primary image
            images = item.get("enrichment", {}).get("images", {})
            image_url = images.get("primary_image_url", "")

            # Get UPC
            upc = item.get("primary_barcode", "")

            if tcin and name:
                products.append({
                    "tcin": tcin,
                    "name": name,
                    "dpci": dpci,
                    "price": price,
                    "image_url": image_url,
                    "upc": upc,
                    "relationship_type": relationship,
                    "source": "target",
                })

        return products, total

    except Exception as e:
        print(f"    Target search error for '{keyword}': {e}")
        return [], 0


def verify_target_first_party(tcin):
    """
    Double-check a single product by fetching its full PDP data.
    Returns True if the product is sold by Target (first-party).
    """
    url = "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
    params = {
        "key": TARGET_API_KEY,
        "tcin": tcin,
        "pricing_store_id": "3991",
    }

    try:
        resp = requests.get(url, params=params, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "application/json",
        })

        if resp.status_code != 200:
            return True  # If we can't verify, keep it

        data = resp.json()
        product = data.get("data", {}).get("product", {})

        item = product.get("item", {})
        relationship = item.get("relationship_type_code", "")
        if relationship in ("TAP", "TPCL"):
            return False

        desc = product.get("item", {}).get("product_description", {})
        title = desc.get("title", "").lower()

        fulfillment = product.get("fulfillment", {})
        shipping = fulfillment.get("shipping_options", {})
        is_store_pickup = bool(fulfillment.get("store_options", []))

        if is_store_pickup:
            return True

        return True  # Default to keeping it

    except Exception:
        return True


def discover_target_products(queries):
    """
    Search Target for all TCG products across multiple search queries.
    Deduplicates by TCIN.
    """
    all_products = {}

    for query in queries:
        print(f"  Searching Target: '{query}'...")
        offset = 0
        max_pages = 5  # 5 pages x 24 = 120 products per query

        while offset < max_pages * 24:
            products, total = search_target(query, count=24, offset=offset)

            if not products:
                break

            for p in products:
                tcin = p["tcin"]
                if tcin not in all_products and is_sealed_product(p["name"]):
                    all_products[tcin] = p
                    print(f"    [{len(all_products)}] {p['name'][:60]}... (TCIN: {tcin})")

            offset += 24
            if offset >= total:
                break

            time.sleep(2)  # Rate limit

        time.sleep(3)  # Pause between queries

    return list(all_products.values())


# ══════════════════════════════════════════════════════════════════
# SOURCE 2: WALMART SEARCH
# ══════════════════════════════════════════════════════════════════

def search_walmart(keyword, page=1):
    """
    Search Walmart.com for products.
    Extracts product data from the __NEXT_DATA__ JSON blob on search pages.

    FIRST-PARTY FILTER: Only includes products sold by Walmart.com directly.
    """
    url = f"https://www.walmart.com/search"
    params = {
        "q": keyword,
        "page": str(page),
        "facet": "retailer_id:0",
    }

    try:
        resp = requests.get(url, params=params, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })

        if resp.status_code != 200:
            print(f"    Walmart search returned {resp.status_code}")
            return []

        html = resp.text

        # Extract product data from __NEXT_DATA__ script tag
        next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if not next_data_match:
            return extract_walmart_from_html(html)

        try:
            next_data = json.loads(next_data_match.group(1))
            props = next_data.get("props", {}).get("pageProps", {})
            initial_data = props.get("initialData", {})
            search_result = initial_data.get("searchResult", {})
            items = search_result.get("itemStacks", [{}])[0].get("items", [])
        except (json.JSONDecodeError, KeyError, IndexError):
            return extract_walmart_from_html(html)

        products = []
        for item in items:
            name = item.get("name", "")
            product_id = item.get("usItemId", "") or item.get("id", "")
            price = item.get("priceInfo", {}).get("currentPrice", {}).get("price")
            image = item.get("imageInfo", {}).get("thumbnailUrl", "")
            product_url = item.get("canonicalUrl", "")

            if product_url and not product_url.startswith("http"):
                product_url = f"https://www.walmart.com{product_url}"

            # FIRST-PARTY SELLER FILTER
            # Method 1: Check sellerName / sellerDisplayName
            seller_name = (
                item.get("sellerName", "") or
                item.get("sellerDisplayName", "") or
                item.get("seller_name", "") or ""
            )
            seller_name_lower = seller_name.lower().strip()

            WALMART_SELLERS = {"walmart.com", "walmart", "walmart inc", "walmart inc."}

            if seller_name_lower and seller_name_lower not in WALMART_SELLERS:
                continue

            # Method 2: Check sellerId
            seller_id = str(item.get("sellerId", ""))
            if seller_id and seller_id not in ("0", ""):
                if not seller_name_lower:
                    continue

            # Method 3: Check sellerType field
            seller_type = item.get("sellerType", "") or item.get("seller_type", "")
            if seller_type.upper() == "EXTERNAL":
                continue

            # Method 4: Check for "Marketplace" flag
            if item.get("isMarketplace") is True:
                continue

            # Method 5: Check fulfillment info
            fulfillment_badges = item.get("fulfillmentBadgeGroups", [])
            has_walmart_fulfillment = False
            for badge_group in fulfillment_badges:
                for badge in badge_group.get("badges", []):
                    badge_text = str(badge.get("text", "")).lower()
                    if "walmart" in badge_text:
                        has_walmart_fulfillment = True
                    if "sold by" in badge_text and "walmart" not in badge_text:
                        continue

            # Method 6: Price sanity check
            if price and price > 400:
                print(f"    Skipping (price ${price:.2f} too high, likely 3P): {name[:50]}")
                continue

            if name and product_id:
                products.append({
                    "walmart_id": product_id,
                    "name": name,
                    "price": price,
                    "image_url": image,
                    "walmart_url": product_url,
                    "seller_name": seller_name,
                    "source": "walmart",
                })

        return products

    except Exception as e:
        print(f"    Walmart search error for '{keyword}': {e}")
        return []


def verify_walmart_first_party(product_url):
    """
    Double-check a Walmart product by fetching its product page.
    Returns True if sold by Walmart.com, False otherwise.
    """
    if not product_url:
        return False

    try:
        resp = requests.get(product_url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html",
        })

        if resp.status_code != 200:
            return True

        html = resp.text

        if "Sold and shipped by Walmart" in html or "Sold &amp; shipped by Walmart" in html:
            return True

        if "Sold by " in html and "Sold by Walmart" not in html:
            seller_match = re.search(r'Sold by\s+([^<"]+)', html)
            if seller_match:
                seller = seller_match.group(1).strip()
                if seller.lower() not in ("walmart.com", "walmart"):
                    return False

        next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html)
        if next_data_match:
            try:
                data = json.loads(next_data_match.group(1))
                product = (data.get("props", {}).get("pageProps", {})
                          .get("initialData", {}).get("data", {}).get("product", {}))

                seller = product.get("sellerDisplayName", "") or product.get("sellerName", "")
                if seller and seller.lower().strip() not in ("walmart.com", "walmart"):
                    return False

                offers = product.get("offers", []) or product.get("buyBoxOffers", [])
                for offer in offers:
                    offer_seller = offer.get("sellerName", "") or offer.get("sellerDisplayName", "")
                    if offer_seller.lower().strip() in ("walmart.com", "walmart"):
                        return True
                    elif offer_seller:
                        return False

            except (json.JSONDecodeError, KeyError):
                pass

        return True

    except Exception:
        return True


def extract_walmart_from_html(html):
    """Fallback: extract basic product info from Walmart HTML."""
    products = []

    links = re.findall(r'href="(/ip/[^"]+)"', html)
    names = re.findall(r'data-automation-id="product-title"[^>]*>([^<]+)', html)

    for i, link in enumerate(links[:20]):
        id_match = re.search(r'/ip/[^/]+/(\d+)', link)
        product_id = id_match.group(1) if id_match else ""
        name = names[i] if i < len(names) else ""

        if product_id and name:
            products.append({
                "walmart_id": product_id,
                "name": name,
                "price": None,
                "image_url": "",
                "walmart_url": f"https://www.walmart.com{link}",
                "seller_name": "",
                "source": "walmart",
            })

    return products


def discover_walmart_products(queries):
    """
    Search Walmart for all TCG products across multiple search queries.
    Deduplicates by Walmart product ID.
    """
    all_products = {}

    for query in queries:
        print(f"  Searching Walmart: '{query}' (Walmart.com seller only)...")

        for page in range(1, 4):
            products = search_walmart(query, page=page)

            if not products:
                break

            for p in products:
                wid = p["walmart_id"]
                if wid not in all_products and is_sealed_product(p["name"]):
                    seller = p.get("seller_name", "")
                    seller_label = f" [Seller: {seller}]" if seller else ""
                    all_products[wid] = p
                    print(f"    [{len(all_products)}] {p['name'][:55]}...{seller_label} (ID: {wid})")

            time.sleep(3)

        time.sleep(5)

    # Verify ambiguous products
    unverified = [p for p in all_products.values() if not p.get("seller_name")]
    if unverified:
        print(f"\n  Verifying {len(unverified)} products with unknown seller...")
        verified_products = {}
        for p in unverified:
            if verify_walmart_first_party(p["walmart_url"]):
                verified_products[p["walmart_id"]] = p
            else:
                print(f"    Removed (third-party): {p['name'][:55]}")
            time.sleep(2)

        for wid, p in all_products.items():
            if not p.get("seller_name") and wid not in verified_products:
                del all_products[wid]

    return list(all_products.values())


# ══════════════════════════════════════════════════════════════════
# SOURCE 3: POKEMON TCG API (canonical set/product catalog)
# ══════════════════════════════════════════════════════════════════

def fetch_pokemon_sets():
    """
    Fetch all Pokemon TCG sets from the free Pokemon TCG API.
    https://pokemontcg.io/
    """
    print("  Fetching Pokemon TCG set catalog...")

    url = "https://api.pokemontcg.io/v2/sets"
    params = {"orderBy": "-releaseDate", "pageSize": "50"}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        sets = data.get("data", [])

        recent_sets = []
        for s in sets:
            release = s.get("releaseDate", "")
            if release >= "2024-01-01":
                recent_sets.append({
                    "name": s.get("name", ""),
                    "series": s.get("series", ""),
                    "release_date": release,
                    "set_id": s.get("id", ""),
                })

        print(f"    Found {len(recent_sets)} recent Pokemon TCG sets")
        return recent_sets

    except Exception as e:
        print(f"    Pokemon TCG API error: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# MERGE & UPSERT
# ══════════════════════════════════════════════════════════════════

def merge_and_upsert(target_products, walmart_products, pokemon_sets):
    """
    Merge products from all sources, detect TCG + product type,
    cross-reference Target <-> Walmart where possible, and upsert to Supabase.
    """
    print(f"\nMerging {len(target_products)} Target + {len(walmart_products)} Walmart products...")

    merged = {}

    def normalize_name(name):
        """Create a fuzzy key for matching same product across retailers."""
        n = name.lower()
        n = re.sub(r'[^a-z0-9\s]', '', n)
        n = re.sub(r'\s+', ' ', n).strip()
        for remove in ["target exclusive", "walmart exclusive", "trading card game", "tcg", "card game"]:
            n = n.replace(remove, "")
        return n.strip()

    # Process Target products first
    for p in target_products:
        key = normalize_name(p["name"])
        tcg = detect_tcg(p["name"])
        ptype = detect_product_type(p["name"])

        if tcg in ("unknown",):
            continue

        merged[key] = {
            "name": p["name"],
            "tcg": tcg,
            "product_type": ptype,
            "msrp": p["price"] or 0,
            "upc": p.get("upc") or None,
            "target_dpci": p.get("dpci") or None,
            "target_tcin": p.get("tcin") or None,
            "walmart_sku": None,
            "walmart_url": None,
            "image_url": p.get("image_url") or None,
            "packaging_keywords": generate_keywords(p["name"]),
            "is_active": True,
        }

    # Cross-reference Walmart products
    for p in walmart_products:
        key = normalize_name(p["name"])
        tcg = detect_tcg(p["name"])

        if tcg in ("unknown",):
            continue

        if key in merged:
            merged[key]["walmart_sku"] = p.get("walmart_id")
            merged[key]["walmart_url"] = p.get("walmart_url")
            if not merged[key]["image_url"] and p.get("image_url"):
                merged[key]["image_url"] = p["image_url"]
            print(f"    Cross-matched: {p['name'][:50]}...")
        else:
            ptype = detect_product_type(p["name"])
            merged[key] = {
                "name": p["name"],
                "tcg": tcg,
                "product_type": ptype,
                "msrp": p.get("price") or 0,
                "upc": None,
                "target_dpci": None,
                "target_tcin": None,
                "walmart_sku": p.get("walmart_id"),
                "walmart_url": p.get("walmart_url"),
                "image_url": p.get("image_url") or None,
                "packaging_keywords": generate_keywords(p["name"]),
                "is_active": True,
            }

    # Generate additional search queries from Pokemon set names
    set_queries = []
    for s in pokemon_sets:
        set_name = s["name"]
        set_queries.extend([
            f"pokemon {set_name} elite trainer box",
            f"pokemon {set_name} booster",
        ])

    if set_queries:
        print(f"\nRunning {len(set_queries)} set-specific Target searches...")
        set_target = discover_target_products(set_queries[:20])
        for p in set_target:
            key = normalize_name(p["name"])
            if key not in merged:
                tcg = detect_tcg(p["name"])
                ptype = detect_product_type(p["name"])
                if tcg != "unknown":
                    merged[key] = {
                        "name": p["name"],
                        "tcg": tcg,
                        "product_type": ptype,
                        "msrp": p["price"] or 0,
                        "upc": p.get("upc") or None,
                        "target_dpci": p.get("dpci") or None,
                        "target_tcin": p.get("tcin") or None,
                        "walmart_sku": None,
                        "walmart_url": None,
                        "image_url": p.get("image_url") or None,
                        "packaging_keywords": generate_keywords(p["name"]),
                        "is_active": True,
                    }

    products = list(merged.values())

    # Filter: only Pokemon and One Piece
    products = [p for p in products if p["tcg"] in ("pokemon", "one_piece")]

    print(f"\nFinal product count: {len(products)}")
    print(f"   Pokemon: {sum(1 for p in products if p['tcg'] == 'pokemon')}")
    print(f"   One Piece: {sum(1 for p in products if p['tcg'] == 'one_piece')}")

    types = {}
    for p in products:
        types[p["product_type"]] = types.get(p["product_type"], 0) + 1
    for ptype, count in sorted(types.items()):
        print(f"   {ptype}: {count}")

    both = sum(1 for p in products if p["target_tcin"] and p["walmart_url"])
    target_only = sum(1 for p in products if p["target_tcin"] and not p["walmart_url"])
    walmart_only = sum(1 for p in products if not p["target_tcin"] and p["walmart_url"])
    print(f"\n   Both retailers: {both}")
    print(f"   Target only: {target_only}")
    print(f"   Walmart only: {walmart_only}")

    # Upsert to Supabase
    if products:
        print(f"\nUpserting {len(products)} products to Supabase...")
        url = f"{SUPABASE_URL}/rest/v1/restock_products"

        for i in range(0, len(products), 25):
            batch = products[i:i+25]
            resp = requests.post(url, headers=HEADERS_SUPA, json=batch)
            if resp.status_code < 300:
                print(f"   Batch {i//25 + 1}: {len(batch)} products upserted")
            else:
                print(f"   Batch {i//25 + 1} error: {resp.status_code} {resp.text[:200]}")

    return products


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════

def run():
    start = datetime.now(timezone.utc)
    print("=" * 60)
    print("CollectLocal — TCG Product Auto-Discovery")
    print(f"   Started: {start.isoformat()}")
    print("=" * 60)

    # Source 1: Target search
    print("\nPHASE 1: Target Product Discovery")
    print("-" * 40)
    all_queries = POKEMON_QUERIES + ONE_PIECE_QUERIES
    target_products = discover_target_products(all_queries)
    print(f"   Found {len(target_products)} Target products")

    # Source 2: Walmart search
    print("\nPHASE 2: Walmart Product Discovery")
    print("-" * 40)
    walmart_products = discover_walmart_products(all_queries)
    print(f"   Found {len(walmart_products)} Walmart products")

    # Source 3: Pokemon TCG API
    print("\nPHASE 3: Pokemon TCG Set Catalog")
    print("-" * 40)
    pokemon_sets = fetch_pokemon_sets()
    print(f"   Found {len(pokemon_sets)} recent sets")

    # Merge and upsert
    print("\nPHASE 4: Merge & Upsert")
    print("-" * 40)
    products = merge_and_upsert(target_products, walmart_products, pokemon_sets)

    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"Discovery complete in {elapsed:.0f}s")
    print(f"   Total products discovered: {len(products)}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    run()
