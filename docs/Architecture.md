# Architecture — Scout: Retail AI Shopping Assistant

## Overview
Scout is a full-stack shopping assistant. A React e-commerce frontend
handles browsing, cart, and checkout through plain deterministic REST
routes. A floating AI assistant, backed by a LangGraph multi-agent system,
handles conversational tasks — recommendations, stock/store/order lookups,
policy Q&A, checkout — rendering real product data as UI cards inline in
the chat.

The recommendation path specifically combines an LLM (intent, explanation)
with two deterministic subsystems (semantic retrieval, ranking) — a hybrid
design chosen deliberately after testing showed that leaving business-
critical decisions to model judgment alone was unreliable (see Architecture
Decisions below).

## Components

### 1. Frontend — React + Vite
(Unchanged from prior build — see README for the page/component table.)
Single-page app, purple visual identity, `ProductCard` as the shared
rendering unit across grids, search, detail page, and the chat widget.
Checkout via Stripe's Payment Element — cardholder data never touches this
application's code, qualifying for PCI compliance tier SAQ A.

### 2. Backend — FastAPI
Two categories of routes, split by whether a task needs a model's
judgment: deterministic REST (`/products`, `/checkout`, `/affiliate/*`) and
agentic (`/chat`, `/chat/stream`). Both call the same service-layer
functions, so there's one implementation of each business rule regardless
of entry point.

### 3. Agents — LangGraph supervisor + specialists

**`recommend_agent`** now owns two distinct capabilities:
- `recommend_products` — the hybrid pipeline (below), for open-ended
  requests ("something for a night out", "recommend a dress under $80")
- `search` — narrow literal lookups (specific brand, specific keyword)
  where the heavier ranking pipeline isn't the better fit
- `stock`, `alternatives` — unchanged from earlier build

**`stock_order_agent`** and **`policy_agent`** are unchanged in design
from the initial backend build.

### 4. The Recommendation Pipeline (`recommend_products`)

This is the newest and most architecturally interesting piece of the
system — a single MCP tool wrapping a multi-stage, mostly-deterministic
pipeline:

```
1. Natural language query (e.g. "something for a fancy night out")
        |
2. semantic_search_products()  -- rag/product_embeddings.py
   Embeds the query with Ollama (nomic-embed-text), retrieves nearest
   neighbors from a dedicated Chroma collection built from each
   product's name + description + tags. This is what lets the system
   match "night out" to items tagged evening/party without any literal
   keyword overlap -- something the existing `search` tool's ILIKE
   matching structurally cannot do.
        |
3. rank_products()  -- services/ranking_service.py
   For each semantic candidate, in order:
     a. In-stock filter (reuses check_stock -- zero-stock items excluded)
     b. Active promotion lookup (product-specific promotions take
        priority over category-wide ones)
     c. Weighted score: 35% rating, 30% price-fit-to-budget, 15% has an
        active promotion, 20% semantic search rank position
   Fully deterministic, fully unit-testable, no LLM involved in this step.
        |
4. Top 3 results (with score + promotion/discounted-price data) returned
   to recommend_agent, which generates the natural-language explanation --
   grounded in the actual returned data, same anti-hallucination
   discipline as the rest of the system.
```

**Why a hybrid design, not "let the LLM do all of it":** an LLM could,
in principle, look at all 30 products and pick 3 by itself — but that
repeats exactly the failure mode already discovered and fixed once today
in the affiliate-fallback feature: asking a model to reliably execute a
multi-step judgment call (retrieve → filter → apply a business rule →
rank) is measurably less reliable than deterministic code doing those
steps, with the model reserved for the parts that are genuinely
language-shaped — understanding the query, and writing a natural
explanation of results it's handed.

**Promotions (`Promotion` model):** intentionally minimal — a discount
percentage tied to either a specific `product_id` or a whole `category`,
with an `active` flag. Not a cart-rules engine (no stacking logic, no
date ranges, no minimum-purchase conditions) — just enough real data for
the ranking pipeline to demonstrate promotion-aware scoring and for the
agent to report genuine, non-hallucinated discount pricing.

