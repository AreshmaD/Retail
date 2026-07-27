# Scout — Retail AI Shopping Assistant

A full-stack shopping assistant: a React e-commerce site with product
browsing, cart, and real (test-mode) Stripe checkout — plus a floating AI
assistant powered by a LangGraph multi-agent backend that can recommend
products, check stock/store availability, track orders, answer policy
questions, and complete checkout, all rendering real product cards inline
in the conversation.

## Architecture

```
Customer / Browser
   → React + Vite Frontend (routing, cart, search, chat widget)
   → FastAPI (Pydantic validation)
        ├─ /products, /products/{id}, /products/{id}/similar   (deterministic, no LLM)
        ├─ /checkout                                            (deterministic, no LLM)
        ├─ /affiliate/click/{id}, /affiliate/stats              (deterministic, no LLM)
        └─ /chat, /chat/stream                                  (LangGraph Supervisor)
                → Specialist Agent (recommend / stock & orders / policy)
                → MCP Tools (recommend_products, search, stock, stores,
                             orders, checkout, retrieve_policy_chunks)
                → SQLite / 2x Chroma vector stores / Stripe (test mode) / mock affiliate catalog
```

**Key design principle carried through the whole build:** anything that
doesn't need a model's judgment (browsing, checkout, click tracking,
product ranking) is deterministic code — fast, free, and testable. Only
genuinely conversational/ambiguous tasks (routing, explaining results)
go through the LLM.

## Frontend

React + Vite, single-page, purple visual identity. The chat widget is the
"conversational commerce" layer — browsing works normally via routes/grids,
and the AI assistant is a floating overlay that renders real product cards
inline, not just text.

**Pages:**
| Route | Page | Purpose |
|---|---|---|
| `/` | HomePage | Hero banner + featured products grid |
| `/category/:category` | CategoryPage | Filtered product grid |
| `/search?q=...` | SearchResultsPage | Keyword search results |
| `/product/:productId` | ProductDetailPage | Full detail, tags, "You might also like" |
| `/cart` | CartPage | Line items, quantities, real Stripe Elements checkout |
| `/saved` | SavedItemsPage | Wishlist |

**State:** `CartContext` and `SavedItemsContext` — reducer + `localStorage`
persistence, survives page refresh without a backend session.

**Shared components:** `ProductCard` (used identically in grids, search,
detail page, and the chat widget), `ProductGrid`, `ChatWidget` (floating
bubble → chat panel, renders real `products` from `/chat` as cards).

**Checkout:** Stripe's **Payment Element** (`@stripe/react-stripe-js`).
Card details are entered into Stripe's own embedded iframe and never touch
this app's code — qualifies for PCI compliance tier SAQ A, the simplest
tier, since the application never handles cardholder data at all.

## Backend

**Agents:**
| Agent | Tools | Responsibility |
|---|---|---|
| `recommend_agent` | `recommend_products`, `search`, `stock`, `alternatives` | Hybrid semantic recommendations, plus narrow literal lookups |
| `stock_order_agent` | `search`, `stock`, `stores`, `orders`, `checkout` | Stock/store lookup, order status, checkout |
| `policy_agent` | `retrieve_policy_chunks` | RAG-grounded returns/refunds/shipping Q&A |

### The recommendation pipeline

`recommend_products` is the flagship tool — a single, deterministic
pipeline the agent calls once per open-ended recommendation request:

```
Natural language query
   → semantic_search_products()      Chroma embeddings (nomic-embed-text),
                                       understands "something for a night
                                       out" — no literal keyword needed
   → rank_products()                 in-stock filter, active-promotion
                                       lookup, weighted scoring (rating,
                                       price-fit, promotion, semantic rank)
   → top 3 results returned to the agent, which explains them in
     natural language — grounded in real scores/prices/promotions,
     never invented
```

Two Chroma collections exist for two different jobs: one for policy
document retrieval (RAG Q&A), one for product catalog semantic search
(recommendations) — separate concerns, separate embeddings, same
underlying pattern.

**Promotions:** a lightweight `Promotion` table (product-specific or
category-wide discounts) — not a full promo-code/cart-rules engine, just
enough to demonstrate promotion-aware ranking and honest discount
reporting in the agent's replies.

