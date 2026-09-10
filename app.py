from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify, abort
from functools import wraps
import os
# Must happen before duckdb is imported/used — Vercel's filesystem is read-only
# except /tmp, and duckdb needs a resolvable home directory.
os.environ.setdefault("HOME", "/tmp")
os.environ.setdefault("DUCKDB_HOME_DIR", "/tmp")
import json
import time
import uuid
import secrets
import base64
from datetime import datetime
import duckdb
import requests
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.environ.get("FLASK_SECRET_KEY", "super-secret-key-for-sessions"))

DELIVERY_CHARGES = float(os.getenv("DELIVERY_CHARGES", 150))
FREE_DELIVERY_ABOVE = float(os.getenv("FREE_DELIVERY_ABOVE", 3000))


def get_db():
    token = os.getenv("MOTHERDUCK_TOKEN")
    db = os.getenv("MOTHERDUCK_DATABASE", "zelo_boutique")

    conn = duckdb.connect(
        f"md:{db}?motherduck_token={token}",
        config={"home_directory": "/tmp"}
    )
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

    try:
        p['sizes_list'] = json.loads(p['sizes']) if p['sizes'] else []
    except Exception:
        p['sizes_list'] = []

    try:
        p['colors_list'] = json.loads(p['colors']) if p['colors'] else []
    except Exception:
        p['colors_list'] = []

    try:
        p['features_list'] = json.loads(p['features']) if p.get('features') else []
    except Exception:
        p['features_list'] = []

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


# ---------- Cart (single source of truth: DB-backed) ----------
@app.route("/cart")
def cart():
    items = get_cart_items()
    for it in items:
        it["price"] = float(it["sale_price"] or it["original_price"])
        it["total"] = it["price"] * it["quantity"]
    subtotal = sum(it["total"] for it in items)
    delivery = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_CHARGES
    total = subtotal + delivery
    return render_template("cart.html", cart_items=items, subtotal=subtotal,
                           delivery=delivery, total=total)


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


@app.route("/cart/update/<int:cart_id>", methods=["POST"])
def cart_update(cart_id):
    qty = max(1, int(request.form.get("quantity", 1)))
    execute("UPDATE cart SET quantity = ? WHERE id = ?", [qty, cart_id])
    return redirect(url_for("cart"))


@app.route("/cart/remove/<int:cart_id>", methods=["POST"])
def cart_remove(cart_id):
    execute("DELETE FROM cart WHERE id = ?", [cart_id])
    flash("Item removed from cart.", "info")
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
        if "user_id" in session:
            user = query_one("SELECT * FROM users WHERE id=?", [session["user_id"]])
            ship_name = user["name"]
            ship_phone = user["phone"] or ""
            guest_name, guest_email, guest_phone = user["name"], user["email"], user["phone"] or ""
        else:
            ship_name = request.form["guest_name"].strip()
            ship_phone = request.form["guest_phone"].strip()
            guest_name = ship_name
            guest_email = request.form["guest_email"].strip()
            guest_phone = ship_phone

        shipping = {
            "name": ship_name,
            "phone": ship_phone,
            "address": request.form["shipping_address"].strip(),
            "city": request.form["shipping_city"].strip(),
            "province": request.form["shipping_province"].strip(),
            "postal": request.form.get("shipping_postal", "").strip(),
        }

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

        delivery = 0 if subtotal >= FREE_DELIVERY_ABOVE else DELIVERY_CHARGES
        total = subtotal + delivery - discount
        payment_method = request.form.get("payment_method", "JazzCash")

        order_number = "ZLB-" + datetime.now().strftime("%Y%m%d") + "-" + \
                       secrets.token_hex(3).upper()

        conn = get_db()
        try:
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
                conn.execute("""
                    UPDATE products SET stock = stock - ?, sold = sold + ?
                    WHERE id = ?
                """, [it["quantity"], it["quantity"], it["id"]])

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

    if session.get("role") == "admin":
        pass
    elif "user_id" in session:
        if order["user_id"] != session["user_id"]:
            abort(403)
    else:
        if "recent_orders" not in session:
            session["recent_orders"] = []
        if order_number not in session["recent_orders"]:
            session["recent_orders"].append(order_number)
            session["recent_orders"] = session["recent_orders"][-5:]

    items = query("SELECT * FROM order_items WHERE order_id = ?", [order["id"]])
    return render_template("order.html", order=order, items=items)


