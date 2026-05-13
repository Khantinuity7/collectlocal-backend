# CollectLocal Backend — Pivot Deprecation Inventory

**Date:** 2026-05-13
**Source memo:** `/Users/larry/IOS APP/CardLocal/CollectLocal/docs/strategy/PIVOT_STRATEGY_MEMO.md`
**Status:** Inventory only — nothing has been deleted. Awaiting Omar's review.
**Companion branch (iOS):** `chore/pivot-deprecation-sprint` in the iOS repo, blocked on the migration deliverables in Section J below.

This document is a walked end-to-end inventory of every artifact in this repo that the pivot kills, retains, or flags for an explicit decision. The structure mirrors the memo's deprecation list and adds the categories the brief asked for.

---

## Notable findings before the categorized inventory

1. **The "Claude Haiku FB listing parser prompt" referenced in the memo does not actually exist in this repo.** The current AI parser is **Gemini 2.5 Flash Lite** (used for vision card identification in `scraper.py` and `lot_analyzer.py`, and in the `analyze-lot` edge function). There is no Anthropic / Claude / Haiku code in the codebase today. The memo's plan to "repurpose the Haiku parser scaffolding for AI restock detection on Community posts" is therefore a **net-new build**, not a salvage. Flag this for Omar's awareness in Section F.

2. **Two distinct restock systems exist; only one of them is what the memo describes.**
   - `restock_checker.py` + `discover_products.py` + `seed_restock_products.py` + `add_restock_tables.sql` + `send-restock-notifications` edge function + the `Check Restocks` / `Discover New TCG Products` GitHub Actions workflows poll **Target Redsky and Walmart product pages** every 15 minutes for inventory and send push notifications to a waitlist. This is automated retail-API polling.
   - The memo's new "AI restock detection on Community posts" is **community-post → Claude parser → auto-pin on map**, a different mechanism.
   - The retail-API polling stack is **NOT named** in the memo's deprecation list. It is also not endorsed. **Flag for explicit decision** (Section C and Section F).

3. **`marketplace`, `source_platform`, and `source_url` distinction.** The memo names `source_platform` and `source_url` explicitly as columns that "may be retained for future eBay-only import." A grep of this repo found that `source_platform` and `source_url` **do not currently exist** in any migration or in `setup_supabase.sql`. What does exist is `listings.marketplace` (TEXT DEFAULT 'facebook') and `listings.scraped_at`. So the column-decision really applies to **`marketplace` and `scraped_at`**. Treating those as the analog of the memo's flagged columns. Flagged in Section D and Section J.

4. **All current rows in the `listings` table are FB-scraped data.** When we keep the table per the memo, we should also plan a one-time wipe of all existing rows so the table only contains native CollectLocal listings going forward. Flag in Section D.

---

## A. Apify scraper code and configs

| Path | What it does | Why deprecated | Replaced by |
|---|---|---|---|
| `scraper.py` | 67 KB orchestrator. Calls the Apify `apify~facebook-marketplace-scraper` actor, fans queries (`SEARCH_QUERIES = ["PSA 10 pokemon", ...]`) across DFW, fetches FB listing detail pages, runs Gemini Flash Lite vision card-ID, enriches with TCGTracking + eBay prices, geocodes seller location, upserts into `listings`. The whole FB pipeline. | Memo §"What gets deprecated" item 1. Meta TOS + CFAA exposure. | Nothing. Native user-posted listings replace it. |
| `lot_analyzer.py` | Lot-listing decomposition module called from `scraper.py`. Uses Gemini vision to break a multi-card lot photo into individual cards, prices each, writes to `lot_analysis` / `lot_cards`. | Coupled to scraper input (FB listing photos + descriptions). Lot-from-photo is still valuable for the **Scan** feature, but **this module ingests scraped FB lots**, not user uploads. | The Scan feature uses the `analyze-lot` edge function (Section E) on user-uploaded photos. This module is dead. |
| `push_files.py` | One-shot deployer that pushes backend files into the GitHub repo via the Contents API. | Used only during initial scraper rollout. Dev-tool, not infra. | Nothing. Repo is bootstrapped. |
| `push_to_github.sh` | Initial `git init` + `git add scraper.py …` + push. Hard-codes the scraper as the primary artifact. | Same as above. Bootstrap script for the FB pipeline. | Nothing. |
| (no Apify actor JSON in repo) | The `APIFY_ACTOR_ID = "apify~facebook-marketplace-scraper"` is referenced in code but no separate actor definition file lives in this repo. The actor lives on Apify. | — | Delete the Apify actor on Apify side after merge. |

