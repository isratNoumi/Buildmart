import { Link } from "react-router-dom";

export default function ProductCard({ product }) {
  const price = product.price;
  return (
    <article className="product-card">
      <div className="product-image"><img src={product.image_url} alt={product.name} /></div>
      <p className="eyebrow">{product.brand.name}</p>
      <Link className="product-name" to={`/products/${product.id}`}>{product.name}</Link>
      <p className="rating">{product.rating.toFixed(1)} / 5 ({product.review_count})</p>
      {price ? <p className="price">${price.current_price.toFixed(2)}</p> : <p className="muted">Choose a location for pricing</p>}
      <p className="availability">{product.availability ? product.availability.status : "Select a store for availability"}</p>
    </article>
  );
}