@app.route("/track-order", methods=["GET", "POST"])
def track_order():
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

    # Expose parsed helpers for the template when editing
    if product:
        try:
            product['features_list'] = json.loads(product['features']) if product.get('features') else []
        except Exception:
            product['features_list'] = []
        try:
            bundle = json.loads(product['bundle_info']) if product.get('bundle_info') else {}
        except Exception:
            bundle = {}
        product['bundle_type'] = bundle.get('type', 'standard')

    brands = query("SELECT * FROM brands ORDER BY name")
    categories = query("SELECT * FROM categories ORDER BY sort_order")
    images = query("SELECT * FROM product_images WHERE product_id = ? ORDER BY sort_order",
                   [pid]) if pid else []

    if request.method == "POST":
        f = request.form

        # Features — read the JSON built client-side by the features textarea parser
        features_json = f.get("features_json", "[]")
        try:
            json.loads(features_json)
        except Exception:
            features_json = "[]"

        slug = slugify(f["name"]) if not (product and product["slug"]) else product["slug"]

        # Sizes / Colors — built client-side as JSON by the chip inputs
        sizes_json = f.get("sizes", "[]")
        colors_json = f.get("colors", "[]")
        try:
            json.loads(sizes_json)
        except Exception:
            sizes_json = "[]"
        try:
            json.loads(colors_json)
        except Exception:
            colors_json = "[]"

        # Bundle info
        included_pieces = request.form.getlist('included_pieces')
        bundle_type = request.form.get('bundle_type', 'standard')
        kameez_length = request.form.get('kameez_length', '').strip()
        dupatta_length = request.form.get('dupatta_length', '').strip()

        bundle_info = json.dumps({
            "type": bundle_type,
            "pieces": included_pieces,
            "measurements": {
                "kameez": kameez_length,
                "dupatta": dupatta_length
            }
        })

        data = [
            f["name"], slug, f.get("description", ""), f.get("fabric", ""),
            int(f["brand_id"]) if f.get("brand_id") else None,
            int(f["category_id"]) if f.get("category_id") else None,
            float(f["original_price"]),
            float(f["sale_price"]) if f.get("sale_price") else None,
            int(f["stock"]),
            sizes_json, colors_json,
            f.get("is_new_arrival") == "on",
            f.get("is_popular") == "on",
            f.get("is_sale") == "on",
            f.get("is_active") == "on",
            features_json,
            bundle_info,
            f.get("video_url", "").strip()
        ]

        if product:
            execute("""
                UPDATE products SET name=?, slug=?, description=?, fabric=?,
                    brand_id=?, category_id=?, original_price=?, sale_price=?,
                    stock=?, sizes=?, colors=?, is_new_arrival=?, is_popular=?,
                    is_sale=?, is_active=?, features=?, bundle_info=?, video_url=?
                WHERE id=?
            """, data + [pid])
            flash("Product updated.", "success")
        else:
            execute("""
                INSERT INTO products
                (name, slug, description, fabric, brand_id, category_id,
                 original_price, sale_price, stock, sizes, colors,
                 is_new_arrival, is_popular, is_sale, is_active, features, bundle_info, video_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, data)
            new_p = query_one("SELECT id FROM products WHERE slug = ?", [slug])
            pid = new_p["id"]
            flash("Product added.", "success")

        execute("DELETE FROM product_images WHERE product_id = ?", [pid])
        image_urls = request.form.getlist("image_urls[]")
        for i, url in enumerate(image_urls):
            url = url.strip()
            if url:
                execute("INSERT INTO product_images (product_id, image_url, sort_order) VALUES (?,?,?)",
                        [pid, url, i])

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


@app.route("/admin/orders/delete", methods=["POST"])
@admin_required
def admin_orders_bulk_delete():
    ids_to_delete = request.form.getlist('order_ids')

    if not ids_to_delete:
        flash("No orders selected.", "warning")
        return redirect(url_for("admin_orders"))

    deleted_count = 0

    for oid in ids_to_delete:
        execute("DELETE FROM order_items WHERE order_id = ?", [oid])
        execute("DELETE FROM orders WHERE id = ?", [oid])
        deleted_count += 1

    flash(f"Successfully deleted {deleted_count} order(s) and their items.", "success")
    return redirect(url_for("admin_orders"))


@app.route("/admin/customer/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def admin_customer_edit(uid):
    user = query_one("SELECT * FROM users WHERE id = ?", [uid])
    if not user:
        abort(404)

    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        address = request.form.get("address", "").strip()
        city = request.form.get("city", "").strip()
        province = request.form.get("province", "").strip()
        postal_code = request.form.get("postal_code", "").strip()

        execute("""
            UPDATE users SET name=?, email=?, phone=?, address=?, city=?, province=?, postal_code=?
            WHERE id=?
        """, [name, email, phone, address, city, province, postal_code, uid])

        flash("Customer updated successfully.", "success")
        return redirect(url_for("admin_customers"))

    slips = query("SELECT * FROM courier_slips WHERE customer_phone = ? ORDER BY created_at DESC", [user["phone"]])

    return render_template("admin/customer_edit.html", user=user, slips=slips)


@app.route("/admin/customers/delete", methods=["POST"])
@admin_required
def admin_customers_bulk_delete():
    ids_to_delete = request.form.getlist('customer_ids')

    if not ids_to_delete:
        flash("No customers selected.", "warning")
        return redirect(url_for("admin_customers"))

    current_admin_id = session.get("user_id")
    deleted_count = 0

    for uid in ids_to_delete:
        user = query_one("SELECT id, role FROM users WHERE id = ?", [uid])
        if user and user['role'] == 'customer' and user['id'] != current_admin_id:
            execute("DELETE FROM users WHERE id = ?", [uid])
            deleted_count += 1

    flash(f"Successfully deleted {deleted_count} customer(s).", "success")
    return redirect(url_for("admin_customers"))


@app.route("/admin/customer/<int:uid>/delete", methods=["POST"])
@admin_required
def admin_customer_delete(uid):
    if session.get("user_id") == uid:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin_customers"))

    execute("DELETE FROM users WHERE id = ? AND role = 'customer'", [uid])
    flash("Customer deleted successfully.", "success")
    return redirect(url_for("admin_customers"))


@app.route("/admin/customers")
@admin_required
def admin_customers():
    customers = query("SELECT * FROM users WHERE role='customer' ORDER BY created_at DESC")
    return render_template("admin/customers.html", customers=customers)


@app.route("/admin/customer/new", methods=["GET", "POST"])
@admin_required
def admin_customer_add():
    if request.method == "POST":
        name = request.form["name"].strip().upper()
        email = request.form["email"].strip().lower()
        phone = request.form["phone"].strip()
        city = request.form.get("city", "").strip().upper()
        address = request.form.get("address", "").strip()

        courier_name = request.form.get("courier_name", "").strip()
        slip_date = request.form.get("slip_date", "").strip()
        description = request.form.get("description", "").strip()
        charges = request.form.get("charges", "").strip()
        tracking_number = request.form.get("tracking_number", "").strip()

        existing = query_one("SELECT id FROM users WHERE email = ?", [email])
        if existing:
            flash("Email already exists.", "error")
            return redirect(url_for("admin_customer_add"))

        phone_exists = query_one("SELECT id FROM users WHERE phone = ?", [phone])
        if phone_exists:
            flash("Phone number already exists.", "error")
            return redirect(url_for("admin_customer_add"))

        execute(
            "INSERT INTO users (name, email, phone, password_hash, role, city, address) VALUES (?,?,?,?,?,?,?)",
            [name, email, phone, generate_password_hash("temp123", method='pbkdf2:sha256'), 'customer', city, address]
        )

        if tracking_number:
            tracking_exists = query_one("SELECT id FROM courier_slips WHERE tracking_number = ?", [tracking_number])
            if not tracking_exists:
                execute("""
                    INSERT INTO courier_slips
                    (customer_name, customer_phone, city, address, tracking_number, courier_name, slip_date, description, charges)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [name, phone, city, address, tracking_number, courier_name, slip_date, description, charges])
                flash(f"Customer '{name}' added successfully with tracking number {tracking_number}.", "success")
            else:
                flash(f"Customer '{name}' added, but tracking number {tracking_number} already exists.", "warning")
        else:
            flash(f"Customer '{name}' added successfully.", "success")

        return redirect(url_for("admin_customers"))

    return render_template("admin/customer_form.html")