**Two separate Chroma collections exist deliberately:** one for policy
document retrieval (`policy_docs`, used by `policy_agent`) and one for
product catalog semantic search (`product_catalog`, used by
`recommend_products`). Different content, different embedding targets,
different consumers — kept as separate collections rather than one shared
index, even though both use the same underlying Ollama embedding model.

## Data Flow

**Recommendation (hybrid):**
```
ChatWidget -> POST /chat -> LangGraph Supervisor -> recommend_agent
   -> recommend_products tool
        -> Chroma (product_catalog collection) -> candidate product_ids
        -> SQLite (stock, promotions) -> filtered, scored, ranked top 3
   -> recommend_agent generates grounded explanation
   -> {reply, products} -> ChatWidget renders real ProductCards
```

**Browsing / Checkout / Affiliate** — unchanged from the prior build (see
earlier sections of this document or the README's architecture diagram).

## Architecture Decisions & Lessons Learned

**(Carried forward from earlier in the build — still the operating
principle:)** Checkout, browsing, and affiliate click-tracking are all
deterministic REST routes with no LLM involved, following the same
reasoning that shaped the recommendation pipeline: anything with a real
cost of failure shouldn't depend on a model reliably following
instructions.

**Recommendation ranking: hybrid, not agent-decided.** Considered letting
`recommend_agent` retrieve candidates and rank them itself via reasoning
over the full candidate list in-context. Rejected for the same reason the
external-vendor fallback was made structural earlier: ranking by
rating/price-fit/promotion is a scoring problem with a correct,
reproducible answer — not a judgment call that benefits from an LLM's
flexibility. Keeping it deterministic also makes the pipeline genuinely
unit-testable (a real gap noted in Known Gaps — this is exactly the kind
of function a `test_ranking_service.py` could exercise without touching
any model at all).

**Semantic search's actual value, honestly assessed.** With only ~30
products, semantic search's *correctness* improvement over keyword search
is real but modest most of the time — a 30-item catalog rarely has enough
ambiguity for embeddings to matter much. Its clearest, most demonstrable
value is on genuinely non-literal queries ("something for a fancy night
out" — zero keyword overlap with any product's actual tags) where keyword
`ILIKE` matching returns nothing. Worth being honest that this feature's
portfolio/architecture value (demonstrating hybrid retrieval, a real
production pattern) is at least as significant as its practical lift on
a catalog this size.

**(Also carried forward: the "Find similar" placement lesson, the
double-handoff fix, the JSON block-parsing bug — see prior sections /
git history of this document for full detail.)**

## Model Provider Findings
(Unchanged — see README summary table. Qwen 3.6 27B via Groq adopted for
development after testing Ollama, Gemini, and Llama 3.3 70B.)

## Known Gaps (by design, not oversight)
- No auth/RBAC/rate limiting at the API boundary
- No live carrier tracking, warehouse system, or human escalation
- **Multi-intent queries not decomposed** — the supervisor's "route to
  exactly one specialist per message" rule (a deliberate fix for a
  double-handoff bug) means a compound request like "recommend a dress
  and check my order" will likely only get one half addressed. True fix
  would require an intent-splitting step before routing — not yet built.
- Ranking is a hand-weighted formula, not a trained model — reasonable
  and explainable at this scale, would need real rework to become a
  "learns from user behavior" system
- No unit tests yet for the deterministic services (ranking, product,
  order) — the eval harness tests the agentic layer end-to-end, but the
  pure-function layer underneath isn't separately covered
- No LLM-as-judge grading — deliberate: current test cases check
  factual/grounding correctness, which code-based assertions verify more
  reliably and without doubling per-test API cost

## Evaluation Plan
`tests/eval/test_cases.json` (9 single-turn) and
`tests/eval/conversation_test_cases.json` (3 multi-turn, testing
cross-agent memory) run against the live `/chat` endpoint via
`run_eval.py`, which distinguishes real behavioral failures from
transient model-API quota errors. Not yet covered: automated tests for
`recommend_products` specifically (e.g., verifying a known promotion
appears correctly, or that an out-of-budget item scores lower) — a
natural next addition given the pipeline is now deterministic enough to
test precisely.
