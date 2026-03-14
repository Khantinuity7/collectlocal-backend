/**
 * CollectLocal — TCGPlayer Market Price Edge Function
 * ====================================================
 * On-demand market price lookup via TCGTracking Open API.
 * Free, no auth, CORS enabled, Cloudflare CDN, no rate limits.
 *
 * Endpoints:
 *   POST /tcgplayer-price
 *   Body: { cardName: string, setName: string, tcg?: string, cardNumber?: string }
 *   Returns: { marketPrice, lowPrice, subType, skus[], allSubtypes, tcgplayerUrl }
 *
 * Data source: tcgtracking.com/tcgapi — TCGPlayer + Manapool + CardTrader data.
 * 55 games, 423K+ products, 6.9M+ SKUs. Updated daily 8 AM EST.
 * Cost: $0 (Supabase Edge Functions free tier: 500K invocations/month)
 */

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const TCGTRACK_BASE = "https://tcgtracking.com/tcgapi/v1";

// TCGPlayer category IDs
const CATEGORIES: Record<string, number> = {
  pokemon: 3,
  onepiece: 73,
  lorcana: 86,
  yugioh: 2,
  magic: 1,
};

// In-memory cache (persists for the life of the edge function instance)
const setsCache: Record<number, { data: Record<string, any>; ts: number }> = {};
const productsCache: Record<string, { data: Record<string, any>; ts: number }> = {};
const pricingCache: Record<string, { data: Record<string, any>; ts: number }> = {};
const skuCache: Record<string, { data: Record<string, any>; ts: number }> = {};

const CACHE_TTL = 4 * 60 * 60 * 1000; // 4 hours

function isFresh(ts: number): boolean {
  return Date.now() - ts < CACHE_TTL;
}

// ── Data loaders ─────────────────────────────────────────────

async function loadSets(catId: number): Promise<Record<string, any>> {
  if (setsCache[catId] && isFresh(setsCache[catId].ts)) return setsCache[catId].data;

  const resp = await fetch(`${TCGTRACK_BASE}/${catId}/sets`);
  if (!resp.ok) return {};
  const json = await resp.json();
  const sets: Record<string, any> = {};
  for (const s of json.sets || []) {
    sets[s.name.toLowerCase()] = s;
  }
  setsCache[catId] = { data: sets, ts: Date.now() };
  return sets;
}

async function loadProducts(catId: number, setId: number): Promise<{ byName: Record<string, any>; byNumber: Record<string, any>; list: any[] }> {
  const key = `${catId}-${setId}`;
  if (productsCache[key] && isFresh(productsCache[key].ts)) return productsCache[key].data;

  const resp = await fetch(`${TCGTRACK_BASE}/${catId}/sets/${setId}`);
  if (!resp.ok) return { byName: {}, byNumber: {}, list: [] };
  const json = await resp.json();
  const byName: Record<string, any> = {};
  const byNumber: Record<string, any> = {};
  const list: any[] = json.products || [];
  for (const p of list) {
    const clean = (p.clean_name || p.name).toLowerCase();
    byName[clean] = p;
    byName[p.name.toLowerCase()] = p;
    if (p.number) {
      byNumber[p.number.toLowerCase()] = p;
    }
  }
  const data = { byName, byNumber, list };
  productsCache[key] = { data, ts: Date.now() };
  return data;
}

async function loadPricing(catId: number, setId: number): Promise<Record<string, any>> {
  const key = `${catId}-${setId}`;
  if (pricingCache[key] && isFresh(pricingCache[key].ts)) return pricingCache[key].data;

  const resp = await fetch(`${TCGTRACK_BASE}/${catId}/sets/${setId}/pricing`);
  if (!resp.ok) return {};
  const json = await resp.json();
  const prices = json.prices || {};
  pricingCache[key] = { data: prices, ts: Date.now() };
  return prices;
}

async function loadSkus(catId: number, setId: number): Promise<Record<string, any>> {
  const key = `${catId}-${setId}`;
  if (skuCache[key] && isFresh(skuCache[key].ts)) return skuCache[key].data;

  const resp = await fetch(`${TCGTRACK_BASE}/${catId}/sets/${setId}/skus`);
  if (!resp.ok) return {};
  const json = await resp.json();
  const products = json.products || {};
  skuCache[key] = { data: products, ts: Date.now() };
  return products;
}

// ── Match helpers ────────────────────────────────────────────

function findSet(sets: Record<string, any>, name: string): any | null {
  const lower = name.toLowerCase();
  if (sets[lower]) return sets[lower];
  for (const [sname, sdata] of Object.entries(sets)) {
    if (lower.includes(sname) || sname.includes(lower)) return sdata;
  }
  return null;
}

