import { createElement } from "react";
import { Link } from "react-router-dom";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  // Relative paths (our own generated product images) need the backend
  // origin prefixed — absolute URLs (Picsum, external vendor images)
  // already point to the right place and should be left alone.
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import "./ProductCard.css";

export default function ProductCard({ product }) {
  const { addToCart } = useCart();
  const { toggleSave, isSaved } = useSavedItems();

  const isExternal = product.source === "external";
  const saved = isSaved(product);

  const resolvedClickUrl = product.click_url
    ? (product.click_url.startsWith("/") ? `${API_BASE}${product.click_url}` : product.click_url)
    : "#";

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

  return (
    <div className="product-card">
      <div className="product-card-image">
        {product.image_url ? (
          <img src={resolveImageUrl(product.image_url)} alt={product.name} />
        ) : (
          <div className="product-card-image-placeholder">No image</div>
        )}
        {isExternal && <span className="product-card-badge">Third-party</span>}

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
            <span className="product-card-promo-badge">
              {product.promotion.discount_percent}% off
            </span>
          </div>
        ) : (
          <p className="product-card-price">${product.price?.toFixed(2)}</p>
        )}

        {isExternal ? (
          externalLink
        ) : (
          <button className="product-card-btn" onClick={() => addToCart(product)}>
            Add to cart
          </button>
        )}
      </div>
    </div>
  );
}
