// CollectLocal — Lot Analyzer Edge Function
// Receives a base64 image from the iOS app, sends it to Gemini 2.5 Flash Lite
// for multi-card identification, then prices each card via the TCGTracking API.
//
// Input:  { image: string (base64), mimeType?: string, description?: string }
// Output: { cards: LotCard[], totalVisible: number, totalMentioned: number, unidentifiedCount: number, totalEstimatedValue: number }

import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const GEMINI_API_KEY = Deno.env.get("GEMINI_API_KEY") ?? "";
const GEMINI_MODEL = "gemini-2.5-flash-lite";
const GEMINI_URL = `https://generativelanguage.googleapis.com/v1beta/models/${GEMINI_MODEL}:generateContent?key=${GEMINI_API_KEY}`;

// TCGTracking API (free, no auth)
const TCGTRACKING_BASE = "https://tcgtracking.com/tcgapi/v1";
const POKEMON_CATEGORY_ID = 3;

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

// ── Gemini Vision Prompt ────────────────────────────────────────

const LOT_ANALYSIS_PROMPT = `You are analyzing a trading card LOT listing — a bundle of multiple cards sold together.

Examine ALL images carefully and identify EVERY individual trading card you can see.
Also extract any card names mentioned in the description that may not be visible in the photos.

Return a JSON object with this exact structure:
{
  "cards": [
    {
      "card_name": "Full card name (e.g., 'Umbreon VMAX Alt Art', 'Charizard V')",
      "set_name": "Set name if identifiable (e.g., 'Evolving Skies', 'Brilliant Stars')",
      "card_number": "Card number if visible (e.g., '215/203', '25/165')",
      "estimated_grade": "Condition estimate: 'PSA 10', 'BGS 9.5', 'NM', 'LP', 'MP', 'HP', or 'Raw'",
      "confidence": 0.95,
      "source_type": "vision"
    }
  ],
  "total_visible": 8,
  "total_mentioned": 0,
  "unidentified_count": 3
}

Rules:
- List EVERY card you can identify, sorted by estimated value (highest first)
- confidence is a float 0-1: 0.9+ = very sure, 0.7-0.9 = fairly sure, below 0.7 = uncertain
- source_type: "vision" if identified from photo, "text" if from description only, "both" if in both
- If cards are stacked/overlapping, identify what you can see and report others as unidentified_count
- For cards in PSA/BGS/CGC slabs, read the grade from the label
- For raw cards, estimate condition (NM, LP, etc.) based on visible wear
- total_visible = cards you can count in the photos (even if you can't identify all of them)
- total_mentioned = additional cards mentioned ONLY in text (not already counted in photos)
- unidentified_count = cards you can see but cannot confidently identify
- Return ONLY the JSON object, no other text`;

// ── Price Lookup via TCGTracking ────────────────────────────────

interface GeminiCard {
  card_name: string;
  set_name: string;
  card_number: string;
  estimated_grade: string;
  confidence: number;
  source_type: string;
}

interface PricedCard extends GeminiCard {
  market_price: number | null;
  price_source: string | null;
  ebay_url: string | null;
}