@app.route("/admin/order/new", methods=["GET", "POST"])
@admin_required
def admin_order_add():
    if request.method == "POST":
        customer_id = request.form.get("customer_id")
        if customer_id:
            customer = query_one("SELECT * FROM users WHERE id = ?", [customer_id])
            ship_name = customer["name"]
            ship_phone = customer["phone"] or ""
            guest_name, guest_email, guest_phone = None, None, None
        else:
            ship_name = request.form["guest_name"].strip()
            ship_phone = request.form["guest_phone"].strip()
            guest_name = ship_name
            guest_email = request.form["guest_email"].strip()
            guest_phone = ship_phone
            customer = None

        shipping = {
            "name": ship_name,
            "phone": ship_phone,
            "address": request.form["shipping_address"].strip(),
            "city": request.form["shipping_city"].strip(),
            "province": request.form["shipping_province"].strip(),
            "postal": request.form.get("shipping_postal", "").strip(),
        }

        product_ids = request.form.getlist("product_id[]")
        quantities = request.form.getlist("quantity[]")
        prices = request.form.getlist("price[]")

        subtotal = sum(float(p) * int(q) for p, q in zip(prices, quantities) if p and q)
        delivery = float(request.form.get("delivery_charges", DELIVERY_CHARGES))
        discount = float(request.form.get("discount", 0))
        total = subtotal + delivery - discount

        payment_method = request.form.get("payment_method", "JazzCash")
        status = request.form.get("status", "Pending")

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

            for pid, qty, price in zip(product_ids, quantities, prices):
                if pid and qty and price:
                    product = query_one("SELECT name FROM products WHERE id = ?", [pid])
                    conn.execute("""
                        INSERT INTO order_items
                        (order_id, product_id, product_name, quantity, price)
                        VALUES (?,?,?,?,?)
                    """, [order_id, int(pid), product["name"], int(qty), float(price)])

                    conn.execute("""
                        UPDATE products SET stock = stock - ?, sold = sold + ?
                        WHERE id = ?
                    """, [int(qty), int(qty), int(pid)])

        finally:
            conn.close()

        flash(f"Order {order_number} created successfully.", "success")
        return redirect(url_for("admin_orders"))

    customers = query("SELECT * FROM users WHERE role='customer' ORDER BY name")
    products = query("SELECT * FROM products WHERE is_active ORDER BY name")
    return render_template("admin/order_form.html", customers=customers, products=products)


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

    if "role" not in session or session["role"] != "admin":
        if order["user_id"] != session.get("user_id"):
            abort(403)

    return render_template("payment_instructions.html", order=order)


