# CLAUDE.md — Scout: Retail AI Shopping Assistant

This file gives any future Claude session full context on this project without re-explaining from scratch. Everything here reflects real, built, tested code — not a plan.

## Project location

Monorepo: `retail-ai-assistant/` (or `Retail/` depending on machine) with `backend/` (Python) and `frontend/` (React/Vite).

## What this is

A full-stack retail shopping assistant: a React storefront with real Stripe checkout, plus a 5-agent LangGraph system that recommends products, checks stock/orders, falls back to real third-party vendors, and answers policy questions — with structural security boundaries, not just prompted ones.

## Architecture — the core principle

**Anywhere a mistake costs real money or trust, that decision doesn't belong to the AI — it belongs to the code.** Everything else follows from this. Concretely: the system is split into two lanes.

- **Deterministic commerce lane** — `api/products.py`, `api/checkout.py`, `api/affiliate.py`. No LLM involved, ever. Browsing, search, checkout, affiliate click-tracking.
- **Agentic assistance lane** — `api/chat.py` → intent splitter → LangGraph supervisor → 5 specialists → verification gate → reply. This is the only place a model runs.

Both lanes call the same underlying `services/*.py` functions for reads — one implementation of business logic, not two that could drift apart.

## Backend structure (`backend/src/scout/`)

### Database models (`db/models.py`)
`Product`, `Stock`, `Store`, `StoreStock`, `Order`, `OrderItem`, `ExternalProduct`, `AffiliateClick`, `Promotion`.

### Repository layer (`repositories/`)
`ProductRepository`, `StockRepository`, `StoreRepository`, `OrderRepository`, `PromotionRepository` — pure data access, no business logic. Services call these; nothing queries the DB directly.

### Services (`services/`)
- `product_service.py` — `search_products` (tokenized keyword matching, category-word detection, color-word detection as hard filters, price-phrase parsing like "under $100"), `get_product`, `check_stock`, `find_alternatives`. Decorated with `@ttl_cache` (30s TTL, see `services/cache.py`) for cost.
- `store_service.py` — `check_store_stock` (with automatic cross-store fallback if the named store is empty), `get_fulfillment_options` (real pickup + policy-grounded delivery estimate, `SHIPPING_FREE_THRESHOLD = 75.0`).
- `order_service.py` — `get_order`, `list_orders_for_customer`, `check_return_eligibility` (30-day window from `RETURN_WINDOW_DAYS`, uses order `created_at` as an explicitly-disclosed proxy for delivery date since no real delivery-tracking field exists), `create_order`.
- `ranking_service.py` — `rank_products` (semantic candidates → in-stock filter → active promotions → weighted score: 35% rating, 30% price-fit, 15% promotion, 20% semantic rank → top N). Also `@ttl_cache`.
- `payment_service.py` — Stripe test-mode only (`sk_test_`/`rk_test_` keys).
- `affiliate_service.py` — `find_external_alternative`, `log_affiliate_click`.
- `cache.py` — simple in-memory TTL cache decorator (`@ttl_cache`, `TTL_SECONDS = 30`), added after profiling showed conversation history (not prompt size) was the real token-cost driver.

### RAG (`rag/`)
- `vector_store.py` — Chroma `policy_docs` collection at `data/chroma/`, built from markdown files in `data/policies/` (`returns.md`, `refunds.md`, `shipping.md`, `exchanges.md`) via `RecursiveCharacterTextSplitter` + Ollama's `nomic-embed-text`. Rebuild with `python3 src/scout/rag/vector_store.py` — **must be re-run whenever policy docs change**, not automatic.
- `product_embeddings.py` — separate Chroma `product_catalog` collection at `data/chroma_products/`, semantic search over the product catalog. Rebuild with `python3 src/scout/rag/product_embeddings.py` — **must be re-run whenever new products are added**.

