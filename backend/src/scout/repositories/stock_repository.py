from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Stock


class StockRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_variants(
        self,
        product_id: str,
        size: Optional[str] = None,
        color: Optional[str] = None,
    ) -> list[Stock]:
        q = self.session.query(Stock).filter_by(product_id=product_id)
        if size:
            q = q.filter(Stock.size.ilike(size))
        if color:
            q = q.filter(Stock.color.ilike(color))
        return q.all()

    def get_in_stock_product_ids(self) -> list[str]:
        rows = (
            self.session.query(Stock.product_id)
            .filter(Stock.quantity > 0)
            .distinct()
            .all()
        )
        return [r[0] for r in rows]
