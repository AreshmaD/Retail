from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.product_service import check_stock, _get_active_promotion_dict

router = APIRouter()


class AddToCartRequest(BaseModel):
    product_id: str
    quantity: int = 1


@router.post("/cart/add")
def add_to_cart(request: AddToCartRequest):
    """Authoritative add-to-cart validation endpoint. The frontend never
    sends a price — only a product_id and quantity. This looks up the
    real product, checks real stock, and returns the true price (including
    any active promotion) so the client-side cart never has to trust a
    browser-supplied value for what something costs or whether it's
    actually available.
    """
    session = SessionLocal()
    try:
        product_repo = ProductRepository(session)
        product = product_repo.get_by_id(request.product_id)

        if not product:
            return {"success": False, "error": "Product not found"}

        stock_info = check_stock(session, request.product_id)
        if not stock_info["in_stock"]:
            return {
                "success": False,
                "error": "Out of stock",
                "in_stock": False,
            }

        promotion = _get_active_promotion_dict(session, product)
        unit_price = promotion["discounted_price"] if promotion else product.price

        return {
            "success": True,
            "in_stock": True,
            "product_id": product.product_id,
            "name": product.name,
            "brand": product.brand,
            "image_url": product.image_url,
            "unit_price": unit_price,
            "quantity": request.quantity,
            "line_total": round(unit_price * request.quantity, 2),
            "promotion": promotion,
        }
    finally:
        session.close()