---

## B. GitHub Actions workflows

| Path | Schedule | What it does | Why deprecated | Replaced by |
|---|---|---|---|---|
| `.github/workflows/scrape.yml` | `0 */6 * * *` (4x/day) | Runs `scraper.py`. | Memo §"What gets deprecated" item 4. | Nothing. |
| `.github/workflows/restock_check.yml` | `*/15 * * * *` | Runs `restock_checker.py` against Target Redsky + Walmart. | **Not named by the memo.** Not a scraper of consumer marketplaces — polls retailer APIs/PDPs. **Flagged for decision** — keep, retire, or repurpose as the schedule backbone for AI restock-detection batch jobs. | TBD pending decision. |
| `.github/workflows/discover_products.yml` | `0 6 * * 1` (Mondays) | Runs `discover_products.py` to discover new TCG SKUs at Target + Walmart. | **Not named by the memo.** Same flag as above. | TBD. |

---

## C. Cron / scheduled jobs (outside GitHub Actions)

No external cron infrastructure is present in this repo. All scheduling is via the three GitHub Actions workflows above, plus Supabase Edge Function on-demand invocations.

**Flagged for decision (per the brief):** Do we want cron infrastructure gone entirely once `scrape.yml` is removed, or kept (via `restock_check.yml` / `discover_products.yml`) and repurposed as the schedule backbone for new AI restock-detection batch jobs that run against Community posts? Recommendation pending Omar.

---

## D. SQL migrations and column changes

### D.1 Definitely deprecated

| Path | What it does | Why deprecated | Replaced by |
|---|---|---|---|
| `setup_supabase.sql` (file as a whole) | Original DB bootstrap. Creates `listings` with FB-scraping semantics, and the `scrape_runs` health-tracking table. | The schema baseline needs to be reauthored for native listings. | A new bootstrap migration that creates `listings` without scraping columns and drops `scrape_runs`. |
| `migrations/add_listing_photos.sql` | Adds `listing_photos JSONB` to hold all FB photos, backfills from `image_url`. | The column is still useful (native listings will have multiple photos), but the **comment and backfill logic explicitly reference FB Marketplace**. The column survives; the migration file as a historical artifact does not need re-running. | Column retained; file is historical-only. **No action needed on the file.** |
| `lot_tables.sql` | Creates `lot_analysis` + `lot_cards` + `listings_with_lot_cards` view. Used by `scraper.py` lot pipeline. | The Scan feature uses lot decomposition on user-uploaded photos, not scraped lots. The tables themselves may be reused by Scan, but the **`listings_with_lot_cards` view joins on `listings.id`** for scraped rows — those rows are going away. | View should be recreated to support Scan results (decoupled from `listings`). Tables can survive with a schema audit. **Flagged for decision: keep tables, drop view, recreate view scoped to Scan only.** |

### D.2 Columns on `listings` table

Reference: the current `listings` schema is in `setup_supabase.sql` lines 7–34 plus the additive migrations.

