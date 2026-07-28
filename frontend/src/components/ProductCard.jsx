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

  // Add-to-cart interaction state: "idle" | "loading" | "added" | "error" | "out_of_stock"
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
      // addToCart now calls the real, authoritative /cart/add endpoint —
      // it validates stock and returns the true price server-side.
      // CartContext also guards against duplicate simultaneous requests
      // for the same product.
      await addToCart(product);
      setCartState("added");
      setTimeout(() => setCartState("idle"), 1800);
    } catch (err) {
      if (err.message === "Out of stock") {
        setCartState("out_of_stock");
      } else {
        setCartState("error");
        setTimeout(() => setCartState("idle"), 4000);
      }
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
    if (cartState === "out_of_stock") {
      return (
        <button className="product-card-btn product-card-btn--disabled" disabled>
          Out of stock
        </button>
      );
    }

    if (cartState === "loading") {
      return (
        <button className="product-card-btn product-card-btn--loading" disabled>
          <span className="product-card-spinner" aria-hidden="true" />
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
        <>
          <button className="product-card-btn product-card-btn--error" onClick={handleAddToCart}>
            Try again
          </button>
          <p className="product-card-error-message">
            Failed to add item. Please try again.
          </p>
        </>
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
