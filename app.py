"""ZELO LIVE BOUTIQUE — Flask backend with MotherDuck."""
import os
import json
import uuid
import secrets
from datetime import datetime
from functools import wraps

import duckdb
from flask import (Flask, render_template, request, redirect, url_for,
                   session, flash, jsonify, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", secrets.token_hex(32))
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB
app.config["UPLOAD_FOLDER"] = "static/uploads"

DELIVERY_CHARGES = float(os.getenv("DELIVERY_CHARGES", 150))
FREE_DELIVERY_ABOVE = float(os.getenv("FREE_DELIVERY_ABOVE", 3000))

os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)


# ---------- Database ----------
def get_db():
    token = os.getenv("MOTHERDUCK_TOKEN")
    db = os.getenv("MOTHERDUCK_DATABASE", "zelo_boutique")
    conn = duckdb.connect(f"md:{db}?motherduck_token={token}")
    return conn


def query(sql, params=None, fetch=True):
    conn = get_db()
    try:
        cur = conn.execute(sql, params or [])
        if fetch:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            return rows
        return None
    finally:
        conn.close()


def query_one(sql, params=None):
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql, params=None):
    conn = get_db()
    try:
        conn.execute(sql, params or [])
    finally:
        conn.close()


# ---------- Helpers ----------
def slugify(text):
    import re
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_-]+", "-", text)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            abort(403)
        return f(*args, **kwargs)
    return wrapper


def get_cart_count():
    if "user_id" in session:
        r = query_one("SELECT COALESCE(SUM(quantity),0) as c FROM cart WHERE user_id = ?",
                      [session["user_id"]])
    else:
        sid = session.get("cart_session")
        if not sid:
            return 0
        r = query_one("SELECT COALESCE(SUM(quantity),0) as c FROM cart WHERE session_id = ?",
                      [sid])
    return int(r["c"]) if r else 0


def get_cart_items():
    if "user_id" in session:
        return query("""
            SELECT c.id as cart_id, c.quantity, c.size, c.color,
                   p.id, p.name, p.slug, p.sale_price, p.original_price, p.stock,
                   (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
            FROM cart c JOIN products p ON c.product_id = p.id
            WHERE c.user_id = ?
        """, [session["user_id"]])
    sid = session.get("cart_session")
    if not sid:
        return []
    return query("""
        SELECT c.id as cart_id, c.quantity, c.size, c.color,
               p.id, p.name, p.slug, p.sale_price, p.original_price, p.stock,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM cart c JOIN products p ON c.product_id = p.id
        WHERE c.session_id = ?
    """, [sid])


@app.context_processor
def inject_globals():
    return {
        "cart_count": get_cart_count(),
        "current_user": query_one("SELECT * FROM users WHERE id = ?",
                                  [session["user_id"]]) if "user_id" in session else None,
        "categories": query("SELECT * FROM categories WHERE parent_id IS NULL ORDER BY sort_order"),
    }


# ---------- Public routes ----------
@app.route("/")
def index():
    new_arrivals = query("""
        SELECT p.*, b.name as brand_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p LEFT JOIN brands b ON p.brand_id=b.id
        WHERE p.is_active AND p.is_new_arrival ORDER BY p.created_at DESC LIMIT 8
    """)
    popular = query("""
        SELECT p.*, b.name as brand_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p LEFT JOIN brands b ON p.brand_id=b.id
        WHERE p.is_active AND p.is_popular ORDER BY p.sold DESC LIMIT 8
    """)
    sale = query("""
        SELECT p.*, b.name as brand_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p LEFT JOIN brands b ON p.brand_id=b.id
        WHERE p.is_active AND p.is_sale ORDER BY p.created_at DESC LIMIT 8
    """)
    return render_template("index.html",
                           new_arrivals=new_arrivals, popular=popular, sale=sale)


