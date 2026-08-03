import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import ProductCard from "../components/ProductCard";
import { formatCurrency, getPromotionPresentation } from "../components/ProductCard.helpers";
import "./ProductDetailPage.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

const VARIANT_OPTIONS = {
  dresses: {
    sizes: ["S", "M", "L"],
    colors: ["Black", "Floral", "Navy"],
  },
  tops: {
    sizes: ["S", "M", "L"],
    colors: ["White", "Black", "Gray"],
  },
  bottoms: {
    sizes: ["S", "M", "L"],
    colors: ["Blue", "Black", "Khaki"],
  },
  shoes: {
    sizes: ["8", "9", "10"],
    colors: ["White", "Black", "Brown"],
  },
  outerwear: {
    sizes: ["S", "M", "L"],
    colors: ["Blue", "Black", "Olive"],
  },
  accessories: {
    sizes: ["One Size"],
    colors: ["Brown", "Black"],
  },
};

function getInitialVariants(product) {
  const options = VARIANT_OPTIONS[product.category] || { sizes: [], colors: [] };
  return {
    size: options.sizes[0] || "",
    color: options.colors.find((color) => product.tags?.includes(color.toLowerCase())) || options.colors[0] || "",
  };
}

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

function titleCase(value) {
  return value ? value.charAt(0).toUpperCase() + value.slice(1) : "";
}

function getDetailBullets(product) {
  const bullets = [];
  if (product.description) bullets.push(product.description);
  if (product.category) bullets.push(`Category: ${titleCase(product.category)}`);
  if (product.tags?.length) bullets.push(`Style notes: ${product.tags.slice(0, 3).join(", ")}`);
  return bullets;
}

