from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Store
from scout.repositories.store_repository import StoreRepository
from scout.repositories.product_repository import ProductRepository


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
    product_repo = ProductRepository(session)
    store_repo = StoreRepository(session)

    product = product_repo.get_by_id(product_id)
    if not product:
        return {"product_id": product_id, "found": False, "stores": []}

    rows = store_repo.get_stock_for_product(product_id, store_name=store_name)
    if not rows:
        return {"product_id": product_id, "found": False, "stores": []}

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

    return {
        "product_id": product_id,
        "found": True,
        "stores": store_results,
    }