function findProduct(
  productsData: { byName: Record<string, any>; byNumber: Record<string, any>; list: any[] },
  name: string,
  cardNumber?: string,
): any | null {
  const { byName, byNumber, list } = productsData;

  // 1. Exact match by card number (most reliable)
  if (cardNumber) {
    const num = cardNumber.toLowerCase();
    if (byNumber[num]) return byNumber[num];
    // Try matching product name that ends with "- {number}"
    for (const p of list) {
      if (p.number && p.number.toLowerCase() === num) return p;
    }
  }

  // 2. Exact match by name
  const lower = name.toLowerCase();
  if (byName[lower]) return byName[lower];

  // 3. If cardNumber provided, try name + number combo (e.g. "Charizard ex - 215/197")
  if (cardNumber) {
    const withNumber = `${lower} - ${cardNumber.toLowerCase()}`;
    if (byName[withNumber]) return byName[withNumber];
    // Fuzzy: find product whose name contains both the card name and number
    for (const p of list) {
      const pLower = p.name.toLowerCase();
      if (pLower.includes(lower) && p.number && p.number.toLowerCase() === cardNumber.toLowerCase()) {
        return p;
      }
    }
  }

  // 4. Fuzzy name match (fallback)
  for (const [pname, pdata] of Object.entries(byName)) {
    if (lower.includes(pname) || pname.includes(lower)) return pdata;
  }
  return null;
}

function pickBestSubtype(tcgPrices: Record<string, any>): [string, any] | null {
  const preferred = [
    "Holofoil", "Reverse Holofoil", "Normal", "Foil",
    "1st Edition Holofoil", "1st Edition Normal",
    "Unlimited Holofoil", "Unlimited Normal",
  ];
  for (const pref of preferred) {
    if (tcgPrices[pref]?.market != null) return [pref, tcgPrices[pref]];
  }
  for (const [sub, data] of Object.entries(tcgPrices)) {
    if ((data as any).market != null) return [sub, data];
  }
  return null;
}

// ── Main handler ─────────────────────────────────────────────

serve(async (req: Request) => {
  const corsHeaders = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  };

  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const { cardName, setName, tcg = "pokemon", cardNumber } = await req.json();

    if (!cardName) {
      return new Response(
        JSON.stringify({ error: "cardName is required" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const catId = CATEGORIES[tcg.toLowerCase()];
    if (!catId) {
      return new Response(
        JSON.stringify({ error: `Unknown TCG: ${tcg}` }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // 1. Find set
    const sets = await loadSets(catId);
    const setData = setName ? findSet(sets, setName) : null;
    if (!setData) {
      return new Response(
        JSON.stringify({ error: `Set not found: ${setName}`, marketPrice: null }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }
    const setId = setData.id ?? setData.set_id ?? setData.groupId;

    // 2. Find product (by number first if available, then by name)
    const products = await loadProducts(catId, setId);
    const product = findProduct(products, cardName, cardNumber);
    if (!product) {
      return new Response(
        JSON.stringify({ error: `Card not found: ${cardName}`, marketPrice: null }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }
    const productId = String(product.id);

    // 3. Get pricing (fetch pricing + SKUs in parallel)
    const [pricing, skuData] = await Promise.all([
      loadPricing(catId, setId),
      loadSkus(catId, setId),
    ]);

    const productPricing = pricing[productId] || {};
    const tcgPrices = productPricing.tcg || {};
    const best = pickBestSubtype(tcgPrices);

    if (!best) {
      return new Response(
        JSON.stringify({ error: "No market price available", marketPrice: null }),
        { status: 200, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const [bestSubtype, bestPriceData] = best;

    // 4. Format SKU data
    const productSkus = skuData[productId] || {};
    const skus = Object.entries(productSkus).map(([skuId, sku]: [string, any]) => ({
      skuId,
      condition: sku.cnd || "",
      variant: sku.var || "",
      language: sku.lng || "",
      marketPrice: sku.mkt,
      lowPrice: sku.low,
      highPrice: sku.hi,
      listingCount: sku.cnt || 0,
    }));

    // 5. Return full price data
    const result = {
      marketPrice: bestPriceData.market,
      lowPrice: bestPriceData.low,
      subType: bestSubtype,
      tcgplayerUrl: product.tcgplayer_url || product.url || "",
      manapoolUrl: product.manapool_url || "",
      imageUrl: product.image_url || product.imageUrl || "",
      productId: product.id,
      setName: setData.name,
      number: product.number || "",
      rarity: product.rarity || "",
      // All subtype prices
      allSubtypes: Object.fromEntries(
        Object.entries(tcgPrices).map(([sub, data]: [string, any]) => [
          sub, { market: data.market, low: data.low }
        ])
      ),
      // Manapool prices
      manapoolPrices: productPricing.manapool || {},
      // SKU-level pricing (condition/variant/language)
      skus,
    };

    return new Response(JSON.stringify(result), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: String(err), marketPrice: null }),
      { status: 500, headers: { "Content-Type": "application/json" } }
    );
  }
});