# ---------- Upload Payment Proof ----------
import cloudinary.uploader

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
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


# ---------- Admin: Customer Map ----------
@app.route("/admin/map")
@admin_required
def admin_map():
    city_stats = query("""
        SELECT shipping_city as city, COUNT(*) as count
        FROM orders
        WHERE shipping_city IS NOT NULL AND shipping_city != ''
        GROUP BY shipping_city
    """)

    slip_stats = query("""
        SELECT city, COUNT(*) as count
        FROM courier_slips
        WHERE city IS NOT NULL AND city != ''
        GROUP BY city
    """)

    merged = {}
    for row in city_stats:
        c = row['city'].title()
        merged[c] = merged.get(c, 0) + row['count']
    for row in slip_stats:
        c = row['city'].title()
        merged[c] = merged.get(c, 0) + row['count']

    map_data = json.dumps([{"city": k, "count": v} for k, v in merged.items()])

    map_data_list = [{"city": k, "count": v} for k, v in sorted(merged.items(), key=lambda x: x[1], reverse=True)]
    max_count = max(merged.values()) if merged else 1

    return render_template("admin/map.html", map_data=map_data, map_data_list=map_data_list, max_count=max_count)


# ---------- Admin: Courier Scanner (GET) ----------
@app.route("/admin/courier-scanner")
@admin_required
def admin_courier_scanner():
    return render_template("admin/courier_scanner.html")


