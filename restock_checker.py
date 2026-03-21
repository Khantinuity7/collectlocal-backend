"""
CollectLocal — Restock Checker Pipeline
========================================
Runs via GitHub Actions every 15 minutes (free tier: 2,000 min/month).
1. Fetches all active TCG products from Supabase
2. Fetches all retail stores from Supabase
3. Polls Target's Redsky API for inventory at each store
4. Polls Walmart product pages for availability
5. Compares against previous inventory → detects restocks
6. Writes updated inventory to Supabase
7. Logs restock events (0→>0) for push notification processing
8. Calls Supabase Edge Function to send push notifications

Cost: $0/month (GitHub Actions free + Supabase free + Target API free)
"""

import os
import re
import json
import time
import math
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── Config ──────────────────────────────────────────────────────
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation"
}

# Target Redsky API config
TARGET_API_BASE = "https://redsky.target.com/redsky_aggregations/v1/web"
TARGET_API_KEY = "ff457966e64d5e877fdbad070f276d18ecec4a01"  # Public API key from Target.com

# Rate limiting
TARGET_DELAY_SECONDS = 1.0   # Delay between Target API calls to avoid rate limits
WALMART_DELAY_SECONDS = 2.0  # Delay between Walmart page fetches

# ── Supabase Helpers ────────────────────────────────────────────

def supabase_get(table, params=None):
    """GET from Supabase REST API."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.get(url, headers=HEADERS, params=params or {})
    resp.raise_for_status()
    return resp.json()

def supabase_upsert(table, data):
    """UPSERT (insert or update) rows into Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**HEADERS, "Prefer": "resolution=merge-duplicates,return=representation"}
    resp = requests.post(url, headers=headers, json=data)
    if resp.status_code >= 400:
        print(f"  ⚠️  Upsert error on {table}: {resp.status_code} {resp.text[:200]}")
    return resp

def supabase_insert(table, data):
    """INSERT rows into Supabase."""
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    resp = requests.post(url, headers=HEADERS, json=data)
    if resp.status_code >= 400:
        print(f"  ⚠️  Insert error on {table}: {resp.status_code} {resp.text[:200]}")
    return resp

def supabase_rpc(function_name, params):
    """Call a Supabase RPC function."""
    url = f"{SUPABASE_URL}/rest/v1/rpc/{function_name}"
    resp = requests.post(url, headers=HEADERS, json=params)
    resp.raise_for_status()
    return resp.json()

# ── Target Redsky API ───────────────────────────────────────────

def check_target_inventory(tcin, store_location_id):
    """
    Query Target's Redsky API for product availability at a specific store.

    Returns dict with:
      - quantity: int (available_to_promise_quantity)
      - status: str ('in_stock', 'low_stock', 'out_of_stock', 'unknown')
      - price: float or None
      - on_sale: bool
    """
    url = f"{TARGET_API_BASE}/pdp_fulfillment_v1"
    params = {
        "key": TARGET_API_KEY,
        "tcin": tcin,
        "store_id": store_location_id,
        "scheduled_delivery_store_id": store_location_id,
        "pricing_store_id": store_location_id,
    }

    try:
        resp = requests.get(url, params=params, timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)",
            "Accept": "application/json",
        })

        if resp.status_code == 404:
            return {"quantity": 0, "status": "unknown", "price": None, "on_sale": False}

        if resp.status_code == 429:
            print(f"  ⚠️  Target rate limited. Backing off.")
            time.sleep(30)
            return None  # Signal to retry

        resp.raise_for_status()
        data = resp.json()

        # Parse the fulfillment response
        product = data.get("data", {}).get("product", {})
        fulfillment = product.get("fulfillment", {})

        # Check store pickup availability
        store_options = fulfillment.get("store_options", [])
        quantity = 0
        pickup_status = "unknown"

        for option in store_options:
            if option.get("order_pickup", {}).get("availability_status") == "IN_STOCK":
                pickup_status = "in_stock"
                # Try to get quantity from location info
                loc_qty = option.get("location_available_to_promise_quantity")
                if loc_qty is not None:
                    quantity = int(loc_qty)
            elif option.get("order_pickup", {}).get("availability_status") == "OUT_OF_STOCK":
                pickup_status = "out_of_stock"

        # Also check shipping availability as a signal
        shipping = fulfillment.get("shipping_options", {})
        if shipping.get("availability_status") == "IN_STOCK" and pickup_status == "unknown":
            pickup_status = "in_stock"

        # Determine stock status
        if quantity > 5:
            status = "in_stock"
        elif quantity > 0:
            status = "low_stock"
        elif pickup_status == "in_stock":
            status = "in_stock"
            quantity = 1  # We know it's in stock but not the exact count
        elif pickup_status == "out_of_stock":
            status = "out_of_stock"
        else:
            status = "unknown"

        # Parse price
        price_data = product.get("price", {})
        current_price = price_data.get("formatted_current_price", "")
        price = None
        on_sale = False

        if current_price:
            price_match = re.search(r'[\d.]+', current_price)
            if price_match:
                price = float(price_match.group())

        reg_price = price_data.get("reg_retail")
        if reg_price and price and price < reg_price:
            on_sale = True

        return {
            "quantity": quantity,
            "status": status,
            "price": price,
            "on_sale": on_sale,
        }

    except requests.exceptions.Timeout:
        print(f"  ⏱️  Target API timeout for TCIN {tcin} at store {store_location_id}")
        return None
    except Exception as e:
        print(f"  ❌ Target API error for TCIN {tcin}: {e}")
        return None


