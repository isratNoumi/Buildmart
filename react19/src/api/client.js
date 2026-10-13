const API_ROOT = "/api";

export async function apiRequest(path) {
  const response = await fetch(`${API_ROOT}${path}`);
  if (!response.ok) throw new Error(`Request to ${path} failed with status ${response.status}`);
  return response.json();
}
