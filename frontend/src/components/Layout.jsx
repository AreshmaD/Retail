import { useState } from "react";
import { Link, Outlet, useNavigate, useLocation } from "react-router-dom";
import { useCart } from "../context/CartContext";
import { useSavedItems } from "../context/SavedItemsContext";
import { useChatWidget } from "../context/ChatWidgetContext";
import ChatWidget from "./ChatWidget";
import "./Layout.css";

const CATEGORIES = ["Dresses", "Shoes", "Outerwear", "Tops", "Bottoms", "Accessories"];

// Inline icon set matching the reference design (thin outline style, inherits color via currentColor)
function ChatIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z" />
    </svg>
  );
}

function HeartIcon({ filled }) {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill={filled ? "currentColor" : "none"} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.6l-1-1a5.5 5.5 0 0 0-7.8 7.8l1 1L12 21l7.8-7.6 1-1a5.5 5.5 0 0 0 0-7.8z" />
    </svg>
  );
}

function UserIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </svg>
  );
}

function CartIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="21" r="1" />
      <circle cx="19" cy="21" r="1" />
      <path d="M2.5 3h2l2.6 12.4a2 2 0 0 0 2 1.6h8.4a2 2 0 0 0 2-1.6L21 8H6" />
    </svg>
  );
}

export default function Layout() {
  const { itemCount } = useCart();
  const { count: savedCount } = useSavedItems();
  const { setIsOpen: setChatOpen } = useChatWidget();
  const [searchInput, setSearchInput] = useState("");
  const [accountOpen, setAccountOpen] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();

  function handleSearchSubmit(e) {
    e.preventDefault();
    const trimmed = searchInput.trim();
    if (trimmed) {
      navigate(`/search?q=${encodeURIComponent(trimmed)}`);
    }
  }

  function isCategoryActive(catPath) {
    return location.pathname === catPath;
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
          <button
            className="layout-help-btn"
            onClick={() => setChatOpen(true)}
            aria-label="Open shopping assistant"
          >
            <ChatIcon /> Need help?
          </button>

          <Link to="/saved" className="layout-icon-btn" aria-label="Saved items">
            <HeartIcon />
            {savedCount > 0 && <span className="layout-cart-badge">{savedCount}</span>}
          </Link>

          <div className="layout-account-wrap">
            <button
              className="layout-icon-btn"
              aria-label="Account"
              aria-expanded={accountOpen}
              onClick={() => setAccountOpen((o) => !o)}
            >
              <UserIcon />
            </button>
            {accountOpen && (
              <div className="layout-account-dropdown">
                <p className="layout-account-title">Your Scout account</p>
                <p className="layout-account-subtitle">
                  Sign in to track orders, save items, and get personalized help.
                </p>
                <button className="layout-account-signin-btn" disabled>
                  Sign in
                </button>
                <p className="layout-account-note">
                  (Account sign-in isn't connected yet — placeholder UI only.)
                </p>
              </div>
            )}
          </div>

          <Link to="/cart" className="layout-icon-btn layout-cart-btn" aria-label="Cart">
            <CartIcon />
            {itemCount > 0 && <span className="layout-cart-badge">{itemCount}</span>}
          </Link>
        </div>
      </header>

      <nav className="layout-nav">
        <Link
          to="/"
          className={`layout-nav-link ${isCategoryActive("/") ? "layout-nav-link--active" : ""}`}
        >
          All
        </Link>
        {CATEGORIES.map((cat) => {
          const path = `/category/${cat.toLowerCase()}`;
          return (
            <Link
              key={cat}
              to={path}
              className={`layout-nav-link ${isCategoryActive(path) ? "layout-nav-link--active" : ""}`}
            >
              {cat}
            </Link>
          );
        })}
      </nav>

      <main className="layout-content">
        <Outlet />
      </main>

      <ChatWidget />
    </div>
  );
}
