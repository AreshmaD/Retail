import { useState, useEffect } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { loadStripe } from "@stripe/stripe-js";
import { Elements } from "@stripe/react-stripe-js";
import { useCart } from "../context/CartContext";
import CheckoutForm from "../components/CheckoutForm";
import "./CartPage.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}
const stripePromise = loadStripe(import.meta.env.VITE_STRIPE_PUBLISHABLE_KEY);

export default function CartPage() {
  const { items, updateQuantity, removeFromCart, clearCart, total } = useCart();
  const [isStartingCheckout, setIsStartingCheckout] = useState(false);
  const [checkoutSession, setCheckoutSession] = useState(null);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    if (searchParams.get("checkout") === "1" && items.length > 0 && !checkoutSession && !confirmation) {
      handleStartCheckout();
      const next = new URLSearchParams(searchParams);
      next.delete("checkout");
      setSearchParams(next, { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleStartCheckout() {
    setIsStartingCheckout(true);
    setError(null);
    try {
      const response = await fetch(`${API_BASE}/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          items: items.map((i) => ({ product_id: i.product_id, quantity: i.quantity })),
        }),
      });
      const data = await response.json();

      if (!data.success) {
        setError(data.error || "Checkout failed. Please try again.");
        return;
      }

      setCheckoutSession({
        clientSecret: data.payment.client_secret,
        order: data.order,
      });
    } catch (err) {
      setError("Something went wrong starting checkout. Please try again.");
    } finally {
      setIsStartingCheckout(false);
    }
  }

  function handlePaymentSuccess(paymentIntent) {
    setConfirmation({
      order: checkoutSession.order,
      paymentIntent,
    });
    clearCart();
  }

  if (confirmation) {
    return (
      <div className="cart-confirmation">
        <p className="cart-confirmation-check">✓</p>
        <h2>Order confirmed!</h2>
        <p className="cart-confirmation-id">Order #{confirmation.order.order_id}</p>
        <p className="cart-confirmation-total">
          Total charged: ${confirmation.order.total.toFixed(2)}
        </p>
        <p className="cart-confirmation-note">
          Payment status: {confirmation.paymentIntent.status} (Stripe test mode)
        </p>
        <Link to="/" className="cart-empty-link">Continue shopping</Link>
      </div>
    );
  }

  if (checkoutSession) {
    return (
      <div className="cart-page">
        <h2 className="cart-title">Payment</h2>
        <p style={{ fontSize: "13px", color: "var(--color-text-secondary)", marginBottom: "16px" }}>
          Order total: ${checkoutSession.order.total.toFixed(2)} — use test card 4242 4242 4242 4242,
          any future expiry, any CVC.
        </p>
        <Elements stripe={stripePromise} options={{ clientSecret: checkoutSession.clientSecret }}>
          <CheckoutForm onSuccess={handlePaymentSuccess} />
        </Elements>
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="cart-empty">
        <p>Your cart is empty.</p>
        <Link to="/" className="cart-empty-link">Continue shopping</Link>
      </div>
    );
  }

  return (
    <div className="cart-page">
      <h2 className="cart-title">Your Cart</h2>

      <div className="cart-items">
        {items.map((item) => (
          <div key={item.product_id} className="cart-item">
            <img className="cart-item-image" src={resolveImageUrl(item.image_url)} alt={item.name} />

            <div className="cart-item-details">
              <p className="cart-item-brand">{item.brand}</p>
              <p className="cart-item-name">{item.name}</p>
              <p className="cart-item-price">${item.price.toFixed(2)}</p>
            </div>

            <div className="cart-item-qty">
              <button
                className="cart-qty-btn"
                onClick={() => updateQuantity(item.product_id, item.quantity - 1)}
              >
                −
              </button>
              <span className="cart-qty-value">{item.quantity}</span>
              <button
                className="cart-qty-btn"
                onClick={() => updateQuantity(item.product_id, item.quantity + 1)}
              >
                +
              </button>
            </div>

            <p className="cart-item-subtotal">
              ${(item.price * item.quantity).toFixed(2)}
            </p>

            <button
              className="cart-item-remove"
              onClick={() => removeFromCart(item.product_id)}
              aria-label="Remove item"
            >
              ✕
            </button>
          </div>
        ))}
      </div>

      {error && <p className="cart-error">{error}</p>}

      <div className="cart-summary">
        <button className="cart-clear-btn" onClick={clearCart}>
          Clear cart
        </button>

        <div className="cart-total-row">
          <span>Total</span>
          <span className="cart-total-value">${total.toFixed(2)}</span>
        </div>

        <button
          className="cart-checkout-btn"
          onClick={handleStartCheckout}
          disabled={isStartingCheckout}
        >
          {isStartingCheckout ? "Starting…" : "Proceed to checkout"}
        </button>
      </div>
    </div>
  );
}
