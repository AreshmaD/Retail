from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy.orm import Session

from scout.db.models import ExternalProduct, AffiliateClick


class ExternalOfferSearchFilters(BaseModel):
    query: str = ""
    category: str = ""
    subcategory: str | None = None
    use_case: str | None = None
    required_attributes: list[str] = Field(default_factory=list)
    waterproof: bool | None = None
    color: str | None = None
    budget_max: float | None = None
    availability_required: bool = True

    @field_validator("budget_max")
    @classmethod
    def _valid_budget(cls, value):
        if value is not None and value < 0:
            raise ValueError("budget_max must be non-negative")
        return value

    @field_validator("required_attributes", mode="before")
    @classmethod
    def _normalize_attributes(cls, value):
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        raise ValueError("required_attributes must be a list or comma-separated string")


class StructuredExternalOffer(BaseModel):
    external_product_id: str
    name: str
    vendor_name: str
    category: str
    subcategory: str | None = None
    use_cases: list[str] = Field(default_factory=list)
    attributes: list[str] = Field(default_factory=list)
    waterproof: bool | None = None
    color: str | None = None
    price: float
    original_price: float | None = None
    currency: str = "USD"
    availability: str = "in_stock"
    product_url: HttpUrl
    click_url: str
    source: str = "external"
    last_verified_at: str
    rating: float | None = None
    image_url: str | None = None

    @field_validator("price", "original_price")
    @classmethod
    def _valid_price(cls, value):
        if value is not None and value < 0:
            raise ValueError("price must be non-negative")
        return value


DEMO_EXTERNAL_OFFERS = {
    "EX010": {
        "external_product_id": "EX010",
        "name": "TrailGuard Waterproof Hiking Shoe",
        "vendor_name": "Outdoor Demo Retailer",
        "category": "shoes",
        "subcategory": "hiking shoes",
        "use_cases": ["hiking"],
        "attributes": ["waterproof", "trail"],
        "waterproof": True,
        "color": "black",
        "price": 64.99,
        "original_price": 79.99,
        "currency": "USD",
        "availability": "in_stock",
        "product_url": "https://example.com/outdoor-demo/trailguard-waterproof-hiking-shoe",
        "click_url": "/affiliate/click/EX010",
        "source": "external",
        "last_verified_at": "2026-07-30T00:00:00+00:00",
        "rating": 4.4,
    }
}


def _external_product_to_dict(product: ExternalProduct) -> dict:
    tags = [t.strip() for t in product.tags.split(",") if t.strip()]
    offer = {
        "external_product_id": product.external_product_id,
        "name": product.name,
        "vendor_name": product.vendor_name,
        "category": product.category,
        "subcategory": _tag_value(tags, "subcategory"),
        "use_cases": _tag_values(tags, "use"),
        "attributes": _tag_values(tags, "attr"),
        "waterproof": _tag_bool(tags, "waterproof"),
        "color": _tag_value(tags, "color"),
        "price": product.price,
        "original_price": None,
        "currency": "USD",
        "availability": _tag_value(tags, "availability") or "in_stock",
        "product_url": product.affiliate_link_template,
        "click_url": f"/affiliate/click/{product.external_product_id}",
        "source": "external",
        "last_verified_at": "2026-07-30T00:00:00+00:00",
        "rating": product.rating,
        "image_url": product.image_url,
        "tags": tags,
    }
    return _validated_offer_dict(offer)


# Maps loose customer/model phrasing to the actual category values
# stored in our (small, fixed) mock catalog.
CATEGORY_SYNONYMS = {
    "cocktail dress": "dresses", "party dress": "dresses",
    "evening dress": "dresses", "gown": "dresses",
    "sneaker": "shoes", "sneakers": "shoes", "trainer": "shoes",
    "jacket": "outerwear", "coat": "outerwear",
    "shirt": "tops", "t-shirt": "tops", "blouse": "tops",
    "jeans": "bottoms", "pants": "bottoms", "trousers": "bottoms",
    "footwear": "shoes", "hiking shoe": "shoes", "hiking shoes": "shoes",
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
    query: str = "",
    subcategory: str | None = None,
    use_case: str | None = None,
    required_attributes: list[str] | str | None = None,
    waterproof: bool | None = None,
    color: str | None = None,
    budget_max: Optional[float] = None,
    availability_required: bool = True,
) -> list[dict]:
    filters = _build_filters(
        query=query,
        category=category,
        subcategory=subcategory,
        use_case=use_case,
        required_attributes=required_attributes,
        waterproof=waterproof,
        color=color,
        budget_max=budget_max if budget_max is not None else max_price,
        availability_required=availability_required,
    )
    normalized = _normalize_category(filters.category)
    q = session.query(ExternalProduct).filter(
        ExternalProduct.category.ilike(f"%{normalized}%")
    )

    db_offers = [_external_product_to_dict(p) for p in q.all()]
    offers_by_id = {offer["external_product_id"]: offer for offer in db_offers}
    for offer_id, offer in DEMO_EXTERNAL_OFFERS.items():
        offers_by_id.setdefault(offer_id, _validated_offer_dict(offer))

    candidates = list(offers_by_id.values())
    filtered = [_annotate_constraints(offer, filters) for offer in candidates]
    matches = [offer for offer in filtered if _offer_matches(offer, filters)]
    matches.sort(key=lambda offer: (offer["price"], offer["external_product_id"]))
    return matches[:limit]


