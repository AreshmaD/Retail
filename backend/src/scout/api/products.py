from typing import Optional

from fastapi import APIRouter, Query

from scout.db.session import SessionLocal
from scout.services.product_service import search_products, get_product, find_alternatives
from scout.services.affiliate_service import find_external_alternative

router = APIRouter()


@router.get("/products")
def list_products(
    query: Optional[str] = Query(default=None),
    category: Optional[str] = Query(default=None),
    department: Optional[str] = Query(default=None),
    max_price: Optional[float] = Query(default=None),
    min_rating: Optional[float] = Query(default=None),
    limit: int = Query(default=50),
):
    """Plain product listing/browsing endpoint — no LLM involved. Used by
    the homepage, category grid pages, and header search bar. If internal
    search returns nothing, automatically checks the external vendor
    catalog as a guaranteed, deterministic fallback — no AI/agent
    judgment required for this endpoint, unlike the chat path's
    agent-routed external check."""
    session = SessionLocal()
    try:
        results = search_products(
            session,
            query=query,
            category=category,
            department=department,
            max_price=max_price,
            min_rating=min_rating,
            in_stock_only=True,
            limit=limit,
        )
        if results:
            return results

        external_results = find_external_alternative(
            session,
            category=category or query or "",
            max_price=max_price,
        )
        for r in external_results:
            r["source"] = "external"
            r["click_url"] = f"/affiliate/click/{r['external_product_id']}"
        return external_results
    finally:
        session.close()


@router.get("/products/{product_id}")
def get_product_detail(product_id: str):
    """Single product detail lookup, for a future product detail page."""
    session = SessionLocal()
    try:
        product = get_product(session, product_id)
        if not product:
            return {"error": "Product not found"}
        return product
    finally:
        session.close()


@router.get("/products/{product_id}/similar")
def get_similar_products(product_id: str, limit: int = 4):
    """Find in-stock alternatives in the same category. No LLM involved."""
    session = SessionLocal()
    try:
        return find_alternatives(session, product_id, limit=limit)
    finally:
        session.close()
