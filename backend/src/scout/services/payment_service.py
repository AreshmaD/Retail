import os

import stripe
from dotenv import load_dotenv

load_dotenv()

stripe.api_key = os.getenv("STRIPE_MCP_KEY")


def create_test_payment(amount_usd: float, description: str = "Scout order") -> dict:
    """Creates a Stripe test-mode PaymentIntent for the given amount.
    Deterministic — no LLM reasoning needed to move money."""
    if not stripe.api_key or not stripe.api_key.startswith(("sk_test_", "rk_test_")):
        raise RuntimeError(
            "Stripe key is missing or not a test-mode key. "
            "Refusing to process a payment outside test mode."
        )

    amount_cents = int(round(amount_usd * 100))

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        description=description,
        automatic_payment_methods={"enabled": True},
        confirm=False,
    )

    return {
        "payment_intent_id": intent.id,
        "amount_usd": amount_usd,
        "status": intent.status,
        "client_secret": intent.client_secret,
    }
