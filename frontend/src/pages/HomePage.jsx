import { Link } from "react-router-dom";
import ProductGrid from "../components/ProductGrid";
import "./HomePage.css";

export default function HomePage() {
  return (
    <div>
      <div className="home-hero">
        <div>
          <p className="home-hero-title">New season, new finds</p>
          <p className="home-hero-subtitle">Up to 30% off dresses and outerwear</p>
        </div>
        <Link to="/category/dresses" className="home-hero-btn">
          Shop now
        </Link>
      </div>

      <h2 className="home-section-title">Featured products</h2>
      <ProductGrid limit={12} />
    </div>
  );
}