| Column | Today's purpose | Memo guidance | Recommendation |
|---|---|---|---|
| `external_id` | FB Marketplace listing ID dedup key | Implicitly tied to scraping | **Drop.** Native listings get their own UUID. |
| `marketplace TEXT DEFAULT 'facebook'` | Source marketplace tag | Memo flags `source_platform` (analog) as "may retain for future eBay-only import" | **Flagged for decision.** Drop / rename to `source` / keep-reserved. |
| `scraped_at TIMESTAMPTZ` | Used by iOS as the primary `ORDER BY` for the feed | Memo flags as "may retain" | **Flagged for decision.** Independent of decision, iOS needs `created_at` populated and used instead (see Section J). |
| `listing_url` | Direct link to the FB listing | Memo doesn't address directly, but it's the same family as `source_url` | **Flagged for decision** alongside `marketplace`. |
| `seller` | Seller name/username (FB display name) | — | Drop. Native listings have an authenticated `user_id` (in `profiles`). |
| `seller_rating` | Float stored on the listing row | — | Drop. Trust is computed off `profiles` (see `add_trust_verification.sql`). |
| `posted` | Human-readable "2 hrs ago" string set by scraper | — | Drop. Compute client-side from `created_at`. |
| `distance` | Pre-computed miles from configured `HOME_LAT/LNG` | The scraper baked a single global "home" into the schema | Drop. Per-metro feed computes distance via PostGIS at query time. |
| `is_active` | False when listing sold/removed (set by scraper) | — | Keep semantics but redefine — toggled by seller, not scraper. |
| `created_at`, `name`, `card_set`, `card_number`, `grade`, `price`, `market_price`, `market_source`, `image_url`, `listing_photos`, `location`, `lat`, `lng`, `description`, `ebay_price`, `ebay_url`, `ebay_listing_url` (from `add_ebay_listing_url.sql`), `ebay_listing_title`, `card_type`, `is_lot`, `lot_card_count`, `lot_estimated_value` | Core listing data | Carry over | **Keep.** |

### D.3 Other migrations — surveyed, all retained

| Path | Survives? | Notes |
|---|---|---|
| `migrations/add_ebay_listing_url.sql` | Yes | Direct BIN URL columns; orthogonal to scraping. |
| `migrations/add_pokemon_card_catalog.sql` | Yes | Canonical card data, source-of-truth for catalog. |
| `migrations/generalize_card_catalog.sql` | Yes | Rename to `card_sets` / `card_catalog`; multi-TCG. |
| `migrations/add_trust_verification.sql` | Yes | Identity/trust tier infra — explicitly retained per brief. |
| `migrations/add_portfolio_tables.sql` | Yes | Portfolio/Collection feature. |
| `migrations/add_location_sharing.sql` | Yes | Per-user location sharing toggle on `profiles`. |
| `migrations/add_referral_tables.sql` | Yes | Refer & Earn — unrelated to pivot. |
| `migrations/add_restock_tables.sql` | **Flagged** | Creates `restock_products`, retail stores, inventory, waitlist, scout rewards, device tokens, restock events. This is the **retail-API polling** stack (see Section A finding 2). Survives or dies with that decision. |

### D.4 Existing data wipe

All current `listings` rows are FB-scraped. Recommend a `DELETE FROM listings;` in the same deletion commit (or a separate cleanup migration) so the surviving table starts empty. **Flagged for explicit confirmation** — Omar may want to keep them as test data in a dev env.

---

## E. Supabase Edge Functions

| Path | What it does | Verdict | Replaced by |
|---|---|---|---|
| `supabase/functions/analyze-lot/index.ts` | Gemini Flash Lite multi-card vision identification + TCGTracking pricing. Called by iOS Scan feature. | **Keep.** Memo §"What carries over" retains the Scan feature. | — |
| `supabase/functions/tcgplayer-price/index.ts` | TCGTracking on-demand market-price lookup. | **Keep.** TCGTracking is retained per memo. | — |
| `supabase/functions/send-restock-notifications/index.ts` | Receives a restock event ID from `restock_checker.py`, looks up waitlist users within radius, sends FCM. | **Flagged for decision.** Lives or dies with `restock_checker.py` (Section B). If we keep retail polling, this stays. If we kill it and rebuild around community-post AI parser, the new fan-out function will look very similar — could be refactored rather than rewritten. | TBD |
| `supabase/functions/process-referral/index.ts` | Refer & Earn back-end. | **Keep.** Unrelated to pivot. | — |

---

## F. AI parser prompts and orchestration

Split per the brief:

### F.1 Definitely-dead

