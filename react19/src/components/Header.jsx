import { Link } from "react-router-dom";
import LocationSelector from "./LocationSelector";

// Auth/cart aren't ported yet, so these are static placeholders rather than live state.
export default function Header() {
  return (
    <>
      <header className="site-header">
        <Link className="brand" to="/">BUILD MART</Link>
        <form className="search-form" action="/products">
          <input name="search" placeholder="What can we help you find?" />
          <button type="submit" aria-label="Search">Search</button>
        </form>
        <div className="header-actions">
          <LocationSelector />
          <Link to="/login">Sign in</Link>
          <Link to="/cart">Cart (0)</Link>
        </div>
      </header>
      <nav className="category-nav"><Link to="/products">Shop All</Link><Link to="/category/power-tools">Power Tools</Link><Link to="/category/hand-tools">Hand Tools</Link><Link to="/category/hardware">Hardware</Link><Link to="/category/paint">Paint</Link><Link to="/category/lawn-garden">Lawn &amp; Garden</Link></nav>
    </>
  );
}
