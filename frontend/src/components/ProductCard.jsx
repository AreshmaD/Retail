import { createElement, useState } from "react";
import { Link } from "react-router-dom";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import "./ProductCard.css";

export default function ProductCard({ product }) {
  const { addToCart } = useCart();
  const { toggleSave, isSaved } = useSavedItems();

  // Add-to-cart interaction state: "idle" | "loading" | "added" | "error"
  const [cartState, setCartState] = useState("idle");

  const isExternal = product.source === "external";
  const saved = isSaved(product);

  const resolvedClickUrl = product.click_url
    ? (product.click_url.startsWith("/") ? `${API_BASE}${product.click_url}` : product.click_url)
    : "#";

  async function handleAddToCart() {
    if (cartState === "loading") return;
    setCartState("loading");
    try {
      // addToCart is synchronous today (local cart, no API call yet).
      // Short simulated delay purely so the loading state is visible.
      await new Promise((resolve) => setTimeout(resolve, 450));
      addToCart(product);
      setCartState("added");
      setTimeout(() => setCartState("idle"), 1800);
    } catch (err) {
      setCartState("error");
      setTimeout(() => setCartState("idle"), 2200);
    }
  }

  const externalLink = createElement(
    "div",
    { className: "product-card-external-wrap" },
    createElement(
      "a",
      {
        className: "product-card-btn product-card-btn--external",
        href: resolvedClickUrl,
        target: "_blank",
        rel: "noopener noreferrer sponsored",
      },
      `View at ${product.vendor_name}`
    ),
    createElement(
      "p",
      { className: "product-card-affiliate-disclosure" },
      "Affiliate link — we may earn a commission"
    )
  );

  function renderCartButton() {
    if (cartState === "loading") {
      return (
        <button className="product-card-btn product-card-btn--loading" disabled>
          Adding…
        </button>
      );
    }

    if (cartState === "added") {
      return (
        <button className="product-card-btn product-card-btn--added" disabled>
          ✓ Added to cart
        </button>
      );
    }

    if (cartState === "error") {
      return (
        <button className="product-card-btn product-card-btn--error" onClick={handleAddToCart}>
          Failed. Try again
        </button>
      );
    }

    return (
      <button className="product-card-btn" onClick={handleAddToCart}>
        Add to cart
      </button>
    );
  }

  return (
    <div className="product-card">
      <div className="product-card-image">
        {product.image_url ? (
          <img src={resolveImageUrl(product.image_url)} alt={product.name} />
        ) : (
          <div className="product-card-image-placeholder">No image</div>
        )}

        {/* Sale badge — only rendered when a real promotion exists, never invented */}
        {product.promotion && (
          <span className="product-card-badge product-card-badge--sale">
            {product.promotion.discount_percent}% OFF
          </span>
        )}
        {isExternal && !product.promotion && (
          <span className="product-card-badge">Third-party</span>
        )}

        <button
          className={`product-card-save ${saved ? "product-card-save--active" : ""}`}
          onClick={() => toggleSave(product)}
          aria-label={saved ? "Remove from saved items" : "Save item"}
        >
          {saved ? "♥" : "♡"}
        </button>
      </div>

      <div className="product-card-body">
        <p className="product-card-brand">{product.brand || product.vendor_name}</p>

        {product.product_id ? (
          <Link to={`/product/${product.product_id}`} className="product-card-name-link">
            <p className="product-card-name">{product.name}</p>
          </Link>
        ) : (
          <p className="product-card-name">{product.name}</p>
        )}

        {/* Rating shown only when a real value exists. No review count field
            exists in the backend response, so we never show one. */}
        {typeof product.rating === "number" && product.rating > 0 && (
          <div className="product-card-rating">
            <span className="product-card-star">★</span>
            <span>{product.rating.toFixed(1)}</span>
          </div>
        )}

        {product.promotion ? (
          <div className="product-card-price-row">
            <span className="product-card-price product-card-price--original">
              ${product.price?.toFixed(2)}
            </span>
            <span className="product-card-price product-card-price--sale">
              ${product.promotion.discounted_price?.toFixed(2)}
            </span>
          </div>
        ) : (
          <p className="product-card-price">${product.price?.toFixed(2)}</p>
        )}

        {/* No stock/pickup endpoint currently exposes this data for listed
            products, so per spec we show the honest "not checked" state
            rather than inventing an in-stock or pickup claim. */}
        {!isExternal && (
          <p className="product-card-availability">
            <span className="product-card-availability-dot" />
            Availability not checked
          </p>
        )}

        <div className="product-card-action">
          {isExternal ? externalLink : renderCartButton()}
        </div>
      </div>
    </div>
  );
}
