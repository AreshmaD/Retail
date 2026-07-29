from typing import Union

from mcp.server.fastmcp import FastMCP

from scout.db.session import SessionLocal
from scout.db.models import Product
from scout.services.product_service import search_products, check_stock, find_alternatives
from scout.services.store_service import check_store_stock, get_fulfillment_options
from scout.services.order_service import get_order, list_orders_for_customer, check_return_eligibility
from scout.services.ranking_service import rank_products
from scout.services.affiliate_service import find_external_alternative
from scout.rag.vector_store import retrieve_policy_chunks as _retrieve_policy_chunks
from scout.rag.product_embeddings import semantic_search_products

mcp = FastMCP("scout-tools")


def _to_float(value, default: float = 0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@mcp.tool()
def search(
    query: str = "",
    category: str = "",
    department: str = "",
    brand: str = "",
    max_price: Union[str, float] = 0,
    min_rating: Union[str, float] = 0,
) -> list[dict]:
    """Search the product catalog by keyword, category, department, brand,
    max price, and/or minimum rating. Only returns in-stock, INTERNAL
    products. Phase 2: this tool no longer automatically falls back to
    external vendors — if it returns an empty list, use
    external_offer_agent (a separate specialist) to check third-party
    alternatives."""
    session = SessionLocal()
    try:
        results = search_products(
            session,
            query=query or None,
            category=category or None,
            department=department or None,
            brand=brand or None,
            max_price=_to_float(max_price) or None,
            min_rating=_to_float(min_rating) or None,
            in_stock_only=True,
        )
        for r in results:
            r["source"] = "internal"
        return results
    finally:
        session.close()


@mcp.tool()
def search_external_offers(category: str = "", max_price: Union[str, float] = 0) -> list[dict]:
    """MOCK DATA: searches a third-party vendor catalog for products in a
    category, optionally under a max price. Use this when the internal
    `search` tool returns no in-stock results. Each result includes a
    click_url — present this to the customer as the link to click, never
    the vendor's raw page directly, so the click can be tracked. ALWAYS
    name the actual vendor and clearly mark this as a third-party item,
    never present it as if it were in our own inventory."""
    session = SessionLocal()
    try:
        results = find_external_alternative(
            session,
            category=category,
            max_price=_to_float(max_price) or None,
        )
        for r in results:
            r["source"] = "external"
            r["click_url"] = f"/affiliate/click/{r['external_product_id']}"
        return results
    finally:
        session.close()


def _filter_to_majority_category(session, candidate_ids: list[str]) -> list[str]:
    """Semantic similarity search has no category awareness — a query
    like "shoes for standing all day" can pull in a stray candidate from
    an unrelated category (e.g. jeans) if its embedding happens to be
    close enough on generic comfort/everyday language. Since the actual
    customer request is virtually always about ONE category, this keeps
    only candidates matching whichever category is most common among the
    semantic results, discarding clear category outliers before ranking.
    If candidates are already evenly split across categories (no clear
    majority), all are kept as-is rather than guessing which is "correct".
    """
    if not candidate_ids:
        return candidate_ids

    products = (
        session.query(Product)
        .filter(Product.product_id.in_(candidate_ids))
        .all()
    )
    category_by_id = {p.product_id: p.category for p in products}

    category_counts: dict[str, int] = {}
    for pid in candidate_ids:
        cat = category_by_id.get(pid)
        if cat:
            category_counts[cat] = category_counts.get(cat, 0) + 1

    if not category_counts:
        return candidate_ids

    majority_category = max(category_counts, key=category_counts.get)
    majority_count = category_counts[majority_category]

    # No clear majority (e.g. a tie, or results are evenly spread) — don't
    # discard anything, since we can't confidently say what's an outlier.
    if majority_count < len(candidate_ids) / 2:
        return candidate_ids

    return [
        pid for pid in candidate_ids
        if category_by_id.get(pid) == majority_category
    ]


@mcp.tool()
def recommend_products(
    query: str,
    max_price: Union[str, float] = 0,
    top_n: Union[str, int] = 3,
) -> list[dict]:
    """Recommends the top N most relevant products for a natural-language
    request, using semantic search (understands vibes/occasions like
    'something for a night out', not just literal keyword matches),
    automatically filtered to in-stock items, with active promotions
    applied and factored into ranking. Each result includes a "score" and,
    if applicable, a "promotion" field with the discounted price."""
    session = SessionLocal()
    try:
        candidates = semantic_search_products(query, k=10)
        candidates = _filter_to_majority_category(session, candidates)
        target_price = _to_float(max_price) or None
        return rank_products(
            session,
            candidate_product_ids=candidates,
            target_price=target_price,
            top_n=int(_to_float(top_n, 3)),
        )
    finally:
        session.close()


@mcp.tool()
def stock(product_id: str, size: str = "", color: str = "") -> dict:
    """Check warehouse stock for a product, optionally filtered by size/color."""
    session = SessionLocal()
    try:
        return check_stock(session, product_id, size=size or None, color=color or None)
    finally:
        session.close()


@mcp.tool()
def alternatives(product_id: str, limit: Union[str, int] = 3) -> list[dict]:
    """Find in-stock alternative products in the same category."""
    session = SessionLocal()
    try:
        return find_alternatives(session, product_id, limit=int(_to_float(limit, 3)))
    finally:
        session.close()


@mcp.tool()
def fulfillment_options(product_id: str, store_name: str = "") -> dict:
    """Check available fulfillment methods for a product: real store
    pickup locations with actual stock, and a delivery estimate grounded
    in our real shipping policy (3-5 business days standard, free over
    $50; 1-2 business days express for $14.99). This is a policy-based
    estimate, NOT live carrier tracking — never state a specific delivery
    date, only the general timeframe."""
    session = SessionLocal()
    try:
        return get_fulfillment_options(session, product_id, store_name=store_name or None)
    finally:
        session.close()


@mcp.tool()
def stores(product_id: str, store_name: str = "") -> dict:
    """Check store-level stock for a product, optionally filtered to a
    named store. Leave store_name empty to check all stores."""
    session = SessionLocal()
    try:
        return check_store_stock(session, product_id, store_name=store_name or None)
    finally:
        session.close()


@mcp.tool()
def order_history(customer_id: str) -> list[dict]:
    """Look up all past orders for a customer ID. Read-only, same as
    `orders`. NOTE: there is no real authentication in this system — the
    customer must state their own customer ID for this to work (e.g.
    "I'm customer C001"). Never guess or assume a customer_id."""
    session = SessionLocal()
    try:
        return list_orders_for_customer(session, customer_id)
    finally:
        session.close()


@mcp.tool()
def return_eligibility(order_id: str) -> dict:
    """Checks whether an order LIKELY qualifies for a return, based on
    real order status and a 30-day policy window. Uses order date as an
    approximation for delivery date (no real delivery tracking exists) —
    ALWAYS mention this is an estimate, and that final eligibility also
    depends on item condition and Final Sale status, which this tool
    cannot verify. Never state a definitive "yes you can return this" —
    frame it as "likely eligible" or "likely not eligible", per the
    tool's own "likely_eligible" field and "reason" explanation."""
    session = SessionLocal()
    try:
        return check_return_eligibility(session, order_id)
    finally:
        session.close()


@mcp.tool()
def orders(order_id: str) -> dict:
    """Look up an order by its order ID. Read-only — does not create,
    modify, or cancel orders. Order creation happens only via the
    deterministic POST /checkout REST route, never through an agent."""
    session = SessionLocal()
    try:
        result = get_order(session, order_id)
        return result if result else {"found": False, "order_id": order_id}
    finally:
        session.close()


@mcp.tool()
def retrieve_policy_chunks(query: str, k: Union[str, int] = 3) -> list[dict]:
    """Retrieve the top-k most relevant policy document chunks (returns,
    refunds, shipping, exchanges) for a customer's policy question."""
    return _retrieve_policy_chunks(query, k=int(_to_float(k, 3)))


# NOTE — Phase 1 security boundary: there is deliberately NO checkout,
# payment, order-creation, refund, cancellation, or inventory-mutation
# tool registered here. Checkout is handled exclusively by the
# deterministic POST /checkout REST route, never by an autonomous agent.
#
# NOTE — Phase 2 change: the external-vendor fallback that was previously
# automatic inside `search` is now a SEPARATE tool (search_external_offers)
# used by a separate specialist (external_offer_agent). This is a
# deliberate tradeoff: it matches a cleaner 5-agent architecture, at the
# cost of the guarantee that external offers are always checked — the
# supervisor must now correctly route to external_offer_agent for this to
# fire, whereas previously it was unconditional. Documented explicitly as
# a known regression risk, not an oversight.


if __name__ == "__main__":
    mcp.run(transport="stdio")
