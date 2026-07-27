from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Product, Promotion
from scout.services.product_service import check_stock
from scout.services.cache import ttl_cache

WEIGHTS = {
    "rating": 0.35,
    "price_fit": 0.30,
    "promotion": 0.15,
    "semantic_rank": 0.20,
}


def _get_active_promotion(session: Session, product: Product) -> Optional[Promotion]:
    specific = (
        session.query(Promotion)
        .filter_by(product_id=product.product_id, active=True)
        .first()
    )
    if specific:
        return specific
    return (
        session.query(Promotion)
        .filter_by(category=product.category, active=True)
        .first()
    )


def _score_candidate(
    product: Product,
    semantic_position: int,
    total_candidates: int,
    target_price: Optional[float],
    promotion: Optional[Promotion],
) -> float:
    rating_score = (product.rating or 0) / 5.0

    if target_price:
        price_diff_ratio = abs(product.price - target_price) / target_price
        price_fit_score = max(0.0, 1.0 - price_diff_ratio)
    else:
        price_fit_score = 0.5

    promotion_score = 1.0 if promotion else 0.0
    semantic_rank_score = 1.0 - (semantic_position / max(total_candidates, 1))

    return (
        WEIGHTS["rating"] * rating_score
        + WEIGHTS["price_fit"] * price_fit_score
        + WEIGHTS["promotion"] * promotion_score
        + WEIGHTS["semantic_rank"] * semantic_rank_score
    )


@ttl_cache
def rank_products(
    session: Session,
    candidate_product_ids: list[str],
    target_price: Optional[float] = None,
    top_n: int = 3,
) -> list[dict]:
    scored = []
    total = len(candidate_product_ids)

    for position, product_id in enumerate(candidate_product_ids):
        product = session.query(Product).filter_by(product_id=product_id).first()
        if not product:
            continue

        stock_info = check_stock(session, product_id)
        if not stock_info["in_stock"]:
            continue

        promotion = _get_active_promotion(session, product)
        score = _score_candidate(product, position, total, target_price, promotion)

        result = {
            "product_id": product.product_id,
            "name": product.name,
            "brand": product.brand,
            "department": product.department,
            "category": product.category,
            "description": product.description,
            "price": product.price,
            "rating": product.rating,
            "image_url": product.image_url,
            "tags": [t.strip() for t in product.tags.split(",") if t.strip()],
            "score": round(score, 4),
        }

        if promotion:
            discounted_price = round(product.price * (1 - promotion.discount_percent / 100), 2)
            result["promotion"] = {
                "label": promotion.label,
                "discount_percent": promotion.discount_percent,
                "discounted_price": discounted_price,
            }

        scored.append(result)

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:top_n]