@app.route("/products")
def products():
    category = request.args.get("category")
    brand = request.args.get("brand")
    search = request.args.get("q", "").strip()
    sort = request.args.get("sort", "newest")
    size = request.args.get("size")
    avail = request.args.get("avail")  # in_stock
    max_price = request.args.get("max_price")

    where = ["p.is_active = TRUE"]
    params = []

    if category:
        where.append("(c.slug = ? OR c2.slug = ?)")
        params.extend([category, category])
    if brand:
        where.append("b.slug = ?")
        params.append(brand)
    if search:
        where.append("(p.name ILIKE ? OR p.description ILIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if size:
        where.append("p.sizes ILIKE ?")
        params.append(f'%"{size}"%')
    if avail == "in_stock":
        where.append("p.stock > 0")
    if max_price:
        where.append("COALESCE(p.sale_price, p.original_price) <= ?")
        params.append(float(max_price))

    order_map = {
        "newest": "p.created_at DESC",
        "price_low": "COALESCE(p.sale_price, p.original_price) ASC",
        "price_high": "COALESCE(p.sale_price, p.original_price) DESC",
        "popular": "p.views DESC",
        "bestselling": "p.sold DESC",
        "discount": "(p.original_price - COALESCE(p.sale_price, p.original_price)) DESC",
    }
    order = order_map.get(sort, "p.created_at DESC")

    sql = f"""
        SELECT p.*, b.name as brand_name, c.name as category_name, c.slug as category_slug,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        LEFT JOIN categories c2 ON c.parent_id=c2.id
        WHERE {' AND '.join(where)}
        ORDER BY {order}
    """
    items = query(sql, params)

    brands_list = query("SELECT * FROM brands ORDER BY name")
    cats_list = query("SELECT * FROM categories ORDER BY sort_order")

    return render_template("products.html", products=items, brands=brands_list,
                           categories=cats_list, current_category=category,
                           current_brand=brand, current_sort=sort, search=search)


@app.route("/product/<slug>")
def product_detail(slug):
    p = query_one("""
        SELECT p.*, b.name as brand_name, c.name as category_name
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE p.slug = ? AND p.is_active
    """, [slug])
    if not p:
        abort(404)
    
    # Parse JSON strings into Python lists for the template
    try:
        p['sizes_list'] = json.loads(p['sizes']) if p['sizes'] else []
    except Exception:
        p['sizes_list'] = []
        
    try:
        p['colors_list'] = json.loads(p['colors']) if p['colors'] else []
    except Exception:
        p['colors_list'] = []

        # Parse features JSON
    try:
        p['features_list'] = json.loads(p['features']) if p['features'] else []
    except Exception:
        p['features_list'] = []

    # Track view
    execute("UPDATE products SET views = views + 1 WHERE id = ?", [p["id"]])
    images = query("SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order",
                   [p["id"]])
    related = query("""
        SELECT p.*, b.name as brand_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p LEFT JOIN brands b ON p.brand_id=b.id
        WHERE p.category_id = ? AND p.id != ? AND p.is_active
        ORDER BY p.created_at DESC LIMIT 4
    """, [p["category_id"], p["id"]])
    return render_template("product.html", product=p, images=images, related=related)


# ---------- Cart ----------
@app.route("/cart/add", methods=["POST"])
def cart_add():
    pid = int(request.form["product_id"])
    qty = int(request.form.get("quantity", 1))
    size = request.form.get("size", "")
    color = request.form.get("color", "")

    p = query_one("SELECT stock FROM products WHERE id = ?", [pid])
    if not p:
        flash("Product not found.", "error")
        return redirect(request.referrer or url_for("index"))

    if "user_id" in session:
        existing = query_one(
            "SELECT id, quantity FROM cart WHERE user_id=? AND product_id=? AND size=? AND color=?",
            [session["user_id"], pid, size, color])
        if existing:
            execute("UPDATE cart SET quantity = quantity + ? WHERE id = ?",
                    [qty, existing["id"]])
        else:
            execute("INSERT INTO cart (user_id, product_id, quantity, size, color) VALUES (?,?,?,?,?)",
                    [session["user_id"], pid, qty, size, color])
    else:
        if "cart_session" not in session:
            session["cart_session"] = str(uuid.uuid4())
        sid = session["cart_session"]
        existing = query_one(
            "SELECT id, quantity FROM cart WHERE session_id=? AND product_id=? AND size=? AND color=?",
            [sid, pid, size, color])
        if existing:
            execute("UPDATE cart SET quantity = quantity + ? WHERE id = ?",
                    [qty, existing["id"]])
        else:
            execute("INSERT INTO cart (session_id, product_id, quantity, size, color) VALUES (?,?,?,?,?)",
                    [sid, pid, qty, size, color])

    flash("Added to cart.", "success")
    return redirect(request.referrer or url_for("products"))


@app.route("/cart")
def cart():
    items = get_cart_items()
    subtotal = sum(
        (float(it["sale_price"] or it["original_price"])) * it["quantity"]
        for it in items
    )
    delivery = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_CHARGES
    return render_template("cart.html", items=items, subtotal=subtotal,
                           delivery=delivery, total=subtotal + delivery)


@app.route("/cart/update/<int:cart_id>", methods=["POST"])
def cart_update(cart_id):
    qty = max(1, int(request.form.get("quantity", 1)))
    execute("UPDATE cart SET quantity = ? WHERE id = ?", [qty, cart_id])
    return redirect(url_for("cart"))


@app.route("/cart/remove/<int:cart_id>", methods=["POST"])
def cart_remove(cart_id):
    execute("DELETE FROM cart WHERE id = ?", [cart_id])
    return redirect(url_for("cart"))


# ---------- Auth ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form["password"]
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return redirect(url_for("register"))
        existing = query_one("SELECT id FROM users WHERE email = ?", [email])
        if existing:
            flash("Email already registered.", "error")
            return redirect(url_for("register"))
        execute(
            "INSERT INTO users (name, email, phone, password_hash) VALUES (?,?,?,?)",
            [name, email, phone, generate_password_hash(password, method='pbkdf2:sha256')]
        )
        user = query_one("SELECT id FROM users WHERE email = ?", [email])
        session["user_id"] = user["id"]
        session["role"] = "customer"
        flash("Welcome to ZELO LIVE BOUTIQUE!", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        password = request.form["password"]
        user = query_one("SELECT * FROM users WHERE email = ?", [email])
        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["role"] = user["role"]
            # Merge guest cart into user cart
            if "cart_session" in session:
                execute("""
                    UPDATE cart SET user_id = ?, session_id = NULL
                    WHERE session_id = ? AND user_id IS NULL
                """, [user["id"], session["cart_session"]])
                session.pop("cart_session", None)
            flash(f"Welcome back, {user['name']}!", "success")
            if user["role"] == "admin":
                return redirect(url_for("admin_dashboard"))
            return redirect(url_for("index"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("index"))


# ---------- Account ----------
@app.route("/account")
@login_required
def account():
    user = query_one("SELECT * FROM users WHERE id = ?", [session["user_id"]])
    addresses = query("SELECT * FROM addresses WHERE user_id = ? ORDER BY is_default DESC",
                      [user["id"]])
    return render_template("account.html", user=user, addresses=addresses)


@app.route("/account/update", methods=["POST"])
@login_required
def account_update():
    name = request.form["name"].strip()
    phone = request.form.get("phone", "").strip()
    execute("UPDATE users SET name=?, phone=? WHERE id=?",
            [name, phone, session["user_id"]])
    flash("Profile updated.", "success")
    return redirect(url_for("account"))


@app.route("/account/address/add", methods=["POST"])
@login_required
def address_add():
    f = request.form
    execute("""INSERT INTO addresses
        (user_id, full_name, phone, address, city, province, postal_code, is_default)
        VALUES (?,?,?,?,?,?,?,?)""",
        [session["user_id"], f["full_name"], f["phone"], f["address"],
         f["city"], f["province"], f.get("postal_code", ""),
         f.get("is_default") == "on"])
    flash("Address saved.", "success")
    return redirect(url_for("account"))


@app.route("/account/address/delete/<int:aid>", methods=["POST"])
@login_required
def address_delete(aid):
    execute("DELETE FROM addresses WHERE id=? AND user_id=?", [aid, session["user_id"]])
    return redirect(url_for("account"))


# ---------- Checkout ----------
@app.route("/checkout", methods=["GET", "POST"])
def checkout():
    items = get_cart_items()
    if not items:
        flash("Your cart is empty.", "warning")
        return redirect(url_for("cart"))

    subtotal = sum(
        float(it["sale_price"] or it["original_price"]) * it["quantity"]
        for it in items
    )

    if request.method == "POST":
        # Collect shipping info
        if "user_id" in session:
            user = query_one("SELECT * FROM users WHERE id=?", [session["user_id"]])
            guest_name, guest_email, guest_phone = user["name"], user["email"], user["phone"] or ""
        else:
            guest_name = request.form["guest_name"].strip()
            guest_email = request.form["guest_email"].strip()
            guest_phone = request.form["guest_phone"].strip()

        shipping = {
            "name": request.form["shipping_name"].strip(),
            "phone": request.form["shipping_phone"].strip(),
            "address": request.form["shipping_address"].strip(),
            "city": request.form["shipping_city"].strip(),
            "province": request.form["shipping_province"].strip(),
            "postal": request.form.get("shipping_postal", "").strip(),
        }

        # Coupon
        coupon_code = request.form.get("coupon_code", "").strip().upper()
        discount = 0
        if coupon_code:
            c = query_one("SELECT * FROM coupons WHERE code=? AND is_active", [coupon_code])
            if c and c["used_count"] < c["max_uses"] and subtotal >= c["min_order"]:
                if c["discount_type"] == "percent":
                    discount = subtotal * (c["discount_value"] / 100)
                else:
                    discount = float(c["discount_value"])
                execute("UPDATE coupons SET used_count = used_count + 1 WHERE id=?", [c["id"]])
            else:
                flash("Invalid or expired coupon.", "error")
                delivery = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_CHARGES
                return render_template("checkout.html", items=items, subtotal=subtotal,
                                       delivery=delivery, discount=discount,
                                       total=subtotal + delivery - discount,
                                       coupon_code=coupon_code)

        delivery = 0 if subtotal >= 5000 else 300
        total = subtotal + delivery - discount
        payment_method = request.form.get("payment_method", "JazzCash")

        # Create order
        order_number = "ZLB-" + datetime.now().strftime("%Y%m%d") + "-" + \
                       secrets.token_hex(3).upper()

        conn = get_db()
        try:
            # Use RETURNING id to get the newly created order ID directly and safely
            order_row = conn.execute("""
                INSERT INTO orders
                (order_number, user_id, guest_name, guest_email, guest_phone,
                 shipping_name, shipping_phone, shipping_address, shipping_city,
                 shipping_province, shipping_postal, subtotal, delivery_charges,
                 discount, total, payment_method, coupon_code)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                RETURNING id
            """, [order_number, session.get("user_id"), guest_name, guest_email,
                  guest_phone, shipping["name"], shipping["phone"],
                  shipping["address"], shipping["city"], shipping["province"],
                  shipping["postal"], subtotal, delivery, discount, total,
                  payment_method, coupon_code or None]).fetchone()
            
            order_id = order_row[0]

            for it in items:
                price = float(it["sale_price"] or it["original_price"])
                conn.execute("""
                    INSERT INTO order_items
                    (order_id, product_id, product_name, quantity, price, size, color, image_url)
                    VALUES (?,?,?,?,?,?,?,?)
                """, [order_id, it["id"], it["name"], it["quantity"], price,
                      it.get("size"), it.get("color"), it.get("image")])
                # Decrease stock and increment sold
                conn.execute("""
                    UPDATE products SET stock = stock - ?, sold = sold + ?
                    WHERE id = ?
                """, [it["quantity"], it["quantity"], it["id"]])

            # Clear cart
            if "user_id" in session:
                conn.execute("DELETE FROM cart WHERE user_id = ?", [session["user_id"]])
            else:
                conn.execute("DELETE FROM cart WHERE session_id = ?",
                             [session.get("cart_session")])
        finally:
            conn.close()

        session.pop("cart_session", None)
        flash(f"Order placed successfully! Order #{order_number}", "success")
        return redirect(url_for("order_detail", order_number=order_number))

    delivery = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_CHARGES
    user = query_one("SELECT * FROM users WHERE id=?", [session["user_id"]]) \
        if "user_id" in session else None
    addresses = query("SELECT * FROM addresses WHERE user_id=?",
                      [session["user_id"]]) if user else []
    return render_template("checkout.html", items=items, subtotal=subtotal,
                           delivery=delivery, discount=0,
                           total=subtotal + delivery, user=user, addresses=addresses)


# ---------- Orders ----------
@app.route("/orders")
@login_required
def orders():
    user_orders = query("""
        SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC
    """, [session["user_id"]])
    return render_template("orders.html", orders=user_orders)


@app.route("/order/<order_number>")
def order_detail(order_number):
    order = query_one("SELECT * FROM orders WHERE order_number = ?", [order_number])
    if not order:
        abort(404)
    
    # Permission check:
    # - Admin can view any order
    # - Logged-in customer can only view their own orders
    # - Guest can view the order they just placed (order number is the secret)
    if session.get("role") == "admin":
        pass  # Admin can view all
    elif "user_id" in session:
        # Logged-in user — only allow if it's their order
        if order["user_id"] != session["user_id"]:
            abort(403)
    else:
        # Guest user — allow access (order number is unique & hard to guess)
        # But store the order number in session so they can re-access it
        if "recent_orders" not in session:
            session["recent_orders"] = []
        if order_number not in session["recent_orders"]:
            session["recent_orders"].append(order_number)
            # Keep only last 5 orders in session
            session["recent_orders"] = session["recent_orders"][-5:]
    
    items = query("SELECT * FROM order_items WHERE order_id = ?", [order["id"]])
    return render_template("order.html", order=order, items=items)

@app.route("/track-order", methods=["GET", "POST"])
def track_order():
    """Allow guests to track their recent orders by order number."""
    if request.method == "POST":
        order_number = request.form.get("order_number", "").strip().upper()
        order = query_one("SELECT * FROM orders WHERE order_number = ?", [order_number])
        if order:
            return redirect(url_for("order_detail", order_number=order_number))
        flash("Order not found. Please check the order number.", "error")
    
    return render_template("track_order.html")


# ---------- Admin ----------
@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "products": query_one("SELECT COUNT(*) as c FROM products")["c"],
        "orders": query_one("SELECT COUNT(*) as c FROM orders")["c"],
        "customers": query_one("SELECT COUNT(*) as c FROM users WHERE role='customer'")["c"],
        "revenue": query_one("SELECT COALESCE(SUM(total),0) as s FROM orders WHERE status != 'Cancelled'")["s"],
        "pending": query_one("SELECT COUNT(*) as c FROM orders WHERE status='Pending'")["c"],
    }
    recent_orders = query("SELECT * FROM orders ORDER BY created_at DESC LIMIT 10")
    low_stock = query("SELECT * FROM products WHERE stock < 10 AND is_active ORDER BY stock LIMIT 10")
    return render_template("admin/dashboard.html", stats=stats,
                           recent_orders=recent_orders, low_stock=low_stock)


@app.route("/admin/products")
@admin_required
def admin_products():
    products = query("""
        SELECT p.*, b.name as brand_name, c.name as category_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        ORDER BY p.created_at DESC
    """)
    return render_template("admin/products.html", products=products)


@app.route("/admin/product/new", methods=["GET", "POST"])
@app.route("/admin/product/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def admin_product_form(pid=None):
    product = query_one("SELECT * FROM products WHERE id = ?", [pid]) if pid else None
    brands = query("SELECT * FROM brands ORDER BY name")
    categories = query("SELECT * FROM categories ORDER BY sort_order")
    images = query("SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order",
                   [pid]) if pid else []

    if request.method == "POST":
        f = request.form
        
        # --- Handle Features ---
        features = []
        for i in range(4):
            title = request.form.get(f"feat_title_{i}", "").strip()
            desc = request.form.get(f"feat_desc_{i}", "").strip()
            if title:
                features.append({"title": title, "desc": desc})
        features_json = json.dumps(features)

        slug = slugify(f["name"]) if not (product and product["slug"]) else product["slug"]
        
        # Data for DB (added features_json at the end)
        data = [
            f["name"], slug, f.get("description", ""), f.get("fabric", ""),
            int(f["brand_id"]) or None, int(f["category_id"]) or None,
            float(f["original_price"]),
            float(f["sale_price"]) if f.get("sale_price") else None,
            int(f["stock"]),
            f.get("sizes", "[]"), f.get("colors", "[]"),
            f.get("is_new_arrival") == "on",
            f.get("is_popular") == "on",
            f.get("is_sale") == "on",
            f.get("is_active") == "on",
            features_json
        ]

        if product:
            execute("""
                UPDATE products SET name=?, slug=?, description=?, fabric=?,
                    brand_id=?, category_id=?, original_price=?, sale_price=?,
                    stock=?, sizes=?, colors=?, is_new_arrival=?, is_popular=?,
                    is_sale=?, is_active=?, features=?
                WHERE id=?
            """, data + [pid])
            flash("Product updated.", "success")
        else:
            execute("""
                INSERT INTO products
                (name, slug, description, fabric, brand_id, category_id,
                 original_price, sale_price, stock, sizes, colors,
                 is_new_arrival, is_popular, is_sale, is_active, features)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, data)
            new_p = query_one("SELECT id FROM products WHERE slug = ?", [slug])
            pid = new_p["id"]
            flash("Product added.", "success")

        # --- Handle Images (Clear old, insert new) ---
        execute("DELETE FROM product_images WHERE product_id = ?", [pid])
        image_urls = request.form.getlist("image_urls[]")
        for url in image_urls:
            url = url.strip()
            if url:
                execute("INSERT INTO product_images (product_id, image_url) VALUES (?,?)",
                        [pid, url])
        
        # Redirect to edit page to show new images
        return redirect(url_for("admin_product_form", pid=pid))

    return render_template("admin/product_form.html", product=product,
                           brands=brands, categories=categories, images=images)


@app.route("/admin/product/<int:pid>/delete", methods=["POST"])
@admin_required
def admin_product_delete(pid):
    execute("DELETE FROM product_images WHERE product_id = ?", [pid])
    execute("DELETE FROM products WHERE id = ?", [pid])
    flash("Product deleted.", "success")
    return redirect(url_for("admin_products"))


@app.route("/admin/product/image/<int:iid>/delete", methods=["POST"])
@admin_required
def admin_image_delete(iid):
    execute("DELETE FROM product_images WHERE id = ?", [iid])
    flash("Image removed.", "success")
    return redirect(request.referrer or url_for("admin_products"))


@app.route("/admin/orders")
@admin_required
def admin_orders():
    status = request.args.get("status")
    if status:
        orders = query("SELECT * FROM orders WHERE status=? ORDER BY created_at DESC", [status])
    else:
        orders = query("SELECT * FROM orders ORDER BY created_at DESC")
    return render_template("admin/orders.html", orders=orders, current_status=status)


@app.route("/admin/order/<int:oid>/status", methods=["POST"])
@admin_required
def admin_order_status(oid):
    status = request.form["status"]
    execute("UPDATE orders SET status=? WHERE id=?", [status, oid])
    flash("Order status updated.", "success")
    return redirect(request.referrer or url_for("admin_orders"))


@app.route("/admin/customers")
@admin_required
def admin_customers():
    customers = query("SELECT * FROM users WHERE role='customer' ORDER BY created_at DESC")
    return render_template("admin/customers.html", customers=customers)


# ---------- Admin: Add Customer ----------
@app.route("/admin/customer/new", methods=["GET", "POST"])
@admin_required
def admin_customer_add():
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "customer123")  # Default password
        
        existing = query_one("SELECT id FROM users WHERE email = ?", [email])
        if existing:
            flash("Email already exists.", "error")
            return redirect(url_for("admin_customer_add"))
        
        execute(
            "INSERT INTO users (name, email, phone, password_hash, role) VALUES (?,?,?,?, 'customer')",
            [name, email, phone, generate_password_hash(password, method='pbkdf2:sha256')]
        )
        flash(f"Customer {name} added successfully.", "success")
        return redirect(url_for("admin_customers"))
    
    return render_template("admin/customer_form.html")


# ---------- Admin: Add Order ----------
@app.route("/admin/order/new", methods=["GET", "POST"])
@admin_required
def admin_order_add():
    if request.method == "POST":
        # Get customer or guest info
        customer_id = request.form.get("customer_id")
        if customer_id:
            customer = query_one("SELECT * FROM users WHERE id = ?", [customer_id])
            guest_name, guest_email, guest_phone = None, None, None
        else:
            guest_name = request.form["guest_name"].strip()
            guest_email = request.form["guest_email"].strip()
            guest_phone = request.form["guest_phone"].strip()
            customer = None
        
        # Shipping info
        shipping = {
            "name": request.form["shipping_name"].strip(),
            "phone": request.form["shipping_phone"].strip(),
            "address": request.form["shipping_address"].strip(),
            "city": request.form["shipping_city"].strip(),
            "province": request.form["shipping_province"].strip(),
            "postal": request.form.get("shipping_postal", "").strip(),
        }
        
        # Get products and quantities
        product_ids = request.form.getlist("product_id[]")
        quantities = request.form.getlist("quantity[]")
        prices = request.form.getlist("price[]")
        
        subtotal = sum(float(p) * int(q) for p, q in zip(prices, quantities) if p and q)
        delivery = float(request.form.get("delivery_charges", DELIVERY_CHARGES))
        discount = float(request.form.get("discount", 0))
        total = subtotal + delivery - discount
        
        payment_method = request.form.get("payment_method", "JazzCash")
        status = request.form.get("status", "Pending")
        
        # Generate order number
        order_number = "ZLB-" + datetime.now().strftime("%Y%m%d") + "-" + secrets.token_hex(3).upper()
        
        conn = get_db()
        try:
            conn.execute("""
                INSERT INTO orders
                (order_number, user_id, guest_name, guest_email, guest_phone,
                 shipping_name, shipping_phone, shipping_address, shipping_city,
                 shipping_province, shipping_postal, subtotal, delivery_charges,
                 discount, total, payment_method, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [order_number, customer_id, guest_name, guest_email, guest_phone,
                  shipping["name"], shipping["phone"], shipping["address"],
                  shipping["city"], shipping["province"], shipping["postal"],
                  subtotal, delivery, discount, total, payment_method, status])
            
            order_row = conn.execute(
                "SELECT id FROM orders WHERE order_number = ?", [order_number]).fetchone()
            order_id = order_row[0]
            
            # Add order items
            for pid, qty, price in zip(product_ids, quantities, prices):
                if pid and qty and price:
                    product = query_one("SELECT name FROM products WHERE id = ?", [pid])
                    conn.execute("""
                        INSERT INTO order_items
                        (order_id, product_id, product_name, quantity, price)
                        VALUES (?,?,?,?,?)
                    """, [order_id, int(pid), product["name"], int(qty), float(price)])
                    
                    # Update stock
                    conn.execute("""
                        UPDATE products SET stock = stock - ?, sold = sold + ?
                        WHERE id = ?
                    """, [int(qty), int(qty), int(pid)])
            
        finally:
            conn.close()
        
        flash(f"Order {order_number} created successfully.", "success")
        return redirect(url_for("admin_orders"))
    
    # GET request - show form
    customers = query("SELECT * FROM users WHERE role='customer' ORDER BY name")
    products = query("SELECT * FROM products WHERE is_active ORDER BY name")
    return render_template("admin/order_form.html", customers=customers, products=products)


# ---------- Admin: Product Quick View (Google Shopping Style) ----------
@app.route("/admin/products/grid")
@admin_required
def admin_products_grid():
    products = query("""
        SELECT p.*, b.name as brand_name, c.name as category_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        ORDER BY p.created_at DESC
    """)
    return render_template("admin/products_grid.html", products=products)


# ---------- Payment Instructions Page ----------
@app.route("/payment-instructions/<order_number>")
def payment_instructions(order_number):
    order = query_one("SELECT * FROM orders WHERE order_number = ?", [order_number])
    if not order:
        abort(404)
    
    # Check if user has permission to view this order
    if "role" not in session or session["role"] != "admin":
        if order["user_id"] != session.get("user_id"):
            abort(403)
    
    return render_template("payment_instructions.html", order=order)


# ---------- Upload Payment Proof ----------
import cloudinary.uploader  # Add this import at top of app.py

@app.route("/upload-payment-proof/<order_number>", methods=["POST"])
def upload_payment_proof(order_number):
    order = query_one("SELECT * FROM orders WHERE order_number = ?", [order_number])
    if not order:
        abort(404)
    
    if "role" not in session or session["role"] != "admin":
        if order["user_id"] != session.get("user_id"):
            abort(403)
    
    if "payment_proof" not in request.files:
        flash("No file uploaded.", "error")
        return redirect(url_for("payment_instructions", order_number=order_number))
    
    file = request.files["payment_proof"]
    if file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("payment_instructions", order_number=order_number))
    
    try:
        # Upload directly to Cloudinary - NO LOCAL SAVING
        result = cloudinary.uploader.upload(
            file, 
            folder="zelo_boutique/payment_proofs",
            resource_type="image"
        )
        proof_url = result["secure_url"]
        
        execute("UPDATE orders SET payment_proof_url = ? WHERE order_number = ?",
                [proof_url, order_number])
        
        flash("Payment proof uploaded successfully!", "success")
    except Exception as e:
        print(f"Cloudinary upload error: {e}")
        flash("Failed to upload payment proof.", "error")
    
    return redirect(url_for("order_detail", order_number=order_number))


# ---------- Run ----------
# REMOVE THIS OLD BLOCK:
# if __name__ == "__main__":
#     port = int(os.getenv("PORT", 5000))
#     app.run(debug=False, host="0.0.0.0", port=port)

# ADD THIS NEW BLOCK INSTEAD:
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

def handler(request):
    return app(request.environ, lambda *args: None)