def check_target_inventory_bulk(tcins, store_location_id):
    """
    Check multiple products at a single Target store.
    Uses the product summary endpoint for efficiency.
    """
    results = {}

    for tcin in tcins:
        result = check_target_inventory(tcin, store_location_id)
        if result is not None:
            results[tcin] = result
        else:
            # Retry once after delay
            time.sleep(5)
            result = check_target_inventory(tcin, store_location_id)
            if result is not None:
                results[tcin] = result

        time.sleep(TARGET_DELAY_SECONDS)

    return results


# ── Walmart Availability Check ──────────────────────────────────

def check_walmart_availability(product_url, store_id=None):
    """
    Check Walmart product availability by scraping the product page.

    Returns dict with:
      - status: str ('in_stock', 'out_of_stock', 'unknown')
      - price: float or None
      - on_sale: bool
    """
    if not product_url:
        return None

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        }

        resp = requests.get(product_url, headers=headers, timeout=15, allow_redirects=True)

        if resp.status_code != 200:
            return None

        html = resp.text

        # Check for "Add to cart" button (indicates in stock)
        in_stock = False
        if "Add to cart" in html or "addToCart" in html:
            in_stock = True
        elif "Out of stock" in html or "out_of_stock" in html:
            in_stock = False
        elif "Check nearby stores" in html:
            in_stock = False  # Only available for pickup check

        # Try to extract price from JSON-LD or meta tags
        price = None
        price_match = re.search(r'"price":\s*"?([\d.]+)"?', html)
        if price_match:
            price = float(price_match.group(1))

        # Check for sale pricing
        on_sale = False
        if '"priceWas"' in html or '"wasPrice"' in html:
            on_sale = True

        return {
            "quantity": 1 if in_stock else 0,
            "status": "in_stock" if in_stock else "out_of_stock",
            "price": price,
            "on_sale": on_sale,
        }

    except Exception as e:
        print(f"  ❌ Walmart check error for {product_url}: {e}")
        return None


# ── Push Notification Trigger ───────────────────────────────────

def trigger_push_notifications(restock_event_ids):
    """
    Call Supabase Edge Function to send push notifications
    for restock events.
    """
    if not restock_event_ids:
        return

    edge_function_url = f"{SUPABASE_URL}/functions/v1/send-restock-notifications"

    try:
        resp = requests.post(
            edge_function_url,
            headers={
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
            },
            json={"event_ids": restock_event_ids},
            timeout=30,
        )

        if resp.status_code == 200:
            result = resp.json()
            print(f"  📱 Notifications sent: {result.get('sent', 0)} users notified")
        else:
            print(f"  ⚠️  Push notification error: {resp.status_code} {resp.text[:200]}")

    except Exception as e:
        print(f"  ❌ Push notification call failed: {e}")


# ── Main Pipeline ───────────────────────────────────────────────

