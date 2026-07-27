from typing import Optional

from sqlalchemy.orm import Session

from scout.db.models import Order, OrderItem


class OrderRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, order_id: str) -> Optional[Order]:
        return self.session.query(Order).filter_by(order_id=order_id).first()

    def get_items(self, order_id: str) -> list[OrderItem]:
        return self.session.query(OrderItem).filter_by(order_id=order_id).all()

    def find_by_customer(self, customer_id: str) -> list[Order]:
        return self.session.query(Order).filter_by(customer_id=customer_id).all()

    def create(self, order_id: str, customer_id: str, status: str = "pending") -> Order:
        order = Order(order_id=order_id, customer_id=customer_id, status=status)
        self.session.add(order)
        return order

    def add_item(
        self, order_id: str, product_id: str, quantity: int, price_at_purchase: float
    ) -> OrderItem:
        item = OrderItem(
            order_id=order_id,
            product_id=product_id,
            quantity=quantity,
            price_at_purchase=price_at_purchase,
        )
        self.session.add(item)
        return item

    def update_status(self, order_id: str, status: str) -> Optional[Order]:
        order = self.get_by_id(order_id)
        if order:
            order.status = status
        return order
