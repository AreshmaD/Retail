from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import ExternalProduct, AffiliateClick


def _external_product_to_dict(product: ExternalProduct) -> dict:
    return {
        "external_product_id": product.external_product_id,
        "name": product.name,
        "vendor_name": product.vendor_name,
        "category": product.category,
        "price": product.price,
        "rating": product.rating,
        "image_url": product.image_url,
        "tags": [t.strip() for t in product.tags.split(",") if t.strip()],
    }


# Maps loose customer/model phrasing to the actual category values
# stored in our (small, fixed) mock catalog.
CATEGORY_SYNONYMS = {
    "cocktail dress": "dresses", "party dress": "dresses",
    "evening dress": "dresses", "gown": "dresses",
    "sneaker": "shoes", "sneakers": "shoes", "trainer": "shoes",
    "jacket": "outerwear", "coat": "outerwear",
    "shirt": "tops", "t-shirt": "tops", "blouse": "tops",
    "jeans": "bottoms", "pants": "bottoms", "trousers": "bottoms",
}


def _normalize_category(category: str) -> str:
    key = category.strip().lower().rstrip("s")  # crude singularize
    for phrase, real_category in CATEGORY_SYNONYMS.items():
        phrase_singular = phrase.rstrip("s")
        if phrase_singular in key or key in phrase_singular:
            return real_category
    return category


def find_external_alternative(
    session: Session,
    category: str,
    max_price: Optional[float] = None,
    limit: int = 3,
) -> list[dict]:
    normalized = _normalize_category(category)
    q = session.query(ExternalProduct).filter(
        ExternalProduct.category.ilike(f"%{normalized}%")
    )
    if max_price is not None:
        q = q.filter(ExternalProduct.price <= max_price)

    results = q.limit(limit).all()
    return [_external_product_to_dict(p) for p in results]


def log_affiliate_click(
    session: Session,
    external_product_id: str,
    session_id: Optional[str] = None,
) -> dict:
    product = (
        session.query(ExternalProduct)
        .filter_by(external_product_id=external_product_id)
        .first()
    )
    if not product:
        return {"success": False, "error": "External product not found"}

    click = AffiliateClick(
        external_product_id=external_product_id,
        session_id=session_id,
    )
    session.add(click)
    session.commit()

    return {
        "success": True,
        "click_id": click.click_id,
        "redirect_url": product.affiliate_link_template,
        "vendor_name": product.vendor_name,
        "clicked_at": click.clicked_at.isoformat(),
    }


def get_click_stats(session: Session) -> dict:
    from sqlalchemy import func

    rows = (
        session.query(
            ExternalProduct.vendor_name,
            func.count(AffiliateClick.click_id).label("clicks"),
        )
        .join(AffiliateClick, AffiliateClick.external_product_id == ExternalProduct.external_product_id)
        .group_by(ExternalProduct.vendor_name)
        .all()
    )
    return {"clicks_by_vendor": {vendor: count for vendor, count in rows}}
