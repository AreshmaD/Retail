import { useEffect, useState } from "react";
import { useParams, Link } from "react-router-dom";
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import ProductCard from "../components/ProductCard";
import "./ProductDetailPage.css";

const API_BASE = import.meta.env.VITE_API_BASE_URL || "http://127.0.0.1:8000";

function resolveImageUrl(url) {
  if (!url) return url;
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

export default function ProductDetailPage() {
  const { productId } = useParams();
  const { addToCart } = useCart();
  const { toggleSave, isSaved } = useSavedItems();

  const [product, setProduct] = useState(null);
  const [similar, setSimilar] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    setLoading(true);
    Promise.all([
      fetch(`${API_BASE}/products/${productId}`).then((r) => r.json()),
      fetch(`${API_BASE}/products/${productId}/similar`).then((r) => r.json()),
    ])
      .then(([productData, similarData]) => {
        setProduct(productData.error ? null : productData);
        setSimilar(similarData);
      })
      .finally(() => setLoading(false));
  }, [productId]);

  if (loading) return <p>Loading…</p>;
  if (!product) return <p>Product not found. <Link to="/">Go back home</Link></p>;

  const saved = isSaved(product);

  return (
    <div>
      <Link to="/" className="product-detail-back">← Back</Link>

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
              <span>{product.rating.toFixed(1)}</span>
            </div>
          )}

          <p className="product-detail-price">${product.price.toFixed(2)}</p>
          <p className="product-detail-description">{product.description}</p>

          {product.tags && product.tags.length > 0 && (
            <div className="product-detail-tags">
              {product.tags.map((tag) => (
                <span key={tag} className="product-detail-tag">{tag}</span>
              ))}
            </div>
          )}

          <div className="product-detail-actions">
            <button className="product-detail-add-btn" onClick={() => addToCart(product)}>
              Add to cart
            </button>
            <button
              className={`product-detail-save-btn ${saved ? "product-detail-save-btn--active" : ""}`}
              onClick={() => toggleSave(product)}
            >
              {saved ? "♥ Saved" : "♡ Save"}
            </button>
          </div>
        </div>
      </div>

      {similar.length > 0 && (
        <div className="product-detail-similar-section">
          <h2 className="product-detail-similar-title">You might also like</h2>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 220px))",
              gap: "16px",
            }}
          >
            {similar.map((p) => (
              <ProductCard key={p.product_id} product={p} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