def _build_filters(**kwargs) -> ExternalOfferSearchFilters:
    query = kwargs.get("query") or ""
    category = kwargs.get("category") or _category_from_query(query)
    required = kwargs.get("required_attributes")
    use_case = kwargs.get("use_case") or _use_case_from_query(query)
    waterproof = kwargs.get("waterproof")
    query_lower = query.lower()
    if waterproof is None and "waterproof" in query_lower:
        waterproof = True
    if required in (None, "") and "waterproof" in query_lower:
        required = ["waterproof"]
    return ExternalOfferSearchFilters(
        query=query,
        category=category or "",
        subcategory=kwargs.get("subcategory"),
        use_case=use_case,
        required_attributes=required or [],
        waterproof=waterproof,
        color=kwargs.get("color"),
        budget_max=kwargs.get("budget_max") if kwargs.get("budget_max") is not None else kwargs.get("max_price"),
    )


def _category_from_query(query: str) -> str:
    lowered = (query or "").lower()
    if "shoe" in lowered or "footwear" in lowered:
        return "shoes"
    if "dress" in lowered:
        return "dresses"
    return ""


def _use_case_from_query(query: str) -> str | None:
    lowered = (query or "").lower()
    for use_case in ("hiking", "running", "formal", "casual", "athletic"):
        if use_case in lowered:
            return use_case
    return None


def _offer_matches(offer: dict, filters: ExternalOfferSearchFilters) -> bool:
    if filters.category and _normalize_category(offer.get("category", "")) != _normalize_category(filters.category):
        return False
    if filters.subcategory and filters.subcategory.lower() not in str(offer.get("subcategory", "")).lower():
        return False
    if filters.use_case and filters.use_case.lower() not in offer.get("use_cases", []):
        return False
    if filters.waterproof is not None and offer.get("waterproof") is not filters.waterproof:
        return False
    attributes = set(offer.get("attributes") or [])
    for attribute in filters.required_attributes:
        if attribute not in attributes:
            return False
    if filters.color and offer.get("color") and offer["color"].lower() != filters.color.lower():
        return False
    if filters.budget_max is not None and offer.get("price", 0) > filters.budget_max:
        return False
    if filters.availability_required and offer.get("availability") != "in_stock":
        return False
    return True


def _annotate_constraints(offer: dict, filters: ExternalOfferSearchFilters) -> dict:
    annotated = dict(offer)
    satisfied = []
    if filters.use_case and filters.use_case in annotated.get("use_cases", []):
        satisfied.append(filters.use_case)
    if filters.waterproof is True and annotated.get("waterproof") is True:
        satisfied.append("waterproof")
    for attribute in filters.required_attributes:
        if attribute in annotated.get("attributes", []):
            satisfied.append(attribute)
    if filters.budget_max is not None and annotated.get("price", 0) <= filters.budget_max:
        satisfied.append(f"under ${filters.budget_max:g}")
    annotated["satisfied_constraints"] = sorted(set(satisfied))
    return annotated


def _requested_constraints(filters: ExternalOfferSearchFilters) -> list[str]:
    constraints = []
    if filters.category:
        constraints.append(filters.category)
    if filters.use_case:
        constraints.append(filters.use_case)
    if filters.waterproof is True:
        constraints.append("waterproof")
    constraints.extend(filters.required_attributes)
    if filters.budget_max is not None:
        constraints.append(f"under ${filters.budget_max:g}")
    return sorted(set(constraints))


def _external_candidate_count(session: Session, filters: ExternalOfferSearchFilters) -> int:
    normalized = _normalize_category(filters.category)
    db_count = session.query(ExternalProduct).filter(ExternalProduct.category.ilike(f"%{normalized}%")).count() if normalized else session.query(ExternalProduct).count()
    demo_count = sum(1 for offer in DEMO_EXTERNAL_OFFERS.values() if not normalized or _normalize_category(offer["category"]) == normalized)
    return db_count + demo_count


def _validated_offer_dict(offer: dict) -> dict:
    return StructuredExternalOffer(**dict(offer)).model_dump(mode="json")


def _tag_values(tags: list[str], prefix: str) -> list[str]:
    marker = f"{prefix}:"
    return [tag[len(marker):].strip().lower() for tag in tags if tag.startswith(marker) and tag[len(marker):].strip()]


def _tag_value(tags: list[str], prefix: str) -> str | None:
    values = _tag_values(tags, prefix)
    return values[0] if values else None


def _tag_bool(tags: list[str], prefix: str) -> bool | None:
    value = _tag_value(tags, prefix)
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    return None


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