async function lookupPrice(card: GeminiCard): Promise<PricedCard> {
  const pricedCard: PricedCard = {
    ...card,
    market_price: null,
    price_source: null,
    ebay_url: null,
  };

  try {
    // Search TCGTracking for the card
    const query = encodeURIComponent(card.card_name);
    const searchResp = await fetch(
      `${TCGTRACKING_BASE}/${POKEMON_CATEGORY_ID}/search?q=${query}`,
      { signal: AbortSignal.timeout(8000) }
    );

    if (!searchResp.ok) return pricedCard;

    const searchData = await searchResp.json();
    const results = searchData?.results ?? searchData?.data ?? [];
    if (!Array.isArray(results) || results.length === 0) return pricedCard;

    // Find best match — prefer exact card number match
    let bestMatch = results[0];
    if (card.card_number) {
      const numMatch = results.find(
        (r: any) => r.number === card.card_number || r.cardNumber === card.card_number
      );
      if (numMatch) bestMatch = numMatch;
    }

    // Get market price from result
    const price =
      bestMatch.marketPrice ??
      bestMatch.market_price ??
      bestMatch.midPrice ??
      bestMatch.lowPrice ??
      null;

    if (price && price > 0) {
      pricedCard.market_price = price;
      pricedCard.price_source = "tcgtracking";
    }

    // Generate eBay search URL as fallback
    const ebayQuery = encodeURIComponent(
      `${card.card_name} ${card.set_name || ""} ${card.card_number || ""}`.trim()
    );
    pricedCard.ebay_url = `https://www.ebay.com/sch/i.html?_nkw=${ebayQuery}&_sacat=183454&LH_BIN=1&_sop=15`;
  } catch {
    // Non-fatal — return card without price
  }

  return pricedCard;
}

// ── Main Handler ────────────────────────────────────────────────

serve(async (req) => {
  // CORS preflight
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const { image, mimeType, description } = await req.json();

    if (!image) {
      return new Response(
        JSON.stringify({ error: "Missing 'image' (base64-encoded)" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    if (!GEMINI_API_KEY) {
      return new Response(
        JSON.stringify({ error: "GEMINI_API_KEY not configured" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    // Build Gemini multimodal request
    const parts: any[] = [
      {
        inlineData: {
          mimeType: mimeType || "image/jpeg",
          data: image,
        },
      },
    ];

    let prompt = LOT_ANALYSIS_PROMPT;
    if (description) {
      prompt += `\n\nListing description: "${description.slice(0, 1000)}"`;
    }
    parts.push({ text: prompt });

    // Call Gemini
    const geminiResp = await fetch(GEMINI_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        contents: [{ parts }],
        generationConfig: {
          maxOutputTokens: 2048,
          temperature: 0.1,
          responseMimeType: "application/json",
        },
      }),
      signal: AbortSignal.timeout(45000),
    });

    if (!geminiResp.ok) {
      const errText = await geminiResp.text();
      console.error(`Gemini error ${geminiResp.status}: ${errText.slice(0, 200)}`);
      return new Response(
        JSON.stringify({ error: "AI analysis failed", detail: errText.slice(0, 200) }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const geminiData = await geminiResp.json();
    const responseText =
      geminiData?.candidates?.[0]?.content?.parts?.[0]?.text ?? "";

    let lotData: any;
    try {
      lotData = JSON.parse(responseText);
    } catch {
      console.error("Failed to parse Gemini JSON:", responseText.slice(0, 300));
      return new Response(
        JSON.stringify({ error: "AI returned invalid response" }),
        { status: 502, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const cards: GeminiCard[] = lotData.cards ?? [];

    // Price each card in parallel (with 8-card concurrency limit)
    const pricedCards: PricedCard[] = [];
    const batchSize = 8;
    for (let i = 0; i < cards.length; i += batchSize) {
      const batch = cards.slice(i, i + batchSize);
      const batchResults = await Promise.all(batch.map(lookupPrice));
      pricedCards.push(...batchResults);
    }

    // Calculate totals
    const totalEstimatedValue = pricedCards.reduce(
      (sum, c) => sum + (c.market_price ?? 0),
      0
    );

    const result = {
      cards: pricedCards,
      total_visible: lotData.total_visible ?? cards.length,
      total_mentioned: lotData.total_mentioned ?? 0,
      unidentified_count: lotData.unidentified_count ?? 0,
      total_estimated_value: Math.round(totalEstimatedValue * 100) / 100,
      card_count: pricedCards.length,
      analysis_model: GEMINI_MODEL,
    };

    return new Response(JSON.stringify(result), {
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    console.error("analyze-lot error:", err);
    return new Response(
      JSON.stringify({ error: "Internal error", detail: String(err) }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