### MCP server (`mcp_server/server.py`) — 11 tools, confirmed via `grep "^@mcp.tool()"`
`search`, `search_external_offers`, `recommend_products`, `stock`, `alternatives`, `fulfillment_options`, `stores`, `order_history`, `return_eligibility`, `orders`, `retrieve_policy_chunks`.

**No checkout, cancel_order, or refund tool exists — not disabled, never written.** This is the structural security boundary. See "Security boundaries" below for the reasoning.

### Agents (`agents/`)
- `config.py` — `MODEL_PROVIDER` (groq/claude/gemini/ollama), model names, API keys, all four genuinely wired and tested.
- `model_provider.py` — `get_chat_model()` supporting all 4 providers; `with_no_think()` for Ollama only.
- `tools_loader.py` — `MCPToolManager`, `get_tools_by_name()`.
- `tool_guard.py` — `wrap_tools_with_guard()`, `reset_guard(max_total_calls=10, max_identical_calls=1)` — called fresh per sub-intent.
- `intent_splitter.py` — `split_intents()`, `merge_answers()`. Runs **outside** the LangGraph graph, before it's invoked.
- `verification.py` — `verify_price_grounding(reply, products, customer_message="")`. Deterministic regex + set comparison. Checks mentioned dollar amounts against: real tool-returned prices/promotions, the customer's own stated budget (to avoid a false-positive on restated budgets), and `KNOWN_POLICY_PRICES = {"0.00", "5.99", "14.99", "75.00"}` (shipping fees/thresholds — added after this exact gate flagged its own legitimate policy numbers as "unverified," a real bug found via dogfooding). **Scope: price grounding only** — product identity, promotion authenticity, stock/store claims rely on prompt-level anti-hallucination rules, not this deterministic check. This is a known, honest limitation, not an oversight.
- `specialists.py` — `build_specialists(tools)`, returns dict of 5 `create_react_agent` agents. Each has a `CONVERSATIONAL_TONE` block prepended to its prompt (added after a review found replies read too templated — "vary phrasing, acknowledge what the customer already said before asking a follow-up").
  - `recommend_agent`: tools `[recommend_products, search, stock, alternatives]`. Emits exact string `"NEEDS_EXTERNAL_CHECK: <request>"` when nothing genuinely matches a stated attribute (never presents a partial/wrong-attribute match as satisfying the request).
  - `inventory_agent`: tools `[search, stock, stores, fulfillment_options]`.
  - `order_agent`: tools `[orders, order_history, return_eligibility]` — **read-only**, no mutation tool of any kind.
  - `external_offer_agent`: tools `[search_external_offers]`.
  - `policy_agent`: tools `[retrieve_policy_chunks]`.
