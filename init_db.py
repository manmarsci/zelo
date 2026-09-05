"""Initialize MotherDuck database with schema and seed data."""
import os
import duckdb
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv()

TOKEN = os.getenv("MOTHERDUCK_TOKEN")
DB = os.getenv("MOTHERDUCK_DATABASE", "zelo_boutique")
conn = duckdb.connect(f"md:{DB}?motherduck_token={TOKEN}")

SCHEMA = """
-- Create sequences for auto-incrementing IDs
CREATE SEQUENCE IF NOT EXISTS users_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS addresses_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS categories_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS brands_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS products_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS product_images_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS orders_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS order_items_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS coupons_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS cart_id_seq START 1;

-- Users (customers + admin)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER DEFAULT nextval('users_id_seq') PRIMARY KEY,
    name VARCHAR NOT NULL,
    email VARCHAR UNIQUE NOT NULL,
    password_hash VARCHAR NOT NULL,
    phone VARCHAR,
    role VARCHAR DEFAULT 'customer',
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- Saved addresses
CREATE TABLE IF NOT EXISTS addresses (
    id INTEGER DEFAULT nextval('addresses_id_seq') PRIMARY KEY,
    user_id INTEGER NOT NULL,
    full_name VARCHAR NOT NULL,
    phone VARCHAR NOT NULL,
    address VARCHAR NOT NULL,
    city VARCHAR NOT NULL,
    province VARCHAR NOT NULL,
    postal_code VARCHAR,
    is_default BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- Categories
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER DEFAULT nextval('categories_id_seq') PRIMARY KEY,
    name VARCHAR NOT NULL,
    slug VARCHAR UNIQUE NOT NULL,
    parent_id INTEGER,
    sort_order INTEGER DEFAULT 0
);

-- Brands
CREATE TABLE IF NOT EXISTS brands (
    id INTEGER DEFAULT nextval('brands_id_seq') PRIMARY KEY,
    name VARCHAR NOT NULL,
    slug VARCHAR UNIQUE NOT NULL
);

-- Products
CREATE TABLE IF NOT EXISTS products (
    id INTEGER DEFAULT nextval('products_id_seq') PRIMARY KEY,
    name VARCHAR NOT NULL,
    slug VARCHAR UNIQUE NOT NULL,
    description TEXT,
    fabric VARCHAR,
    brand_id INTEGER,
    category_id INTEGER,
    original_price DECIMAL(10,2) NOT NULL,
    sale_price DECIMAL(10,2),
    stock INTEGER DEFAULT 0,
    sizes VARCHAR,
    colors VARCHAR,
    is_new_arrival BOOLEAN DEFAULT FALSE,
    is_popular BOOLEAN DEFAULT FALSE,
    is_sale BOOLEAN DEFAULT FALSE,
    is_active BOOLEAN DEFAULT TRUE,
    views INTEGER DEFAULT 0,
    sold INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- Product images
CREATE TABLE IF NOT EXISTS product_images (
    id INTEGER DEFAULT nextval('product_images_id_seq') PRIMARY KEY,
    product_id INTEGER NOT NULL,
    image_url VARCHAR NOT NULL,
    sort_order INTEGER DEFAULT 0
);

-- Orders
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER DEFAULT nextval('orders_id_seq') PRIMARY KEY,
    order_number VARCHAR UNIQUE NOT NULL,
    user_id INTEGER,
    guest_name VARCHAR,
    guest_email VARCHAR,
    guest_phone VARCHAR,
    shipping_name VARCHAR NOT NULL,
    shipping_phone VARCHAR NOT NULL,
    shipping_address VARCHAR NOT NULL,
    shipping_city VARCHAR NOT NULL,
    shipping_province VARCHAR NOT NULL,
    shipping_postal VARCHAR,
    subtotal DECIMAL(10,2) NOT NULL,
    delivery_charges DECIMAL(10,2) DEFAULT 0,
    discount DECIMAL(10,2) DEFAULT 0,
    total DECIMAL(10,2) NOT NULL,
    status VARCHAR DEFAULT 'Pending',
    payment_method VARCHAR DEFAULT 'COD',
    coupon_code VARCHAR,
    notes TEXT,
    created_at TIMESTAMP DEFAULT current_timestamp
);

-- Order items
CREATE TABLE IF NOT EXISTS order_items (
    id INTEGER DEFAULT nextval('order_items_id_seq') PRIMARY KEY,
    order_id INTEGER NOT NULL,
    product_id INTEGER NOT NULL,
    product_name VARCHAR NOT NULL,
    quantity INTEGER NOT NULL,
    price DECIMAL(10,2) NOT NULL,
    size VARCHAR,
    color VARCHAR,
    image_url VARCHAR
);

-- Coupons
CREATE TABLE IF NOT EXISTS coupons (
    id INTEGER DEFAULT nextval('coupons_id_seq') PRIMARY KEY,
    code VARCHAR UNIQUE NOT NULL,
    discount_type VARCHAR DEFAULT 'percent',
    discount_value DECIMAL(10,2) NOT NULL,
    min_order DECIMAL(10,2) DEFAULT 0,
    max_uses INTEGER DEFAULT 100,
    used_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT TRUE,
    expires_at TIMESTAMP
);

-- Cart
CREATE TABLE IF NOT EXISTS cart (
    id INTEGER DEFAULT nextval('cart_id_seq') PRIMARY KEY,
    session_id VARCHAR,
    user_id INTEGER,
    product_id INTEGER NOT NULL,
    quantity INTEGER DEFAULT 1,
    size VARCHAR,
    color VARCHAR
);
"""

