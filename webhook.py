iimport os
import hmac
import hashlib
import json
import sqlite3
from flask import Flask, request, jsonify, render_template, render_template_string, redirect, url_for, flash
from flask_login import (
    LoginManager,
    login_user,
    login_required,
    logout_user,
    UserMixin,
    current_user,
)
import requests
import csv
from werkzeug.utils import secure_filename

# ------------------ CONFIG ------------------ #
app = Flask(__name__)
app.secret_key = "supersecretkey"  # ⚠️ change in production

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "your_bot_token_here")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "your_chat_id_here")

# Razorpay
RZP_WEBHOOK_SECRET = os.getenv("RZP_WEBHOOK_SECRET", "test_secret")

# ------------------ LOGIN ------------------ #
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"

class User(UserMixin):
    def __init__(self, id):
        self.id = id

@login_manager.user_loader
def load_user(user_id):
    return User(user_id)

# ------------------ DATABASE ------------------ #
def get_db_connection():
    conn = sqlite3.connect("site.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()

    # Orders table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT,
            order_id TEXT,
            status TEXT,
            amount REAL,
            currency TEXT,
            raw_payload TEXT
        )"""
    )

    # ✅ Products table
    conn.execute(
        """CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            description TEXT,
            image_url TEXT
        )"""
    )

    conn.commit()
    conn.close()

init_db()

# ------------------ HELPERS ------------------ #
def send_telegram_message(msg: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("Telegram error:", e)

# ------------------ ROUTES ------------------ #
@app.route("/")
def home():
    return redirect(url_for("login"))

# ---- LOGIN ----
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]

        # Hardcoded login
        if username == "admin" and password == "admin123":
            user = User(id=1)
            login_user(user)
            return redirect(url_for("admin_dashboard"))
        else:
            return render_template("login.html", error="Invalid login credentials")

    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))

# ---- DASHBOARD ----
@app.route("/admin")
@login_required
def admin_dashboard():
    conn = get_db_connection()
    payments = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()

    total_revenue = sum(row["amount"] for row in payments)
    total_orders = len(payments)
    last_payment_amount = payments[0]["amount"] if payments else 0

    conn.close()

    return render_template(
        "admin_dashboard.html",
        payments=payments,
        total_revenue=total_revenue,
        total_orders=total_orders,
        last_payment_amount=last_payment_amount
    )

@app.route("/admin/clear_history", methods=["POST"])
@login_required
def clear_history():
    conn = get_db_connection()
    conn.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))

# ---- PRODUCTS ----
@app.route("/admin/products", methods=["GET", "POST"])
@login_required
def admin_products():
    conn = get_db_connection()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        price_raw = request.form.get("price") or "0"
        description = (request.form.get("description") or "").strip()
        image_url = (request.form.get("image_url") or "").strip() if "image_url" in request.form else None

        try:
            price_val = float(price_raw)
        except Exception:
            price_val = 0.0

        conn.execute(
            "INSERT INTO products (name, price, description, image_url) VALUES (?,?,?,?)",
            (name, price_val, description, image_url)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_products"))

    rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_products.html", products=rows)

@app.route("/admin/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    conn = get_db_connection()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        price_raw = request.form.get("price") or "0"
        description = (request.form.get("description") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()

        try:
            price_val = float(price_raw)
        except Exception:
            price_val = 0.0

        conn.execute(
            "UPDATE products SET name=?, price=?, description=?, image_url=? WHERE id=?",
            (name, price_val, description, image_url, product_id)
        )
        conn.commit()
        conn.close()
        return redirect(url_for("admin_products"))

    r = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    conn.close()
    if not r:
        return "Product not found", 404

    edit_html = """
    {% extends "base.html" %}
    {% block content %}
    <div class="container mt-4">
      <h2>Edit Product</h2>
      <form method="POST" action="{{ url_for('edit_product', product_id=product['id']) }}">
        <div class="mb-3">
          <label class="form-label">Product Name</label>
          <input type="text" class="form-control" name="name" value="{{ product['name'] }}" required>
        </div>
        <div class="mb-3">
          <label class="form-label">Price (₹)</label>
          <input type="number" class="form-control" name="price" step="0.01" value="{{ product['price'] }}" required>
        </div>
        <div class="mb-3">
          <label class="form-label">Description</label>
          <textarea class="form-control" name="description" rows="3">{{ product['description'] }}</textarea>
        </div>
        <div class="mb-3">
          <label class="form-label">Image URL (optional)</label>
          <input type="text" class="form-control" name="image_url" value="{{ product['image_url'] or '' }}">
        </div>
        <button class="btn btn-primary">Save</button>
        <a class="btn btn-secondary" href="{{ url_for('admin_products') }}">Cancel</a>
      </form>
    </div>
    {% endblock %}
    """
    return render_template_string(edit_html, product=dict(r))

@app.route("/admin/products/delete/<int:product_id>", methods=["GET", "POST"])
@login_required
def delete_product(product_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_products"))

# ---- ORDERS & PAYMENTS ----
@app.route("/admin/orders")
@login_required
def admin_orders():
    conn = get_db_connection()
    orders = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_orders.html", orders=orders)

@app.route("/admin/payments")
@login_required
def admin_payments():
    conn = get_db_connection()
    payments = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_payments.html", payments=payments)

# ---- RAZORPAY WEBHOOK ----
@app.route("/razorpay_webhook", methods=["POST"])
def razorpay_webhook():
    body = request.data
    sig = request.headers.get("X-Razorpay-Signature")

    if not sig:
        return "Missing signature", 400

    exp = hmac.new(RZP_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    verified = hmac.compare_digest(exp, sig)

    data = request.get_json()
    pay = data.get("payload", {}).get("payment", {}).get("entity", {})

    amount_paise = pay.get("amount", 0)
    amount_inr = amount_paise / 100.0
    pid = pay.get("id")
    oid = pay.get("order_id")
    status = pay.get("status", "unknown")

    conn = get_db_connection()
    conn.execute(
        "INSERT INTO orders(payment_id,order_id,status,amount,currency,raw_payload) VALUES (?,?,?,?,?,?)",
        (pid, oid, status, amount_inr, pay.get("currency", "INR"), json.dumps(data)),
    )
    conn.commit()
    conn.close()

    msg = (
        f"*Razorpay Payment Alert!*\n\n"
        f"📌 Event: `{data.get('event')}`\n"
        f"🆔 Payment ID: `{pid}`\n"
        f"🛍️ Order ID: `{oid or 'Not Linked'}`\n"
        f"💰 Amount: ₹{amount_inr:.2f} INR\n"
        f"✅ Status: *{status.upper()}*"
    )
    send_telegram_message(msg)

    return jsonify({"ok": verified})

# ---- STORE PAGE ----
@app.route("/store")
def store():
    conn = get_db_connection()
    products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("store.html", products=products)

# ---- BULK UPLOAD ----
@app.route("/admin/upload_csv", methods=["POST"])
@login_required
def upload_csv():
    file = request.files.get("file")
    if not file:
        return "No file uploaded", 400

    filename = secure_filename(file.filename)
    if not filename.endswith(".csv"):
        return "Only CSV files are allowed", 400

    # Read CSV
    stream = file.stream.read().decode("utf-8").splitlines()
    reader = csv.DictReader(stream)

    conn = get_db_connection()
    for row in reader:
        name = row.get("Product Type") or row.get("name") or ""
        price_raw = row.get("Price", "0").replace("₹", "").strip()
        try:
            price = float(price_raw)
        except:
            price = 0.0
        description = row.get("Description", "")
        image_url = row.get("Image Link") or None

        conn.execute(
            "INSERT INTO products (name, price, description, image_url) VALUES (?,?,?,?)",
            (name, price, description, image_url)
        )
    conn.commit()
    conn.close()

    return redirect(url_for("admin_products"))

# ---- PRODUCT DETAIL ----
@app.route("/product/<int:product_id>")
def product_detail(product_id):
    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    conn.close()

    if not product:
        return "Product not found", 404

    return render_template("product_detail.html", product=product)

# ---- DELETE ALL PRODUCTS ----
@app.route('/delete_all_products', methods=['POST'])
@login_required
def delete_all_products():
    try:
        conn = get_db_connection()   # ✅ fixed (site.db, not products.db)
        conn.execute("DELETE FROM products")
        conn.commit()
        conn.close()
        flash("✅ All products deleted successfully!", "success")
    except Exception as e:
        flash(f"❌ Error deleting products: {e}", "danger")

    return redirect(url_for("admin_products"))

# ------------------ MAIN ------------------ #
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