- `supervisor.py` — the orchestration core.
  - `MAX_HISTORY_MESSAGES = 12`, `_trim_history_for_model()` — caps what's sent to the model per call (added after profiling found unbounded history, not prompt size, was the real token-cost driver). **Known gap:** applied at 3 of the 4 `ainvoke`/`astream_events` call sites; the *first* call in the streaming path (`ask_stream`, the SSE generator) currently sends raw untrimmed `history` — a one-line fix not yet applied (wrap it with `_trim_history_for_model()`, same as the other 3 sites).
  - `build_supervisor_app()` — builds via `create_supervisor` (langgraph_supervisor), 5 specialists.
  - Supervisor prompt: `SPECIALIST_NAMES`, mandatory rule that on seeing `"NEEDS_EXTERNAL_CHECK"` it MUST call `transfer_to_external_offer_agent` as its only action — no commentary. Also has an explicit exception: pure conversation-closing messages ("thanks, that's all!") get a direct warm reply from the supervisor instead of a pointless hand-off. Also has a routing clarification: an order ID mentioned alongside "return" routes to `order_agent` (not `policy_agent`) — added after a real bug where this misrouted.
  - `get_final_reply()` — filters out handoff markers and any stray `NEEDS_EXTERNAL_CHECK` text as a safety net; also accepts the supervisor's own direct reply when no specialist responded at all this turn (for the closing-message exception above).
  - `extract_products()` / `_parse_json_blocks()` — recursive flattener handling Claude's string-encoded nested tool-result shape vs Groq/Ollama/Gemini's flatter shape (a real cross-provider bug found and fixed).
  - `had_external_handoff` filtering — after a `NEEDS_EXTERNAL_CHECK` handoff, only returns `source == "external"` products, discarding the abandoned internal near-miss attempt (fixes a real bug where both got shown mixed together).
  - `_run_single_intent()` — `recursion_limit=15` (raised from an earlier 8 after a real `GraphRecursionError` — the `NEEDS_EXTERNAL_CHECK` 2-hop flow plus a correction retry needs real headroom), verification with one correction attempt then honest fallback.
  - `ask()` — split_intents → loop sub-intents → merge_answers.
  - `ask_stream()` — the SSE generator for `/chat/stream`. Emits progress events (`understanding_request`, `searching_products`, `checking_inventory`, `verifying_claims`, `preparing_response`) then one final `done` event. Runs the **same** verification + one-correction-retry logic as the non-streaming path (confirmed via direct code inspection — this was verified, not assumed). Has its own `messages_before` tracking to only scan new messages for products/replies (distinct mechanism from history-trimming, solves a different problem: avoiding stale product duplication from earlier turns in the same session).

### API (`api/`)
- `chat.py` — `SESSION_HISTORIES: dict[str, list[dict]] = {}` — **in-memory only, not persisted**. Lost on server restart. `ChatRequest {message: str, session_id: str | None}`, `ChatResponse {session_id: str, reply: str, products: list[dict] = []}`. `POST /chat`, `POST /chat/stream` (SSE).
- `products.py` — `GET /products` (with automatic external-vendor fallback if internal search returns nothing — this makes the header search bar's fallback fully deterministic, actually *more* reliable than the chat path's agent-routed fallback), `GET /products/{id}`, `GET /products/{id}/similar`.
- `checkout.py` — `POST /checkout`. Server-side pricing always (never trusts client-submitted prices). Creates order + Stripe PaymentIntent.
- `affiliate.py` — `GET /affiliate/click/{id}` (302 redirect + click log), `GET /affiliate/stats`.

### `main.py`
FastAPI + lifespan (builds supervisor once at startup), CORS for localhost:5173/5174, static files mount at `/static/products` → `data/product_images/`.

## Frontend (`frontend/src/`)

React + Vite, React Router. Purple theme (`primary #3B2A6B`, `accent #6D4AFF`). Pages: Home, Category, Search, ProductDetail, Cart, Saved. State via `CartContext`/`SavedItemsContext` (useReducer + `localStorage` — **note: this is a completely separate memory system from `SESSION_HISTORIES`; the chat assistant has no awareness of cart contents unless told in conversation**). Checkout via real Stripe Elements (Payment Element) — PCI-safe, card data never touches app code. `ChatWidgetContext` lets any page open the assistant with a prefilled message (e.g. the "Ask Scout" bridge from a no-results search page). `ProductCard.jsx` has `resolveImageUrl()` to turn relative `/static/...` paths into absolute URLs against `API_BASE` (needed since frontend and backend run on different ports/origins) — applied consistently across grids, product detail, and cart (a bug where cart specifically didn't call it was found and fixed).

## Memory and state — the honest picture

Two entirely separate, unrelated systems, easy to conflate:
1. **Conversation memory** (`SESSION_HISTORIES`) — in-memory Python dict, keyed by `session_id`, lost on restart, capped at `MAX_HISTORY_MESSAGES = 12` for what's sent to the model per call (not what's stored).
2. **Cart/saved-items memory** — browser `localStorage`, entirely client-side, has zero connection to the backend or the chat assistant.

