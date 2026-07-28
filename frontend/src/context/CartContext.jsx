import { createContext, useContext, useReducer, useEffect, useState } from "react";

const CartContext = createContext(null);
const STORAGE_KEY = "scout_cart";

function loadInitialState() {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) {
      return JSON.parse(saved);
    }
  } catch (e) {
    console.warn("Failed to load cart from localStorage:", e);
  }
  return { items: [] };
}

function cartReducer(state, action) {
  switch (action.type) {
    case "ADD_ITEM": {
      const existing = state.items.find((i) => i.product_id === action.product.product_id);
      if (existing) {
        return {
          items: state.items.map((i) =>
            i.product_id === action.product.product_id
              ? { ...i, quantity: i.quantity + 1 }
              : i
          ),
        };
      }
      return {
        items: [...state.items, { ...action.product, quantity: 1 }],
      };
    }
    case "REMOVE_ITEM":
      return {
        items: state.items.filter((i) => i.product_id !== action.productId),
      };
    case "UPDATE_QUANTITY":
      return {
        items: state.items
          .map((i) =>
            i.product_id === action.productId
              ? { ...i, quantity: Math.max(0, action.quantity) }
              : i
          )
          .filter((i) => i.quantity > 0),
      };
    case "CLEAR_CART":
      return { items: [] };
    default:
      return state;
  }
}

export function CartProvider({ children }) {
  const [state, dispatch] = useReducer(cartReducer, undefined, loadInitialState);

  // New: tracks the most recently added item so a toast/mini-cart can react to it.
  // notifyId increments on every add so the toast re-triggers even if the same
  // product is added twice in a row.
  const [lastAdded, setLastAdded] = useState(null);
  const [notifyId, setNotifyId] = useState(0);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      console.warn("Failed to save cart to localStorage:", e);
    }
  }, [state]);

  const addToCart = (product) => {
    dispatch({ type: "ADD_ITEM", product });
    setLastAdded(product);
    setNotifyId((n) => n + 1);
  };
  const removeFromCart = (productId) => dispatch({ type: "REMOVE_ITEM", productId });
  const updateQuantity = (productId, quantity) =>
    dispatch({ type: "UPDATE_QUANTITY", productId, quantity });
  const clearCart = () => dispatch({ type: "CLEAR_CART" });

  const itemCount = state.items.reduce((sum, i) => sum + i.quantity, 0);
  const total = state.items.reduce((sum, i) => sum + i.price * i.quantity, 0);

  const value = {
    items: state.items,
    addToCart,
    removeFromCart,
    updateQuantity,
    clearCart,
    itemCount,
    total,
    lastAdded,
    notifyId,
  };

  return <CartContext.Provider value={value}>{children}</CartContext.Provider>;
}

export function useCart() {
  const ctx = useContext(CartContext);
  if (!ctx) {
    throw new Error("useCart must be used within a CartProvider");
  }
  return ctx;
}