@app.route("/admin/courier-scanner/update", methods=["POST"])
@admin_required
def admin_courier_update():
    if not request.is_json:
        return jsonify({"success": False, "error": "Invalid data format"}), 400

    data = request.get_json()
    slips = data.get('slips', [])
    saved_count = 0

    try:
        for slip in slips:
            name = slip.get("customer_name", "").strip().upper()
            phone = slip.get("customer_phone", "").strip()
            city = slip.get("city", "").strip().upper()
            address = slip.get("address", "").strip()
            courier_name = slip.get("courier_name", "").strip()
            description = slip.get("description", "").strip()
            charges = slip.get("charges", "").strip()
            slip_date = slip.get("slip_date", "").strip()
            tracking_number = slip.get("tracking_number", "").strip()

            if not name or not phone or not tracking_number:
                continue

            existing_user = query_one("SELECT id FROM users WHERE phone = ?", [phone])
            if not existing_user:
                placeholder_email = f"{phone}@zelolive.com"
                existing_email = query_one("SELECT id FROM users WHERE email = ?", [placeholder_email])
                if not existing_email:
                    execute("""
                        INSERT INTO users (name, email, phone, password_hash, role, city, address)
                        VALUES (?, ?, ?, ?, 'customer', ?, ?)
                    """, [name, placeholder_email, phone, generate_password_hash("temp123", method='pbkdf2:sha256'), city, address])
            else:
                if address or city:
                    execute("""
                        UPDATE users SET city = COALESCE(NULLIF(?, ''), city),
                                         address = COALESCE(NULLIF(?, ''), address)
                        WHERE id = ?
                    """, [city, address, existing_user['id']])

            exists = query_one("SELECT id FROM courier_slips WHERE tracking_number = ?", [tracking_number])
            if not exists:
                execute("""
                    INSERT INTO courier_slips (customer_name, customer_phone, city, address, tracking_number, courier_name, description, charges, slip_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, [name, phone, city, address, tracking_number, courier_name or 'Other', description, charges, slip_date])
                saved_count += 1

        return jsonify({"success": True, "saved": saved_count})

    except Exception as e:
        print(f"DATABASE ERROR SAVING SLIPS: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ---------- Admin: Gemini Vision Smart Scanner ----------
SMART_SCAN_PROMPT = """You are an expert OCR system extracting data from a photo of a Pakistani courier slip (PostEx, TCS, Leopards, etc.). The photo may be rotated or at an angle — read all text carefully regardless of orientation, including small print and stamped/rotated text near the edges.

Extract the following information and return ONLY a valid JSON object. Do not include markdown formatting.
{
    "tracking_number": "The main tracking/AWB number EXACTLY as it appears. KEEP all dashes, hashes, and spaces. Do not add or drop any digits.",
    "courier_name": "Courier company name (e.g., PostEx, TCS)",
    "customer_name": "Consignee/Receiver/To name, copied exactly as printed",
    "customer_phone": "Phone number in format 03XXXXXXXXX",
    "city": "Destination city",
    "address": "Delivery address copied exactly as printed. Only fix obvious OCR noise (stray symbols); never invent or guess words you cannot clearly read.",
    "description": "Item description (e.g., CLOTH)",
    "charges": "Key financial details (e.g., Payable: 305, Service: 200)",
    "slip_date": "CRITICAL: Near the bottom right corner, just above the bold 'SHIPPER COPY' text, find the label 'Booking Date:' — it may be printed sideways/rotated 90 degrees. Extract the date and time exactly as written (e.g., 2026-09-05 00:00). Look carefully; this field is small and easy to miss."
}

Rules:
- If a field is genuinely not visible anywhere on the slip, use empty string ""
- Never fabricate or guess a character you cannot actually see — copy exactly what is printed
- Pakistani phone numbers start with 03 and are 11 digits
- Return ONLY the JSON, nothing else."""


def call_gemini_vision_with_retry(img_bytes, mime_type, max_retries=3):
    for attempt in range(max_retries):
        try:
            resp = gemini_client.models.generate_content(
                model="gemini-3.5-flash",
                contents=[
                    SMART_SCAN_PROMPT,
                    genai_types.Part.from_bytes(data=img_bytes, mime_type=mime_type)
                ],
                config=genai_types.GenerateContentConfig(
                    temperature=0.0,
                    response_mime_type="application/json",
                    thinking_config=genai_types.ThinkingConfig(thinking_level=genai_types.ThinkingLevel.LOW)
                )
            )
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                um = resp.usage_metadata
                print(f"🔢 Gemini token usage — prompt: {um.prompt_token_count}, "
                      f"output: {um.candidates_token_count}, "
                      f"thinking: {getattr(um, 'thoughts_token_count', 0)}, "
                      f"total: {um.total_token_count}")
            return resp
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    print(f"Rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(wait)
                    continue
            raise


@app.route("/admin/smart-scan", methods=["POST"])
@admin_required
def admin_smart_scan():
    if 'image' not in request.files:
        return jsonify({"error": "No image uploaded"}), 400

    file = request.files['image']
    if file.filename == '':
        return jsonify({"error": "No file selected"}), 400

    selected_model = request.form.get('model', 'qwen/qwen3.6-27b')

    try:
        img_bytes = file.read()
        base64_image = base64.b64encode(img_bytes).decode('utf-8')
        mime_type = file.mimetype or 'image/jpeg'

        if selected_model.startswith('openrouter/'):
            openrouter_model = selected_model.replace('openrouter/', '')

            headers = {
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json",
                "HTTP-Referer": request.host_url,
                "X-Title": "ZELO Courier Scanner"
            }

            payload = {
                "model": openrouter_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """You are an expert at extracting data from Pakistani courier slips (PostEx, TCS, Leopards, etc.).

Extract the following information and return ONLY a valid JSON object. Do not include markdown formatting.
{
    "tracking_number": "The main tracking/AWB number EXACTLY as it appears. KEEP all dashes, hashes, and spaces.",
    "courier_name": "Courier company name (e.g., PostEx, TCS)",
    "customer_name": "Consignee/Receiver/To name",
    "customer_phone": "Phone number in format 03XXXXXXXXX",
    "city": "Destination city",
    "address": "Clean delivery address. Fix OCR typos, ignore random symbols.",
    "description": "Item description (e.g., CLOTH)",
    "charges": "Key financial details (e.g., Payable: 305, Service: 200)",
    "slip_date": "CRITICAL: Look at the bottom right corner, just above the bold text 'SHIPPER COPY'. Find the text starting with 'Booking Date:' and extract the date and time exactly as written (e.g., 2026-09-5 00:00)."
}

Rules:
- If a field is not found, use empty string ""
- Pakistani phone numbers start with 03 and are 11 digits
- Return ONLY the JSON, nothing else."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                "max_tokens": 1000
            }

            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers=headers,
                json=payload,
                timeout=60
            )

            if response.status_code != 200:
                print(f"OpenRouter API Error: {response.status_code} - {response.text}")
                return jsonify({"error": f"OpenRouter API error: {response.status_code}"}), 500

            result = response.json()
            content = result['choices'][0]['message']['content'].strip()
        else:
            response = groq_client.chat.completions.create(
                model=selected_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": """You are an expert at extracting data from Pakistani courier slips (PostEx, TCS, Leopards, etc.).

Extract the following information and return ONLY a valid JSON object. Do not include markdown formatting.
{
    "tracking_number": "The main tracking/AWB number EXACTLY as it appears. KEEP all dashes, hashes, and spaces.",
    "courier_name": "Courier company name (e.g., PostEx, TCS)",
    "customer_name": "Consignee/Receiver/To name",
    "customer_phone": "Phone number in format 03XXXXXXXXX",
    "city": "Destination city",
    "address": "Clean delivery address. Fix OCR typos, ignore random symbols.",
    "description": "Item description (e.g., CLOTH)",
    "charges": "Key financial details (e.g., Payable: 305, Service: 200)",
    "slip_date": "CRITICAL: Look at the bottom right corner, just above the bold text 'SHIPPER COPY'. Find the text starting with 'Booking Date:' and extract the date and time exactly as written (e.g., 2026-09-5 00:00)."
}

Rules:
- If a field is not found, use empty string ""
- Pakistani phone numbers start with 03 and are 11 digits
- Return ONLY the JSON, nothing else."""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                temperature=0.0,
                max_completion_tokens=500,
                response_format={"type": "json_object"},
                extra_body={"reasoning_effort": "none"}
            )
            content = response.choices[0].message.content.strip()

        start_idx = content.find('{')
        end_idx = content.rfind('}')

        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            json_str = content[start_idx:end_idx+1]
            try:
                extracted_data = json.loads(json_str)
            except json.JSONDecodeError as e:
                print(f"JSON Decode Error: {e}\nExtracted string: {json_str}")
                return jsonify({"error": f"AI returned malformed JSON: {str(e)}"}), 500
        else:
            print(f"No JSON brackets found in response: {content}")
            return jsonify({"error": f"AI did not return JSON. Response: {content[:200]}"}), 500

        return jsonify({
            "success": True,
            "data": extracted_data
        })

    except Exception as e:
        print(f"Vision API Error: {e}")
        return jsonify({"error": str(e)}), 500


# ---------- Admin: PostEx Tracking ----------
@app.route("/admin/tracking/postex", methods=["GET", "POST"])
@admin_required
def admin_postex_tracking():
    if request.method == "GET":
        return render_template("admin/tracking_postex.html")

    tracking_number = request.json.get("trackingNumber")
    if not tracking_number:
        return jsonify({"error": "Tracking number is required"}), 400

    try:
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://postex.pk",
            "Referer": "https://postex.pk/tracking",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        }
        payload = {"trackingNumber": tracking_number}

        response = requests.post(
            "https://postex.pk/api/tracking-order",
            json=payload,
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"PostEx API returned status {response.status_code}"}), response.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- Public Customer Tracking ----------
@app.route("/track")
def public_track_page():
    return render_template("public_tracking.html")


@app.route("/api/public-track", methods=["POST"])
def public_track_api():
    tracking_number = request.json.get("trackingNumber")
    if not tracking_number:
        return jsonify({"error": "Tracking number is required"}), 400

    try:
        headers = {
            "Content-Type": "application/json",
            "Origin": "https://postex.pk",
            "Referer": "https://postex.pk/tracking",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
        }
        payload = {"trackingNumber": tracking_number}

        response = requests.post(
            "https://postex.pk/api/tracking-order",
            json=payload,
            headers=headers,
            timeout=10
        )

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": f"PostEx API returned status {response.status_code}"}), response.status_code

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------- Information Pages ----------
@app.route("/faq")
def faq():
    return render_template("faq.html")

@app.route("/shipping")
def shipping():
    return render_template("shipping.html")

@app.route("/returns")
def returns():
    return render_template("returns.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")


# ---------- SEO: Sitemap & Robots.txt ----------
@app.route("/sitemap.xml")
def sitemap():
    products = query("SELECT slug, created_at FROM products WHERE is_active = TRUE")

    xml = '<?xml version="1.0" encoding="UTF-8"?>\n'
    xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'

    static_pages = ['/', '/products', '/faq', '/shipping', '/returns', '/privacy', '/terms']
    for page in static_pages:
        xml += f'  <url><loc>https://zelo-theta-murex.vercel.app{page}</loc><changefreq>weekly</changefreq><priority>0.8</priority></url>\n'

    if products:
        for p in products:
            slug = p['slug']
            xml += f'  <url><loc>https://zelo-theta-murex.vercel.app/product/{slug}</loc><changefreq>monthly</changefreq><priority>0.6</priority></url>\n'

    xml += '</urlset>'
    return xml, 200, {'Content-Type': 'application/xml'}


@app.route("/robots.txt")
def robots_txt():
    domain = request.host_url.rstrip('/')
    content = f"""User-agent: *
Allow: /
Disallow: /admin/
Disallow: /checkout
Disallow: /account
Disallow: /cart

Sitemap: {domain}/sitemap.xml
"""
    return app.response_class(content, mimetype='text/plain')


# ---------- About Us Page ----------
@app.route("/about")
def about():
    return render_template("about.html")


def get_video_id(url):
    if not url:
        return ''
    import re
    reg_exp = re.compile(r'(?:youtube\.com\/(?:[^\/]+\/.+\/|(?:v|e(?:mbed)?)\/|.*[?&]v=)|youtu\.be\/)([^"&?\/ ]{11})')
    match = reg_exp.search(url)
    return match.group(1) if match else ''

app.jinja_env.globals.update(get_video_id=get_video_id)

# ---------- Product Feeds ----------
@app.route("/feeds/google-merchant.xml")
def feed_google_merchant():
    products = query("""
        SELECT p.*, b.name as brand_name, c.name as category_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE p.is_active = TRUE
        ORDER BY p.created_at DESC
    """)
    domain = request.host_url.rstrip('/')

    def esc(s):
        if s is None:
            return ''
        return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('"', '&quot;').replace("'", '&apos;'))

    items_xml = []
    for p in products:
        price = float(p['sale_price'] or p['original_price'])
        orig_price = float(p['original_price'])
        availability = 'in_stock' if (p['stock'] or 0) > 0 else 'out_of_stock'

        try:
            bundle = json.loads(p['bundle_info']) if p.get('bundle_info') else {}
        except Exception:
            bundle = {}
        bundle_type = bundle.get('type', 'standard')
        pieces = bundle.get('pieces', [])

        all_images = query("SELECT image_url FROM product_images WHERE product_id=? ORDER BY sort_order", [p['id']])
        extra_images = ''.join(
            f"<g:additional_image_link>{esc(img['image_url'])}</g:additional_image_link>"
            for img in all_images[1:9]
        )

        title = f"{p['name']} - {p['brand_name']}" if p.get('brand_name') else p['name']
        description = (p.get('description') or '').strip()
        if not description:
            bundle_label = bundle_type.replace('_', ' ')
            description = f"Authentic branded {bundle_label} unstitched suit from {p.get('brand_name') or 'ZELO LIVE BOUTIQUE'}. Fabric: {p.get('fabric') or 'Premium'}."

        item = f"""
        <item>
          <g:id>{p['id']}</g:id>
          <title>{esc(title)}</title>
          <description>{esc(description)}</description>
          <link>{domain}/product/{esc(p['slug'])}</link>
          <g:image_link>{esc(p.get('image') or '')}</g:image_link>
          {extra_images}
          <g:availability>{availability}</g:availability>
          <g:price>{orig_price:.2f} PKR</g:price>
          {f"<g:sale_price>{price:.2f} PKR</g:sale_price>" if p['sale_price'] else ""}
          <g:brand>{esc(p.get('brand_name') or 'ZELO LIVE BOUTIQUE')}</g:brand>
          <g:condition>new</g:condition>
          <g:identifier_exists>no</g:identifier_exists>
          <g:mpn>{esc(p['slug'])}</g:mpn>
          <g:product_type>{esc(p.get('category_name') or 'Unstitched Suits')}</g:product_type>
          <g:google_product_category>Apparel &amp; Accessories &gt; Clothing</g:google_product_category>
          <g:custom_label_0>{esc(bundle_type)}</g:custom_label_0>
          <g:custom_label_1>{esc(p.get('fabric') or '')}</g:custom_label_1>
          <g:custom_label_2>{esc(', '.join(pieces))}</g:custom_label_2>
          <g:custom_label_3>{esc(p.get('brand_name') or '')}</g:custom_label_3>
        </item>"""
        items_xml.append(item)

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">
<channel>
<title>ZELO LIVE BOUTIQUE - Product Feed</title>
<link>{domain}</link>
<description>Branded leftover unstitched suits, patches &amp; laces - Pakistan</description>
{''.join(items_xml)}
</channel>
</rss>"""
    return app.response_class(xml, mimetype='application/xml')


@app.route("/feeds/products.csv")
def feed_products_csv():
    import csv
    import io

    products = query("""
        SELECT p.*, b.name as brand_name, c.name as category_name,
               (SELECT image_url FROM product_images WHERE product_id=p.id ORDER BY sort_order LIMIT 1) as image
        FROM products p
        LEFT JOIN brands b ON p.brand_id=b.id
        LEFT JOIN categories c ON p.category_id=c.id
        WHERE p.is_active = TRUE
        ORDER BY p.created_at DESC
    """)
    domain = request.host_url.rstrip('/')

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "id", "title", "description", "availability", "condition", "price", "sale_price",
        "link", "image_link", "brand", "product_type", "bundle_type", "fabric",
        "included_pieces", "stock"
    ])

    for p in products:
        try:
            bundle = json.loads(p['bundle_info']) if p.get('bundle_info') else {}
        except Exception:
            bundle = {}
        writer.writerow([
            p['id'],
            p['name'],
            (p.get('description') or '').replace('\n', ' ').strip(),
            'in stock' if (p['stock'] or 0) > 0 else 'out of stock',
            'new',
            f"{float(p['original_price']):.2f}",
            f"{float(p['sale_price']):.2f}" if p['sale_price'] else '',
            f"{domain}/product/{p['slug']}",
            p.get('image') or '',
            p.get('brand_name') or '',
            p.get('category_name') or '',
            bundle.get('type', 'standard'),
            p.get('fabric') or '',
            ', '.join(bundle.get('pieces', [])),
            p['stock']
        ])

    return app.response_class(output.getvalue(), mimetype='text/csv',
                              headers={"Content-Disposition": "attachment; filename=zelo_product_feed.csv"})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)