from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Store
from scout.repositories.store_repository import StoreRepository
from scout.repositories.product_repository import ProductRepository

SHIPPING_STANDARD_DAYS = "3-5 business days"
SHIPPING_EXPRESS_DAYS = "1-2 business days"
SHIPPING_FREE_THRESHOLD = 75.0
SHIPPING_STANDARD_FEE = 5.99
SHIPPING_EXPRESS_FEE = 14.99


def _store_to_dict(store: Store) -> dict:
    return {
        "store_id": store.store_id,
        "name": store.name,
        "address": store.address,
    }


def list_stores(session: Session) -> list[dict]:
    store_repo = StoreRepository(session)
    return [_store_to_dict(s) for s in store_repo.get_all()]


def find_store_by_name(session: Session, name: str) -> Optional[dict]:
    store_repo = StoreRepository(session)
    store = store_repo.find_by_name(name)
    return _store_to_dict(store) if store else None


def check_store_stock(
    session: Session,
    product_id: str,
    store_name: Optional[str] = None,
) -> dict:
    """If a specific store is named and has NO stock, automatically
    checks all other stores and returns them as alternatives — a
    customer asking "is it at Maple Grove?" shouldn't get a dead-end
    answer if Downtown Minneapolis has 5 in stock. The response marks
    these as alternatives so the agent can present them clearly as
    "not at X, but available at Y", not as if X actually had it."""
    product_repo = ProductRepository(session)
    store_repo = StoreRepository(session)
    requested_store_name = (
        store_name.strip() if isinstance(store_name, str) and store_name.strip() else None
    )

    product = product_repo.get_by_id(product_id)
    if not product:
        return {
            "product_id": product_id,
            "found": False,
            "requested_store_name": requested_store_name,
            "stores": [],
        }

    rows = store_repo.get_stock_for_product(product_id, store_name=requested_store_name)

    if requested_store_name and not rows:
        return {
            "product_id": product_id,
            "found": True,
            "requested_store_name": requested_store_name,
            "requested_store_had_no_stock": False,
            "stores": [],
        }

    requested_store_had_no_stock = False
    if requested_store_name and all(stock_row.quantity == 0 for stock_row, _ in rows):
        requested_store_had_no_stock = True
        rows = store_repo.get_stock_for_product(product_id, store_name=None)

    if not rows:
        return {
            "product_id": product_id,
            "found": False,
            "requested_store_name": requested_store_name,
            "stores": [],
            "alternatives_checked": requested_store_had_no_stock,
        }

    store_results = [
        {
            "store_id": store.store_id,
            "store_name": store.name,
            "address": store.address,
            "quantity": stock_row.quantity,
            "in_stock": stock_row.quantity > 0,
        }
        for stock_row, store in rows
    ]

    if requested_store_had_no_stock:
        store_results = [s for s in store_results if s["in_stock"]]

    return {
        "product_id": product_id,
        "found": True,
        "requested_store_name": requested_store_name,
        "requested_store_had_no_stock": requested_store_had_no_stock,
        "stores": store_results,
    }

def get_fulfillment_options(
    session: Session,
    product_id: str,
    store_name: Optional[str] = None,
) -> dict:
    """Returns available fulfillment methods for a product: real store
    pickup (checked against actual store stock) and delivery, with a
    shipping estimate grounded in the SAME numbers from shipping.md that
    policy_agent would cite if asked directly — never a fabricated
    delivery date, since there is no live carrier integration."""
    product_repo = ProductRepository(session)
    product = product_repo.get_by_id(product_id)
    if not product:
        return {"product_id": product_id, "found": False}

    pickup_result = check_store_stock(session, product_id, store_name=store_name)
    pickup_locations = [
        s for s in pickup_result.get("stores", []) if s["in_stock"]
    ]

    if product.price >= SHIPPING_FREE_THRESHOLD:
        shipping_fee = 0.0
        shipping_fee_note = "free (order qualifies for free shipping over $75)"
    else:
        shipping_fee = SHIPPING_STANDARD_FEE
        shipping_fee_note = f"${SHIPPING_STANDARD_FEE} (orders under $75)"

    return {
        "product_id": product_id,
        "found": True,
        "pickup": {
            "available": len(pickup_locations) > 0,
            "locations": pickup_locations,
        },
        "delivery": {
            "available": True,
            "standard_estimate": SHIPPING_STANDARD_DAYS,
            "standard_fee": shipping_fee,
            "standard_fee_note": shipping_fee_note,
            "express_estimate": SHIPPING_EXPRESS_DAYS,
            "express_fee": SHIPPING_EXPRESS_FEE,
            "note": "Estimate based on standard shipping policy — not a live carrier tracking date.",
        },
    }
