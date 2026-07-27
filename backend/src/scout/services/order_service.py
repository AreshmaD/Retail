import uuid
from typing import Optional

from sqlalchemy.orm import Session

from scout.repositories.order_repository import OrderRepository
from scout.repositories.product_repository import ProductRepository


def _order_to_dict(order_id: str, session: Session) -> dict:
    order_repo = OrderRepository(session)
    product_repo = ProductRepository(session)

    order = order_repo.get_by_id(order_id)
    items = order_repo.get_items(order_id)

    item_list = []
    for item in items:
        product = product_repo.get_by_id(item.product_id)
        item_list.append({
            "product_id": item.product_id,
            "name": product.name if product else "Unknown product",
            "quantity": item.quantity,
            "price_at_purchase": item.price_at_purchase,
        })

    return {
        "order_id": order.order_id,
        "customer_id": order.customer_id,
        "status": order.status,
        "created_at": order.created_at.isoformat(),
        "items": item_list,
        "total": round(sum(i["quantity"] * i["price_at_purchase"] for i in item_list), 2),
    }


def get_order(session: Session, order_id: str) -> Optional[dict]:
    order_repo = OrderRepository(session)
    order = order_repo.get_by_id(order_id)
    return _order_to_dict(order_id, session) if order else None


def list_orders_for_customer(session: Session, customer_id: str) -> list[dict]:
    order_repo = OrderRepository(session)
    orders = order_repo.find_by_customer(customer_id)
    return [_order_to_dict(o.order_id, session) for o in orders]


def create_order(
    session: Session,
    customer_id: str,
    cart_items: list[dict],
) -> dict:
    """Business logic: prices are always looked up server-side from the
    real product record — never trusted from the caller. This remains
    true regardless of the repository refactor."""
    order_repo = OrderRepository(session)
    product_repo = ProductRepository(session)

    order_id = f"O{uuid.uuid4().hex[:8].upper()}"
    order_repo.create(order_id=order_id, customer_id=customer_id, status="pending")

    for item in cart_items:
        product = product_repo.get_by_id(item["product_id"])
        if not product:
            continue
        order_repo.add_item(
            order_id=order_id,
            product_id=item["product_id"],
            quantity=item.get("quantity", 1),
            price_at_purchase=product.price,
        )

    session.commit()
    return get_order(session, order_id)


def update_order_status(session: Session, order_id: str, status: str) -> Optional[dict]:
    order_repo = OrderRepository(session)
    order = order_repo.update_status(order_id, status)
    if not order:
        return None
    session.commit()
    return get_order(session, order_id)