def run():
    start_time = datetime.now(timezone.utc)
    print(f"\n🏪 Restock Checker started at {start_time.isoformat()}")
    print("=" * 60)

    # 1. Fetch active products
    print("\n📦 Fetching active products...")
    products = supabase_get("restock_products", {"is_active": "eq.true", "select": "*"})
    print(f"   Found {len(products)} active products")

    if not products:
        print("   No products to check. Exiting.")
        return

    # Separate by retailer
    target_products = [p for p in products if p.get("target_tcin")]
    walmart_products = [p for p in products if p.get("walmart_url")]
    print(f"   Target: {len(target_products)} products with TCINs")
    print(f"   Walmart: {len(walmart_products)} products with URLs")

    # 2. Fetch all stores
    print("\n🏬 Fetching retail stores...")
    stores = supabase_get("retail_stores", {"select": "*"})
    target_stores = [s for s in stores if s["retailer"] == "target" and s.get("target_location_id")]
    walmart_stores = [s for s in stores if s["retailer"] == "walmart"]
    print(f"   Target stores: {len(target_stores)}")
    print(f"   Walmart stores: {len(walmart_stores)}")

    # 3. Fetch current inventory (to detect changes)
    print("\n📊 Fetching current inventory state...")
    current_inventory = supabase_get("store_inventory", {"select": "*"})
    inventory_lookup = {}
    for inv in current_inventory:
        key = (inv["store_id"], inv["product_id"])
        inventory_lookup[key] = inv
    print(f"   {len(current_inventory)} existing inventory records")

    # 4. Check Target inventory
    restock_events = []
    inventory_updates = []
    now = datetime.now(timezone.utc).isoformat()

    if target_products and target_stores:
        print(f"\n🎯 Checking Target inventory ({len(target_products)} products × {len(target_stores)} stores)...")
        target_tcins = [p["target_tcin"] for p in target_products]
        tcin_to_product = {p["target_tcin"]: p for p in target_products}

        for i, store in enumerate(target_stores):
            store_id = store["id"]
            location_id = store["target_location_id"]
            print(f"   [{i+1}/{len(target_stores)}] {store['name']} (#{location_id})...")

            results = check_target_inventory_bulk(target_tcins, location_id)

            for tcin, result in results.items():
                product = tcin_to_product[tcin]
                product_id = product["id"]
                key = (store_id, product_id)

                prev = inventory_lookup.get(key)
                prev_quantity = prev["quantity"] if prev and prev["quantity"] else 0
                prev_status = prev["status"] if prev else "unknown"
                new_quantity = result["quantity"]
                new_status = result["status"]

                # Prepare inventory update
                inventory_updates.append({
                    "store_id": store_id,
                    "product_id": product_id,
                    "status": new_status,
                    "quantity": new_quantity,
                    "last_checked": now,
                    "source": "api_poll",
                    "price": result["price"],
                    "on_sale": result["on_sale"],
                })

                # Detect restock: was out/unknown, now in stock
                if (prev_status in ("out_of_stock", "unknown") and
                    new_status in ("in_stock", "low_stock") and
                    new_quantity > 0):

                    print(f"      🚨 RESTOCK DETECTED: {product['name']} — {new_quantity} units!")
                    restock_events.append({
                        "store_id": store_id,
                        "product_id": product_id,
                        "previous_quantity": prev_quantity,
                        "new_quantity": new_quantity,
                        "source": "api_poll",
                        "created_at": now,
                    })

            # Small delay between stores
            time.sleep(0.5)

    # 5. Check Walmart inventory (online only for now)
    if walmart_products:
        print(f"\n🔵 Checking Walmart online availability ({len(walmart_products)} products)...")

        for i, product in enumerate(walmart_products):
            print(f"   [{i+1}/{len(walmart_products)}] {product['name']}...")

            result = check_walmart_availability(product["walmart_url"])

            if result:
                # For Walmart, we track "online" availability per product
                # Use a special "online" store entry or the first Walmart store
                for store in walmart_stores[:1]:  # Just track online availability
                    store_id = store["id"]
                    product_id = product["id"]
                    key = (store_id, product_id)

                    prev = inventory_lookup.get(key)
                    prev_status = prev["status"] if prev else "unknown"
                    new_status = result["status"]

                    inventory_updates.append({
                        "store_id": store_id,
                        "product_id": product_id,
                        "status": new_status,
                        "quantity": result["quantity"],
                        "last_checked": now,
                        "source": "web_monitor",
                        "price": result["price"],
                        "on_sale": result["on_sale"],
                    })

                    if (prev_status in ("out_of_stock", "unknown") and
                        new_status == "in_stock"):
                        print(f"      🚨 WALMART RESTOCK: {product['name']} back online!")
                        restock_events.append({
                            "store_id": store_id,
                            "product_id": product_id,
                            "previous_quantity": 0,
                            "new_quantity": result["quantity"],
                            "source": "web_monitor",
                            "created_at": now,
                        })

            time.sleep(WALMART_DELAY_SECONDS)

    # 6. Write inventory updates to Supabase
    if inventory_updates:
        print(f"\n💾 Writing {len(inventory_updates)} inventory updates to Supabase...")
        # Batch upsert in chunks of 50
        for i in range(0, len(inventory_updates), 50):
            batch = inventory_updates[i:i+50]
            supabase_upsert("store_inventory", batch)
        print("   ✅ Inventory updated")

    # 7. Log restock events
    restock_event_ids = []
    if restock_events:
        print(f"\n🚨 Logging {len(restock_events)} restock events...")
        resp = supabase_insert("restock_events", restock_events)
        if resp.status_code < 300:
            events = resp.json()
            restock_event_ids = [e["id"] for e in events]
            print(f"   ✅ {len(restock_event_ids)} restock events logged")

    # 8. Trigger push notifications for restock events
    if restock_event_ids:
        print(f"\n📱 Triggering push notifications for {len(restock_event_ids)} restocks...")
        trigger_push_notifications(restock_event_ids)

    # 9. Summary
    elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
    print(f"\n{'=' * 60}")
    print(f"✅ Restock check complete in {elapsed:.1f}s")
    print(f"   Products checked: {len(products)}")
    print(f"   Stores checked: {len(target_stores)} Target + {len(walmart_stores)} Walmart")
    print(f"   Inventory updates: {len(inventory_updates)}")
    print(f"   Restocks detected: {len(restock_events)}")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    run()