- **Gemini vision card-ID prompt in `scraper.py`** (the FB listing → identify card → write to `listings` flow). Lives inside `scraper.py` itself; goes with the file.
- **Gemini lot decomposition prompt in `lot_analyzer.py`** — the **scraper-facing version** is dead. The same prompt is duplicated in the `analyze-lot` edge function for Scan, which lives on.

### F.2 Salvageable

- **`analyze-lot` edge function's Gemini prompt** — survives unchanged for Scan.
- **TCGTracking price-cascade logic in `scraper.py`** — the algorithm itself (TCGTracking → Pokémon TCG API → eBay Browse API) is referenced by the memo as carrying over. The implementation in `scraper.py` is dead, but `supabase/functions/tcgplayer-price/index.ts` and `update_catalog_prices.py` already implement the same cascade and survive. Nothing to extract from `scraper.py`.

### F.3 Net-new (memo says "repurpose Haiku" but there's nothing to repurpose)

- **Claude Haiku does not currently exist in this repo.** The memo's "AI restock detection on Community posts" parser will be a from-scratch build. No Anthropic SDK, no Claude prompt, no Haiku orchestration to salvage. **Flagged for Omar.**

---

## G. Env vars and secrets

### G.1 Drop from `.env.example` and from GitHub Actions secrets

| Name | Used by |
|---|---|
| `APIFY_TOKEN` | Apify FB scraper |
| `GITHUB_TOKEN` (in `.env.example`) | `push_files.py` bootstrap utility |
| `GITHUB_REPO` | `push_files.py` |
| `HOME_LAT` | Single global "home" baked into scraper's distance calc |
| `HOME_LNG` | Same |
| `SEARCH_LOCATION` | Scraper's FB query location filter |
| `SEARCH_RADIUS_MILES` | Same |
| `MAX_LISTING_AGE_HOURS` | Scraper-only filter |
| `GEMINI_API_KEY` | **Kept** — used by `analyze-lot` edge function for Scan. Note: still needs to exist as a Supabase Edge Function secret, just not as a GitHub Actions secret for the scraper. |

### G.2 GitHub Actions secrets to remove

These were set per `SETUP_GUIDE.md` Step 3. Remove from the repo's GitHub Actions secrets after the workflow YAMLs are deleted: `APIFY_TOKEN`, `HOME_LAT`, `HOME_LNG`, `SEARCH_LOCATION`, `SEARCH_RADIUS_MILES`. **Action item for Omar — Claude cannot delete repo secrets.**

### G.3 Apify side

Delete the Apify account (or rotate the token) once the workflow is removed. **Action item for Omar.**

### G.4 Keep

`SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `SUPABASE_PROJECT_REF`, `SUPABASE_ANON_KEY`, `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `FCM_SERVICE_ACCOUNT`, `GEMINI_API_KEY` (Scan).

---

## H. Dependencies (`requirements.txt`)

| Package | Used by | Verdict |
|---|---|---|
| `apify-client>=1.6.0` | `scraper.py` only | **Drop.** |
| `supabase>=2.0.0` | All Python utilities and seeders | Keep. |
| `requests>=2.31.0` | All Python utilities | Keep. |
| `python-dotenv>=1.0.0` | All Python utilities | Keep. |

No `package.json` exists in this repo (the edge functions are Deno + URL imports — no manifest to prune).

---

## I. Tests and fixtures

This repo contains **no test files** (no `tests/`, no `*_test.py`, no `*.test.ts`). No fixtures to remove. Adding a tests/ directory is out of scope for the deprecation sprint.

---

## J. iOS unblocker (cross-repo coordination)

The iOS deprecation sprint on `chore/pivot-deprecation-sprint` is intentionally leaving the `scraped_at` order-by in place until this backend produces replacements. The backend deliverables iOS needs **before they can ship commit 2** are:

### J.1 Migration the iOS team needs (proposed name: `migrations/pivot_native_listings.sql`)

