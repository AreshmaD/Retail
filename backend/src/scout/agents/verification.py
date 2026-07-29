import re

PRICE_PATTERN = re.compile(r"\$(\d+(?:\.\d{1,2})?)")

# Real, fixed policy constants that are legitimate to mention in a reply
# even though they don't come from a product/promotion record this turn —
# shipping fees and thresholds, grounded in the actual shipping.md policy
# (see services/store_service.py, which uses these same values). Treating
# these as "unverified" was a real false-positive bug: the fulfillment
# tool correctly returned these numbers, but verification only knew about
# product prices, not policy constants.
KNOWN_POLICY_PRICES = {"0.00", "5.99", "14.99", "75.00"}


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
    real price from this turn's actual tool results, the customer's own
    stated budget, OR a known fixed policy constant (shipping fees/
    thresholds) — the last of these because those numbers are real and
    correct even though they don't come from a product record. If NO
    products were returned this turn, there's no product-price ground
    truth to check — treated as unverifiable-but-passing to avoid false
    positives on non-product answers."""
    if not products:
        return True, "no product data this turn — price check not applicable"

    known_prices = _collect_known_prices(products)
    customer_stated_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(customer_message)}
    mentioned_prices = {f"{float(m):.2f}" for m in PRICE_PATTERN.findall(reply)}

    allowed_prices = known_prices | customer_stated_prices | KNOWN_POLICY_PRICES
    unverified = mentioned_prices - allowed_prices

    if unverified:
        return False, (
            f"reply mentions price(s) {sorted(unverified)} that do not match "
            f"any real price/promotion from this turn's tool results "
            f"({sorted(known_prices)}), the customer's own stated budget "
            f"({sorted(customer_stated_prices)}), or a known policy constant"
        )

    return True, "all mentioned prices verified against tool results, customer budget, or policy"
