from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Store, StoreStock


class StoreRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, store_id: str) -> Optional[Store]:
        return self.session.query(Store).filter_by(store_id=store_id).first()

    def find_by_name(self, name: str) -> Optional[Store]:
        return self.session.query(Store).filter(Store.name.ilike(f"%{name}%")).first()

    def get_all(self) -> list[Store]:
        return self.session.query(Store).all()

    def get_stock_for_product(
        self, product_id: str, store_name: Optional[str] = None
    ) -> list[tuple[StoreStock, Store]]:
        q = (
            self.session.query(StoreStock, Store)
            .join(Store, StoreStock.store_id == Store.store_id)
            .filter(StoreStock.product_id == product_id)
        )
        if store_name:
            q = q.filter(Store.name.ilike(f"%{store_name}%"))
        return q.all()
