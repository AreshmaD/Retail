import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useCart } from "../context/CartContext";
import "./CartToast.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

export default function CartToast() {
  const { lastAdded, notifyId, itemCount, total } = useCart();
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (notifyId === 0) return;
    setVisible(true);
    const timer = setTimeout(() => setVisible(false), 4000);
    return () => clearTimeout(timer);
  }, [notifyId]);

  if (!visible || !lastAdded) return null;

  return (
    <div className="cart-toast" role="status">
      <div className="cart-toast-header">
        <span className="cart-toast-check">✓</span>
        <span className="cart-toast-title">Added to cart!</span>
        <button
          className="cart-toast-close"
          onClick={() => setVisible(false)}
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>

      <div className="cart-toast-body">
        {lastAdded.image_url ? (
          <img
            className="cart-toast-image"
            src={resolveImageUrl(lastAdded.image_url)}
            alt={lastAdded.name}
          />
        ) : (
          <div className="cart-toast-image cart-toast-image--placeholder" />
        )}
        <div className="cart-toast-details">
          <p className="cart-toast-name">{lastAdded.name}</p>
          <p className="cart-toast-price">${lastAdded.price?.toFixed(2)}</p>
        </div>
      </div>

      <div className="cart-toast-footer">
        <span className="cart-toast-subtotal">
          Cart subtotal ({itemCount} item{itemCount === 1 ? "" : "s"})
        </span>
        <span className="cart-toast-subtotal-amount">${total.toFixed(2)}</span>
      </div>

      <div className="cart-toast-actions">
        <Link to="/cart" className="cart-toast-view-btn" onClick={() => setVisible(false)}>
          View cart
        </Link>
      </div>
    </div>
  );
}
