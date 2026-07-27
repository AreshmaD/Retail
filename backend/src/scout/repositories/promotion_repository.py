from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Promotion


class PromotionRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_for_product(self, product_id: str) -> Optional[Promotion]:
        return (
            self.session.query(Promotion)
            .filter_by(product_id=product_id, active=True)
            .first()
        )

    def get_for_category(self, category: str) -> Optional[Promotion]:
        return (
            self.session.query(Promotion)
            .filter_by(category=category, active=True)
            .first()
        )