1. Ensure `listings.created_at` exists, is NOT NULL, and is backfilled for every surviving row (it already exists with `DEFAULT NOW()` per `setup_supabase.sql:33`, but iOS needs the guarantee that every row has it so the order-by switch is safe).
2. Add `CREATE INDEX idx_listings_created_at ON listings(created_at DESC);` to replace the existing `idx_listings_scraped`.
3. **Decision-pending columns** (these are the iOS-visible questions):
   - **`marketplace`** — drop, rename to `source`, or keep-reserved-for-eBay-imports?
     - **Recommendation:** Keep but rename to `source` and default to `'native'`. Cheap to keep, gives us the lane for eBay imports later, and the rename forces iOS to do an intentional update of any filter logic.
   - **`scraped_at`** — drop entirely, or keep-reserved-for-future-eBay-imports?
     - **Recommendation:** Drop. iOS switches to `created_at`. If we add eBay import later, the eBay import job populates `created_at` from the eBay listing's `itemCreationDate` and there's no need for a parallel timestamp.
   - **`listing_url`** (analog of memo's `source_url`) — same question.
     - **Recommendation:** Keep as nullable. Cheap and useful for "view on eBay" affiliate links from imports.
   - **`external_id`** — drop.
     - **Recommendation:** Drop. Native rows use the row UUID.
4. **One-time data wipe:** `DELETE FROM listings;` (all current rows are FB-scraped). Flagged for explicit confirmation.

### J.2 What iOS does after merge of J.1

- Switch the feed query's `ORDER BY scraped_at DESC` to `ORDER BY created_at DESC`.
- Remove FB-specific marketplace filter chips.
- Remove `external_id` from the iOS `Listing` model.
- Update field name from `marketplace` to `source` if J.1 recommendation is adopted.

### J.3 Branch strategy proposal (per the brief)

**Recommendation: same branch, two commits.**
- Commit 1: `chore: deprecate FB scraping infrastructure per pivot strategy memo`
- Commit 2: `feat(db): add native-listings migration for iOS feed unblock`

Rationale: the deletion and the unblock-migration are part of the same pivot and want to land together so the iOS branch can rebase on a single backend SHA. A separate branch would force iOS to wait on two PR merges in sequence.

---

## Items explicitly flagged for Omar's decision

Consolidated list — all items requiring an explicit yes/no before deletions execute:

1. **`restock_checker.py` + `discover_products.py` + `seed_restock_products.py` + `restock_check.yml` + `discover_products.yml` + `send-restock-notifications` edge function + `migrations/add_restock_tables.sql`** — the retail-API polling stack. Memo does not name it. Keep, retire, or repurpose?
2. **Cron infrastructure (the GitHub Actions schedule backbone)** — gone entirely, or kept as the runner for new AI restock-detection batch jobs?
3. **`listings.marketplace` column** — drop, rename to `source`, or keep-reserved-for-eBay-imports?
4. **`listings.scraped_at` column** — drop or keep-reserved?
5. **`listings.listing_url` column** — drop or keep-reserved?
6. **One-time `DELETE FROM listings;`** in the same deletion commit — yes or hold?
7. **`lot_tables.sql` view `listings_with_lot_cards`** — recreate scoped to Scan only, or drop entirely (Scan would query `lot_analysis` directly)?
8. **Apify account / token rotation** — Claude cannot do this; flagged as an Omar action item post-merge.
9. **GitHub Actions repo secrets removal** (`APIFY_TOKEN`, `HOME_LAT`, etc.) — Omar action item post-merge.
10. **Claude Haiku scaffolding for restock detection** — the memo assumes scaffolding exists to repurpose; it doesn't. Confirm this is understood (the new feature is a from-scratch build, not a salvage).

---

## Suggested execution order once approved

1. **PR 1 (this branch, `chore/pivot-deprecation-sprint`):**
   - Commit 1: deletions per sections A–H above, modulo the flagged items.
   - Commit 2: `migrations/pivot_native_listings.sql` per section J.1.
2. **Post-merge, Omar's action items:** delete GitHub Actions secrets, delete/rotate Apify token, run the new migration in Supabase, run the `DELETE FROM listings;` if approved.
3. **iOS rebase on the new backend SHA** and ship their commit 2.