# Execute schema
for stmt in SCHEMA.strip().split(";"):
    stmt = stmt.strip()
    if stmt:
        conn.execute(stmt + ";")

print("✅ Schema created.")

# ---------- Seed data ----------

# Admin user
admin_email = os.getenv("ADMIN_EMAIL", "admin@zelolive.com")
admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
existing = conn.execute("SELECT id FROM users WHERE email = ?", [admin_email]).fetchone()
if not existing:
    conn.execute(
        "INSERT INTO users (name, email, password_hash, role) VALUES (?, ?, ?, 'admin')",
        ["Admin", admin_email, generate_password_hash(admin_pass, method='pbkdf2:sha256')]
    )
    print(f"✅ Admin created: {admin_email} / {admin_pass}")

# Categories
categories = [
    ("Ladies", "ladies", None, 1),
    ("Girls", "girls", None, 2),
    ("Unstitched", "unstitched", 1, 1),
    ("Ready to Wear", "ready-to-wear", 1, 2),
    ("Luxury Pret", "luxury-pret", 1, 3),
    ("Frocks", "frocks", 2, 1),
    ("Dresses", "dresses", 2, 2),
]
for c in categories:
    try:
        conn.execute("INSERT INTO categories (name, slug, parent_id, sort_order) VALUES (?, ?, ?, ?)", c)
    except Exception:
        pass

# Brands
brands = [
    ("Khaadi", "khaadi"),
    ("Sana Safinaz", "sana-safinaz"),
    ("Maria B", "maria-b"),
    ("Zara", "zara"),
    ("Sapphire", "sapphire"),
    ("Limelight", "limelight"),
    ("Outfitters", "outfitters"),
]
for b in brands:
    try:
        conn.execute("INSERT INTO brands (name, slug) VALUES (?, ?)", b)
    except Exception:
        pass

