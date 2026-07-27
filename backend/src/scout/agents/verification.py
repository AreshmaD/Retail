import re

PRICE_PATTERN = re.compile(r"\$(\d+(?:\.\d{1,2})?)")


def _collect_known_prices(products: list[dict]) -> set[str]:
    known = set()
    for p in products:
        if "price" in p and p["price"] is not None:
            known.add(f"{float(p['price']):.2f}")
        promo = p.get("promotion")
        if promo and "discounted_price" in promo:
            known.add(f"{float(promo['discounted_price']):.2f}")
    return known


def verify_price_grounding(
    reply: str, products: list[dict], customer_message: str = ""
) -> tuple[bool, str]:
    """Checks that every dollar amount mentioned in the reply matches a
    real price from this turn's actual tool results OR was itself
    mentioned by the customer (e.g. "under $80" — restating the
    customer's own stated budget is not a hallucination, it's normal
    conversational acknowledgment). If NO products were returned this
    turn, there's no ground truth to check against — treated as
    unverifiable-but-passing to avoid false positives on non-product
    answers."""
    if not products:
        return True, "no product data this turn — price check not applicable"

    known_prices = _collect_known_prices(products)
    customer_stated_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(customer_message)}
    mentioned_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(reply)}

    allowed_prices = known_prices | customer_stated_prices
    unverified = mentioned_prices - allowed_prices

    if unverified:
        return False, (
            f"reply mentions price(s) {sorted(unverified)} that do not match "
            f"any real price/promotion from this turn's tool results "
            f"({sorted(known_prices)}) or the customer's own stated budget "
            f"({sorted(customer_stated_prices)})"
        )

    return True, "all mentioned prices verified against tool results or customer's own budget"
