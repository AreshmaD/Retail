from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.services.order_service import create_order
from scout.services.payment_service import create_test_payment, PaymentProcessingError

router = APIRouter()


class CheckoutItem(BaseModel):
    product_id: str
    quantity: int = 1


class CheckoutRequest(BaseModel):
    items: list[CheckoutItem]
    customer_id: Optional[str] = "guest"


@router.post("/checkout")
def checkout(request: CheckoutRequest):
    session = SessionLocal()
    try:
        cart_items = [{"product_id": i.product_id, "quantity": i.quantity} for i in request.items]
        order = create_order(session, customer_id=request.customer_id, cart_items=cart_items)

        if not order or not order.get("items"):
            return {"success": False, "error": "Could not create order — check product IDs."}

        try:
            payment = create_test_payment(
                amount_usd=order["total"],
                description=f"Order {order['order_id']}",
            )
        except PaymentProcessingError as e:
            return {"success": False, "error": str(e)}

        return {"success": True, "order": order, "payment": payment}
    finally:
        session.close()