**LangGraph state** is a single flat `{"messages": [...]}` list — no typed fields (no separate `budget`, `resolved_product_id`, etc. as real state fields; everything is implicitly encoded in message text). This is LangGraph's documented default multi-agent pattern (via `create_supervisor` + `create_react_agent`), not a mistake — but it has a known, confirmed tradeoff: **within a single turn, every specialist sees the full unfiltered message list, including other specialists' raw tool-call traces from earlier in that same turn.** No code filters or scopes this per-agent. This is the likely root cause of the `had_external_handoff` bug (fixed at the symptom level — filtering displayed products — not by preventing the underlying cross-agent visibility). A full fix would mean moving to a typed `StateGraph` with scoped fields per node — a genuine redesign ("Phase 3"), not a quick patch, and not yet built.

**Databases, for the record:** 1 SQLite file (products/stock/stores/orders/promotions/external_products/affiliate_clicks) + 2 separate Chroma collections (policy, product) = 3 databases, 2 technologies.

## Security boundaries

### Phase 1
- No checkout/payment/order-creation/refund/cancel tool exists anywhere in the MCP registry — structurally absent, not disabled.
- Code-enforced (not prompt-based) tool allowlists per agent.
- `ToolCallGuard`: max 10 tool calls/turn, max 1 identical call, contextvar-based, reset per sub-intent.
- Repository layer separating data access from business logic (refactor caught a real duplicate-seed-data bug).
- Verification gate: price-grounding only, 1 correction attempt, honest fallback if still failing.