**Deterministic REST routes** (`api/products.py`, `api/checkout.py`,
`api/affiliate.py`) power browsing, search, checkout, and affiliate click
tracking — reusing the exact same service-layer functions the MCP tools
call, so there's one source of truth for business logic regardless of
whether a request comes from the UI directly or through the AI agent.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | React, Vite, React Router |
| Payments (client) | `@stripe/stripe-js`, `@stripe/react-stripe-js` (Payment Element) |
| Backend | FastAPI, Pydantic, SSE (`sse-starlette`) |
| Orchestration | LangGraph, `langgraph-supervisor`, LangChain |
| Model | Swappable — Ollama (local), Google Gemini, or Groq |
| Tool boundary | MCP (`mcp` Python SDK, `langchain-mcp-adapters`) |
| Data | SQLite (SQLAlchemy) |
| RAG / Semantic search | Chroma (2 collections) + Ollama embeddings (`nomic-embed-text`) |
| Ranking | Deterministic weighted scoring (`ranking_service.py`) — no ML model |
| Payments (server) | Stripe (test mode, direct SDK) |
| Monetization | Mock affiliate vendor catalog + real click-tracking |

## Project Structure

```
retail-ai-assistant/
├── docs/
│   └── ARCHITECTURE.md
├── backend/
│   ├── src/scout/
│   │   ├── main.py
│   │   ├── config.py
│   │   ├── api/
│   │   │   ├── chat.py
│   │   │   ├── products.py
│   │   │   ├── checkout.py
│   │   │   └── affiliate.py
│   │   ├── agents/
│   │   ├── mcp_server/
│   │   │   └── server.py            # 9 tools, including recommend_products
│   │   ├── services/
│   │   │   ├── product_service.py
│   │   │   ├── store_service.py
│   │   │   ├── order_service.py
│   │   │   ├── payment_service.py
│   │   │   ├── affiliate_service.py
│   │   │   └── ranking_service.py   # semantic + inventory + promotion scoring
│   │   ├── rag/
│   │   │   ├── vector_store.py      # policy docs collection
│   │   │   └── product_embeddings.py # product catalog collection
│   │   └── db/
│   │       └── models.py            # includes Promotion
│   ├── tests/eval/
│   │   ├── test_cases.json
│   │   ├── conversation_test_cases.json
│   │   └── run_eval.py
│   ├── data/
│   │   ├── product_images/
│   │   ├── chroma/                  # policy embeddings
│   │   └── chroma_products/         # product embeddings
│   └── requirements.txt
└── frontend/
    └── src/
        ├── context/
        ├── components/
        ├── pages/
        └── api/
```

## Setup

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -e .
pip install -r requirements.txt

ollama pull nomic-embed-text
```

```bash
python3 src/scout/db/session.py
python3 src/scout/db/seed.py
python3 src/scout/rag/vector_store.py           # policy embeddings
python3 src/scout/rag/product_embeddings.py     # product embeddings — rebuild any time products change

uvicorn scout.main:app --reload --app-dir src
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

`.env` files: see `backend/.env` and `frontend/.env` for required keys
(model provider, Stripe test keys — publishable client-side, restricted
secret server-side).

### Eval harness

```bash
cd backend
python3 tests/eval/run_eval.py
```

Runs 9 single-turn test cases + 3 multi-turn conversation tests against the
live `/chat` endpoint. Reports transient model-API quota errors separately
from real behavioral failures.

## Model Provider Notes

Multi-agent tool-calling reliability proved genuinely model-dependent
during development. Tested: Qwen3 (Ollama, correct but 60-90s/turn on
consumer hardware), Gemini 3.5 Flash (fast/accurate, but a ~20 requests/day
free tier made iteration impractical), Llama 3.3 70B (Groq, deprecated
mid-project). Settled on **Qwen 3.6 27B via Groq** — fast, reliable
tool-call formatting, generous free tier (though even this was exhausted
more than once during heavy same-day development and testing).

## Known Gaps (by design, not oversight)

- No auth/RBAC/rate limiting at the API boundary
- No live carrier tracking or warehouse system
- No human escalation workflow
- **Multi-intent queries are not decomposed** — e.g. "recommend a dress
  and check my order" will likely only address one of the two asks, since
  the supervisor routes to exactly one specialist per message (a
  deliberate fix for a different, earlier bug — see Architecture
  Decisions). True multi-intent handling would need an intent-splitting
  preprocessing step, not yet built.
- Session/cart state: backend chat history is in-memory; frontend
  cart/saved-items use `localStorage` (client-only)
- Ranking uses a hand-weighted scoring formula, not a trained ML ranking
  model — a reasonable, explainable choice at this catalog size, but
  wouldn't scale to a "learn from click/purchase data" system without
  real rework
- No LLM-as-judge grading in the eval harness — deliberately: the current
  test cases check factual/grounding correctness (stock status, policy
  figures, absence of hallucination), which a code grader checks more
  reliably and without doubling API quota cost per test

## Roadmap

- [ ] Multi-intent query decomposition
- [ ] Unit tests for the services layer (ranking, product, order services)
- [ ] REST route integration tests (no LLM involved, cheap to run exhaustively)
- [ ] Deployment (currently local-only)
