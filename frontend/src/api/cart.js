import { apiRequest } from "./client";

function sessionId() {
  let value = localStorage.getItem("build-mart-session");
  if (!value) {
    value = `session-${Date.now()}`;
    localStorage.setItem("build-mart-session", value);
  }
  return value;
}

export function getCart(location) {
  const query = new URLSearchParams({ session_id: sessionId() });
  if (location) {
    query.set("region_id", location.region.id);
    query.set("store_id", location.store.id);
    query.set("zip_code", location.zip_code);
  }
  return apiRequest(`/cart?${query.toString()}`);
}

export function addCartItem(item) {
  return apiRequest(`/cart/items?session_id=${encodeURIComponent(sessionId())}`, {
    method: "POST",
    body: JSON.stringify(item)
  });
}

export function updateCartItem(id, item) {
  return apiRequest(`/cart/items/${id}?session_id=${encodeURIComponent(sessionId())}`, {
    method: "PATCH",
    body: JSON.stringify(item)
  });
}

export function removeCartItem(id) {
  return apiRequest(`/cart/items/${id}?session_id=${encodeURIComponent(sessionId())}`, {
    method: "DELETE"
  });
}