export default function ProductDetailPage() {
  const { productId } = useParams();
  const { addToCart } = useCart();
  const { toggleSave, isSaved } = useSavedItems();

  const [product, setProduct] = useState(null);
  const [similar, setSimilar] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedSize, setSelectedSize] = useState("");
  const [selectedColor, setSelectedColor] = useState("");
  const [quantity, setQuantity] = useState(1);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLoading(true);
    Promise.all([
      fetch(`${API_BASE}/products/${productId}`).then((r) => r.json()),
      fetch(`${API_BASE}/products/${productId}/similar`).then((r) => r.json()),
    ])
      .then(([productData, similarData]) => {
        const nextProduct = productData.error ? null : productData;
        setProduct(nextProduct);
        if (nextProduct) {
          const initialVariants = getInitialVariants(nextProduct);
          setSelectedSize(initialVariants.size);
          setSelectedColor(initialVariants.color);
        }
        setSimilar(similarData);
      })
      .finally(() => setLoading(false));
  }, [productId]);

  if (loading) return <p>Loading…</p>;
  if (!product) return <p>Product not found. <Link to="/">Go back home</Link></p>;

  const saved = isSaved(product);
  const variantOptions = VARIANT_OPTIONS[product.category] || { sizes: [], colors: [] };
  const promotionPresentation = getPromotionPresentation(product);
  const detailBullets = getDetailBullets(product);

  return (
    <div className="product-detail-page">
      <nav className="product-detail-breadcrumbs" aria-label="Breadcrumb">
        <Link to="/">Home</Link>
        {product.category && (
          <>
            <span>/</span>
            <Link to={`/category/${product.category}`}>{titleCase(product.category)}</Link>
          </>
        )}
        <span>/</span>
        <span>{product.name}</span>
      </nav>

      <div className="product-detail">
        <div className="product-detail-image">
          <img src={resolveImageUrl(product.image_url)} alt={product.name} />
        </div>

        <div className="product-detail-info">
          <p className="product-detail-brand">{product.brand}</p>
          <h1 className="product-detail-name">{product.name}</h1>

          {typeof product.rating === "number" && product.rating > 0 && (
            <div className="product-detail-rating">
              <span className="product-detail-star">★</span>
              <span>{product.rating.toFixed(1)} rating</span>
            </div>
          )}

          <p className="product-detail-description">{product.description}</p>

          <div className="product-detail-buy-box">
            <div className="product-detail-price-block">
              {promotionPresentation.hasPromotion && promotionPresentation.salePrice ? (
                <>
                  <div className="product-detail-sale-row">
                    <span className="product-detail-price product-detail-price--sale">
                      {formatCurrency(promotionPresentation.salePrice)}
                    </span>
                    <span className="product-detail-promo-badge">
                      {promotionPresentation.badgeText}
                    </span>
                  </div>
                  <p className="product-detail-original-price">
                    Was {formatCurrency(promotionPresentation.originalPrice)}
                  </p>
                </>
              ) : (
                <p className="product-detail-price">{formatCurrency(product.price)}</p>
              )}
            </div>

            {(variantOptions.sizes.length > 0 || variantOptions.colors.length > 0) && (
              <div className="product-detail-variants" aria-label="Available product options">
                {variantOptions.sizes.length > 0 && (
                  <div className="product-detail-variant-group">
                    <div className="product-detail-variant-label">
                      <span>Size</span>
                      <span>{selectedSize}</span>
                    </div>
                    <div className="product-detail-variant-options">
                      {variantOptions.sizes.map((size) => (
                        <button
                          key={size}
                          type="button"
                          className={`product-detail-variant-chip ${size === selectedSize ? "product-detail-variant-chip--selected" : ""}`}
                          onClick={() => setSelectedSize(size)}
                          aria-pressed={size === selectedSize}
                        >
                          {size}
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {variantOptions.colors.length > 0 && (
                  <div className="product-detail-variant-group">
                    <div className="product-detail-variant-label">
                      <span>Color</span>
                      <span>{selectedColor}</span>
                    </div>
                    <div className="product-detail-variant-options">
                      {variantOptions.colors.map((color) => (
                        <button
                          key={color}
                          type="button"
                          className={`product-detail-variant-chip ${color === selectedColor ? "product-detail-variant-chip--selected" : ""}`}
                          onClick={() => setSelectedColor(color)}
                          aria-pressed={color === selectedColor}
                        >
                          {color}
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div className="product-detail-quantity-row">
              <span>Quantity</span>
              <div className="product-detail-quantity-control" aria-label="Quantity">
                <button
                  type="button"
                  onClick={() => setQuantity((value) => Math.max(1, value - 1))}
                  aria-label="Decrease quantity"
                >
                  −
                </button>
                <span>{quantity}</span>
                <button
                  type="button"
                  onClick={() => setQuantity((value) => Math.min(9, value + 1))}
                  aria-label="Increase quantity"
                >
                  +
                </button>
              </div>
            </div>

            <div className="product-detail-actions">
              <button className="product-detail-add-btn" onClick={() => addToCart(product, quantity)}>
                Add to cart
              </button>
              <button
                className={`product-detail-save-btn ${saved ? "product-detail-save-btn--active" : ""}`}
                onClick={() => toggleSave(product)}
              >
                {saved ? "♥ Saved" : "♡ Save"}
              </button>
            </div>

            <div className="product-detail-trust">
              <span>Free shipping over $75</span>
              <span>30-day returns</span>
              <span>Secure checkout</span>
            </div>
          </div>

          <div className="product-detail-fulfillment" aria-label="Fulfillment options">
            <div className="product-detail-fulfillment-card">
              <strong>Shipping</strong>
              <span>Free over $75</span>
            </div>
            <div className="product-detail-fulfillment-card">
              <strong>Pickup</strong>
              <span>Ask Scout to check store stock</span>
            </div>
            <div className="product-detail-fulfillment-card">
              <strong>Delivery</strong>
              <span>Availability verified before checkout</span>
            </div>
          </div>

          {product.tags && product.tags.length > 0 && (
            <div className="product-detail-tags">
              {product.tags.map((tag) => (
                <span key={tag} className="product-detail-tag">{tag}</span>
              ))}
            </div>
          )}

          {detailBullets.length > 0 && (
            <section className="product-detail-details" aria-labelledby="product-details-heading">
              <h2 id="product-details-heading">Product details</h2>
              <ul>
                {detailBullets.map((detail) => (
                  <li key={detail}>{detail}</li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>

      {similar.length > 0 && (
        <div className="product-detail-similar-section">
          <h2 className="product-detail-similar-title">You might also like</h2>
          <div className="product-detail-similar-grid">
            {similar.map((p) => (
              <ProductCard key={p.product_id} product={p} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
