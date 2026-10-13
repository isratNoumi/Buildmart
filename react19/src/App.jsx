import { BrowserRouter, Routes, Route } from "react-router-dom";
import HomePage from "./pages/HomePage";
import Header from "./components/Header";
import { LocationProvider } from "./context/LocationContext";
import "./styles/global.css";

// Placeholders only: Products/ProductDetail/Login/Cart aren't ported yet, these exist so links don't dead-end.
function ProductsPlaceholder() {
  return <p className="muted">Product listing coming in a later phase.</p>;
}

function CategoryPlaceholder() {
  return <p className="muted">Category listing coming in a later phase.</p>;
}

function LoginPlaceholder() {
  return <p className="muted">Sign in coming in a later phase.</p>;
}

function CartPlaceholder() {
  return <p className="muted">Cart coming in a later phase.</p>;
}

function ProductDetailPlaceholder() {
  return <p className="muted">Product details coming in a later phase.</p>;
}

function NotFound() {
  return <p className="muted">Page not found.</p>;
}

export default function App() {
  return (
    <BrowserRouter>
      <LocationProvider>
        <Header />
        <main className="page-shell">
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/products" element={<ProductsPlaceholder />} />
            <Route path="/products/:id" element={<ProductDetailPlaceholder />} />
            <Route path="/category/:slug" element={<CategoryPlaceholder />} />
            <Route path="/login" element={<LoginPlaceholder />} />
            <Route path="/cart" element={<CartPlaceholder />} />
            <Route path="*" element={<NotFound />} />
          </Routes>
        </main>
      </LocationProvider>
    </BrowserRouter>
  );
}
