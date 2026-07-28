import { createContext, useContext, useReducer, useEffect, useState, useRef } from "react";
import { addToCartRequest } from "../api/client";

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
      const existing = state.items.find((i) => i.product_id === action.item.product_id);
      if (existing) {
        return {
          items: state.items.map((i) =>
            i.product_id === action.item.product_id
              ? { ...i, quantity: i.quantity + action.item.quantity }
              : i
          ),
        };
      }
      return {
        items: [...state.items, action.item],
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

  const [lastAdded, setLastAdded] = useState(null);
  const [notifyId, setNotifyId] = useState(0);

  // The element that was focused (e.g. the "Add to cart" button clicked)
  // right before addToCart was called. Captured synchronously so the
  // CartToast can return keyboard focus there when it closes.
  const triggerElementRef = useRef(null);

  const pendingProductIds = useRef(new Set());

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (e) {
      console.warn("Failed to save cart to localStorage:", e);
    }
  }, [state]);

  const addToCart = async (product, quantity = 1) => {
    const productId = product.product_id;

    if (pendingProductIds.current.has(productId)) {
      return;
    }

    // Capture the currently focused element synchronously, before any
    // await — this is reliably the button the user just clicked.
    triggerElementRef.current = document.activeElement;

    pendingProductIds.current.add(productId);
    try {
      const result = await addToCartRequest(productId, quantity);

      if (!result.success) {
        throw new Error(result.error || "Failed to add item");
      }

      const item = {
        product_id: result.product_id,
        name: result.name,
        brand: product.brand,
        image_url: result.image_url,
        price: result.unit_price,
        promotion: result.promotion,
        quantity: result.quantity,
        // size is intentionally omitted — no variant-selection UI exists
        // yet, so we never invent one here.
      };

      dispatch({ type: "ADD_ITEM", item });
      setLastAdded(item);
      setNotifyId((n) => n + 1);
    } finally {
      pendingProductIds.current.delete(productId);
    }
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
    triggerElementRef,
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