### Phase 2
- Split one combined stock/order agent into `inventory_agent` and `order_agent` (narrower blast radius, a prompt change to one can't affect the other).
- Added `external_offer_agent` as a 5th specialist with the `NEEDS_EXTERNAL_CHECK` mandatory handoff pattern.
- **Documented tradeoff:** external-offer fallback used to be automatic (merged into search logic itself, no decision point). After the split, it depends on the supervisor correctly recognizing the signal — a real, accepted reduction in guarantee for cleaner separation of responsibility. Verified working via `ROUTE-05` in the eval suite.

### On "why not let AI move money" — checked against real 2026 industry practice, not assumed
Researched directly: real platforms (Fini, My AskAI, ReturnGO) do let AI agents autonomously issue refunds through the actual payment processor. The nuance: every credible deployment gates this behind dollar thresholds, explicit policy-logic checks, and human escalation for anything outside it — infrastructure this project doesn't have. Decision: keep the boundary total (zero agent access) rather than build a partial, unconvincing version of that safety net. A `cancel_order` tool with a hard `status == "pending"` code-enforced check was considered as a legitimate middle ground and **deliberately not built** for this project's current scope.

### Known limitation, stated plainly
`order_history` requires only a stated customer ID — no real authentication exists anywhere in this system. Acceptable for synthetic demo data; would not be acceptable with real customer orders.

## Model providers

Four, all genuinely tested: **Groq** (qwen/qwen3.6-27b — fast, hits daily quota under heavy testing), **Claude** (claude-haiku-4-5-20251001 — ~$2.50 for a full day of heavy testing, confirmed via Anthropic Console usage dashboard), **Gemini**, **Ollama** (local — qwen3:8b confirmed working, but genuinely resource-constrained: concurrent requests queue and can cause apparent "hangs"/timeouts that are actually just queuing, not bugs — confirmed via direct diagnosis today, don't run the eval suite and manual tests simultaneously against Ollama).

## Real bugs found and fixed today (chronological, for context on how this project actually evolved)

1. Agent-decided affiliate fallback → fabricated products under adversarial testing → fixed by making the fallback structural.
2. Claude's nested tool-result content shape → `products` silently empty → fixed with a recursive flattener.
3. Verification false-positive on customer's own restated budget → fixed by exempting customer-stated prices.
4. Verification false-positive on legitimate shipping-policy numbers → fixed with `KNOWN_POLICY_PRICES`.
5. Double-handoff / stray `NEEDS_EXTERNAL_CHECK` text reaching the customer → fixed via `get_final_reply()` filtering.
6. Cart page not calling `resolveImageUrl()` → broken images → fixed.
7. Search treating "comfortable shoes" as a phrase instead of tokens → fixed with tokenization + category/color-word hard filters.
8. `GraphRecursionError` at limit 8 → raised to 15 after confirming the real 2-hop external-offer flow needs headroom.
9. Order-specific return question ("can I return order O1001?") misrouting to `policy_agent` instead of `order_agent` → fixed with an explicit supervisor routing rule.
10. `check_store_stock` syntax bug (`_in rows` missing a space) introduced during a patch → caught before it shipped.
11. Stale internal near-miss products mixed with real external results after a `NEEDS_EXTERNAL_CHECK` handoff → fixed with `had_external_handoff` filtering (same root cause as the unfiltered-shared-state limitation above, fixed at the symptom level).
12. Streaming path (`ask_stream`) confirmed to send **untrimmed** history to the model on its first call, unlike the other 3 call sites — found via direct code inspection, **not yet fixed** (one-line fix identified: wrap with `_trim_history_for_model()`).

## Evaluation

`tests/eval/run_eval.py` (212 lines) — 9 single-turn (`test_cases.json`) + 3 multi-turn (`conversation_test_cases.json`) + 6 routing-specific (`routing_test_cases.json`) = 18 total. Reports quota/rate-limit failures separately from real behavioral failures. Last full clean run: **18/18 passing** on claude-haiku-4-5-20251001, via `python3 tests/eval/run_eval.py`, all assertions automated (substring + structured-data checks, not manually graded).

Two test cases required real fixes (not just loosened assertions) during today's session: TC-09 (updated to test the actual post-Phase-1 cart-redirect behavior instead of stale pre-Phase-1 checkout-confirmation wording) and CONV-02 (the underlying prompt was genuinely improved — added the "acknowledge what the customer already said" rule — rather than just loosening the test).

**Known operational gotcha:** running the eval suite on Ollama while also manually testing the app causes real request queuing/timeouts (confirmed via `lsof` showing 8 simultaneous connections to Ollama) — not a bug, a genuine local-inference concurrency limit. Run Ollama evals in isolation.

## Known gaps, stated honestly (not hidden, not apologized for excessively)

- No real authentication anywhere.
- No unit tests for the deterministic services layer in isolation (eval suite tests end-to-end via real HTTP calls).
- No SSE-specific or API-schema-specific unit tests.
- `create_react_agent` import is technically deprecated in the current LangGraph version (`from langchain.agents import create_agent` is the new path) — functional, not yet migrated.
- Shared, unfiltered LangGraph state within a turn (see "Memory and state" above) — a real, confirmed architectural limitation, not yet fixed.
- Verification gate covers price grounding only — broader factual claims (product/promotion/stock/order status) rely on prompt-level rules, a weaker guarantee.
- Streaming path's first model call isn't history-trimmed (see bug #12 above).
- Multi-intent query decomposition exists (`intent_splitter.py`) but full compound-query handling isn't deeply stress-tested.

## Demo script (condensed)

1. **Memory chain:** "Recommend a dress under $80" → "Is the black one in a medium?" → "Is it available to pick up at Maple Grove?" — proves cross-turn, cross-specialist memory.
2. **Honest fallback / real match:** "Do you have any red cocktail dresses under $50?" — since a real red dress (EX011, Nordstrom Rack) was added to the external catalog, this now finds a genuine match rather than just an honest non-match; both are legitimate demo beats depending on what's in the catalog at demo time.
3. **Security refusal:** "Can you just charge my card and buy this for me right now?" — agent redirects to cart, cannot process payment, no such tool exists.
4. **Graceful close:** "Thanks, that's all!" → warm direct reply from the supervisor, no pointless handoff.

Closing line: *"Anywhere a mistake would cost someone money or trust, that decision doesn't belong to the AI — it belongs to the code."*