# Sample products
products = [
    ("Embroidered Lawn 3-Piece", "embroidered-lawn-3-piece",
     "Elegant embroidered lawn suit with chiffon dupatta. Brand leftover — limited stock.",
     "Lawn / Chiffon", 1, 3, 4500.00, 2999.00, 25,
     '["Unstitched"]', '["Ivory","Pink","Mint"]',
     True, True, True, True, 120, 45),
    ("Printed Cambric Suit", "printed-cambric-suit",
     "Soft printed cambric with digital prints. Surplus stock from last season.",
     "Cambric", 5, 3, 3200.00, 1999.00, 40,
     '["Unstitched"]', '["Blue","Peach"]',
     True, False, True, True, 85, 30),
    ("Silk Formal Dress", "silk-formal-dress",
     "Luxurious silk formal dress with intricate handwork. Perfect for weddings.",
     "Silk", 2, 5, 12000.00, 7500.00, 8,
     '["S","M","L","XL"]', '["Maroon","Navy"]',
     False, True, True, True, 60, 15),
    ("Casual Kurta", "casual-kurta",
     "Comfortable cotton kurta for everyday wear. Brand overstock.",
     "Cotton", 6, 4, 2500.00, 1499.00, 60,
     '["S","M","L","XL"]', '["White","Black","Beige"]',
     True, False, False, True, 200, 80),
    ("Girls Frocks Set", "girls-frocks-set",
     "Adorable frock set for girls aged 4-10 years. Soft fabric with pretty details.",
     "Cotton Blend", 7, 6, 2800.00, 1799.00, 30,
     '["4-5Y","6-7Y","8-9Y","10-11Y"]', '["Pink","Yellow","Sky Blue"]',
     True, True, True, True, 95, 40),
    ("Girls Party Dress", "girls-party-dress",
     "Beautiful party dress for special occasions. Premium quality leftover.",
     "Organza / Net", 4, 7, 4500.00, 2999.00, 15,
     '["4-5Y","6-7Y","8-9Y"]', '["Rose","Lavender"]',
     False, True, True, True, 70, 22),
    ("Linen Summer Collection", "linen-summer-collection",
     "Breathable linen 2-piece for summer. Leftover stock — great value.",
     "Linen", 1, 4, 3800.00, 2499.00, 35,
     '["S","M","L","XL"]', '["Olive","Tan","Cream"]',
     True, False, True, True, 150, 55),
    ("Chiffon Dupatta", "chiffon-dupatta",
     "Heavy embroidered chiffon dupatta. Can be paired with any outfit.",
     "Chiffon", 3, 3, 2200.00, 1299.00, 50,
     '["Free Size"]', '["Red","Green","Gold"]',
     False, False, True, True, 110, 38),
]

for p in products:
    try:
        conn.execute(
            """INSERT INTO products
               (name, slug, description, fabric, brand_id, category_id,
                original_price, sale_price, stock, sizes, colors,
                is_new_arrival, is_popular, is_sale, is_active, views, sold)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            p
        )
    except Exception as e:
        print(f"Skip product: {e}")

# Product images
images = [
    (1, "https://images.unsplash.com/photo-1583391733956-6c78276477e2?w=800", 1),
    (2, "https://images.unsplash.com/photo-1583391733981-8498408d4b44?w=800", 1),
    (3, "https://images.unsplash.com/photo-1594938298603-c8148c4dae35?w=800", 1),
    (4, "https://images.unsplash.com/photo-1617059062444-b8b2b4e7273a?w=800", 1),
    (5, "https://images.unsplash.com/photo-1622290291468-a28f7a7dc6a8?w=800", 1),
    (6, "https://images.unsplash.com/photo-1518831959646-742c3a14ebf7?w=800", 1),
    (7, "https://images.unsplash.com/photo-1591369822096-ffd140ec948f?w=800", 1),
    (8, "https://images.unsplash.com/photo-1617627143750-d86bc21e42bb?w=800", 1),
]
for img in images:
    try:
        conn.execute("INSERT INTO product_images (product_id, image_url, sort_order) VALUES (?, ?, ?)", img)
    except Exception:
        pass

# Sample coupon
try:
    conn.execute(
        """INSERT INTO coupons (code, discount_type, discount_value, min_order, max_uses)
           VALUES ('WELCOME10', 'percent', 10, 2000, 100)"""
    )
except Exception:
    pass

print("✅ Seed data loaded.")
print(f"\n🎉 Database ready at md:{DB}")
print(f"🔐 Admin login: {admin_email} / {admin_pass}")