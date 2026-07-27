import { useState } from "react";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import ChatWidget from "./ChatWidget";
import "./Layout.css";

const CATEGORIES = ["Dresses", "Shoes", "Outerwear", "Tops", "Bottoms", "Accessories"];

export default function Layout() {
  const { itemCount } = useCart();
  const { count: savedCount } = useSavedItems();
  const [searchInput, setSearchInput] = useState("");
  const navigate = useNavigate();

  function handleSearchSubmit(e) {
    e.preventDefault();
    const trimmed = searchInput.trim();
    if (trimmed) {
      navigate(`/search?q=${encodeURIComponent(trimmed)}`);
    }
  }

  return (
    <div className="layout">
      <header className="layout-header">
        <Link to="/" className="layout-logo">
          <span className="layout-logo-mark">S</span>
          <span className="layout-logo-text">Scout</span>
        </Link>

        <form className="layout-search" onSubmit={handleSearchSubmit}>
          <span className="layout-search-icon">⌕</span>
          <input
            placeholder="Search products…"
            value={searchInput}
            onChange={(e) => setSearchInput(e.target.value)}
          />
        </form>

        <div className="layout-header-actions">
          <Link to="/saved" className="layout-icon-btn" aria-label="Saved items">
            ♡
            {savedCount > 0 && <span className="layout-cart-badge">{savedCount}</span>}
          </Link>
          <button className="layout-icon-btn" aria-label="Account">👤</button>
          <Link to="/cart" className="layout-icon-btn layout-cart-btn" aria-label="Cart">
            🛒
            {itemCount > 0 && <span className="layout-cart-badge">{itemCount}</span>}
          </Link>
        </div>
      </header>

      <nav className="layout-nav">
        <Link to="/" className="layout-nav-link layout-nav-link--active">
          All
        </Link>
        {CATEGORIES.map((cat) => (
          <Link key={cat} to={`/category/${cat.toLowerCase()}`} className="layout-nav-link">
            {cat}
          </Link>
        ))}
      </nav>

      <main className="layout-content">
        <Outlet />
      </main>

      <ChatWidget />
    </div>
  );
}
