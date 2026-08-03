const API_BASE = import.meta.env?.VITE_API_BASE_URL || "http://127.0.0.1:8000";

export function resolveImageUrl(url) {
  if (!url || typeof url !== "string") return "";
  return url.startsWith("/") ? `${API_BASE}${url}` : url;
}

export function formatCurrency(value) {
  const number = Number(value);
  return Number.isFinite(number) ? `$${number.toFixed(2)}` : "";
}

function validPositiveNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : null;
}

export function getPromotionPresentation(product) {
  const promotion = product?.promotion;
  if (!promotion || typeof promotion !== "object") {
    return { hasPromotion: false, badgeText: null, originalPrice: null, salePrice: null };
  }

  const originalPrice = validPositiveNumber(product.price);
  const salePrice = validPositiveNumber(promotion.discounted_price);
  const explicitPercent = validPositiveNumber(promotion.discount_percent);
  const hasDistinctSalePrice = originalPrice !== null && salePrice !== null && salePrice < originalPrice;

  if (hasDistinctSalePrice) {
    const calculatedPercent = Math.round(((originalPrice - salePrice) / originalPrice) * 100);
    const percent = explicitPercent !== null ? Math.round(explicitPercent) : calculatedPercent;
    return {
      hasPromotion: true,
      badgeText: percent > 0 ? `${percent}% OFF` : "Sale",
      originalPrice,
      salePrice,
    };
  }

  if (explicitPercent !== null) {
    return {
      hasPromotion: true,
      badgeText: `${Math.round(explicitPercent)}% OFF`,
      originalPrice: null,
      salePrice: null,
    };
  }

  return { hasPromotion: true, badgeText: "Sale", originalPrice: null, salePrice: null };
}

export function getImagePresentation(product, imageFailed = false) {
  const name = product?.name || "this product";
  const imageUrl = imageFailed ? "" : resolveImageUrl(product?.image_url);
  if (imageUrl) {
    return {
      imageUrl,
      altText: name,
      showPlaceholder: false,
      placeholderAltText: `Image unavailable for ${name}`,
    };
  }
  return {
    imageUrl: "",
    altText: name,
    showPlaceholder: true,
    placeholderAltText: `Image unavailable for ${name}`,
  };
}
