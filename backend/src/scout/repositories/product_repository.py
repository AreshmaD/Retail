from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Product


class ProductRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, product_id: str) -> Optional[Product]:
        return self.session.query(Product).filter_by(product_id=product_id).first()

    def find(
        self,
        query: Optional[str] = None,
        category: Optional[str] = None,
        department: Optional[str] = None,
        brand: Optional[str] = None,
        max_price: Optional[float] = None,
        min_rating: Optional[float] = None,
        product_id_filter: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[Product]:
        q = self.session.query(Product)

        if product_id_filter is not None:
            q = q.filter(Product.product_id.in_(product_id_filter))
        if category:
            q = q.filter(Product.category.ilike(f"%{category}%"))
        if department:
            q = q.filter(Product.department.ilike(f"%{department}%"))
        if brand:
            q = q.filter(Product.brand.ilike(f"%{brand}%"))
        if max_price is not None:
            q = q.filter(Product.price <= max_price)
        if min_rating is not None:
            q = q.filter(Product.rating >= min_rating)
        if query:
            like = f"%{query}%"
            q = q.filter(
                (Product.name.ilike(like))
                | (Product.description.ilike(like))
                | (Product.tags.ilike(like))
            )

        return q.limit(limit).all()

    def find_by_category(self, category: str, exclude_product_id: Optional[str] = None, limit: int = 10) -> list[Product]:
        q = self.session.query(Product).filter(Product.category == category)
        if exclude_product_id:
            q = q.filter(Product.product_id != exclude_product_id)
        return q.limit(limit).all()

    def get_all(self) -> list[Product]:
        return self.session.query(Product).all()
