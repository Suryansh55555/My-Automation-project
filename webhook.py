# webhook.py (corrected)
import os
import hmac
import hashlib
import json
import threading
import time
import sqlite3
import csv
import requests
from flask import Flask, request, jsonify, render_template, redirect, url_for, flash
from flask_login import (
    LoginManager, login_user, login_required, logout_user, UserMixin
)
from werkzeug.utils import secure_filename
# Google Sheets libs
import gspread
from oauth2client.service_account import ServiceAccountCredentials
# Razorpay client
from razorpay import Client
import re

def slugify(name):
    """Convert product name to a URL-friendly slug"""
    return re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')

# ------------------ CONFIG ------------------ #
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "supersecretkey") # change in production

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Razorpay
RAZORPAY_KEY_ID = os.getenv("RAZORPAY_KEY_ID", "rzp_test_key")
RAZORPAY_KEY_SECRET = os.getenv("RAZORPAY_KEY_SECRET", "rzp_test_secret")
RZP_WEBHOOK_SECRET = os.getenv("RZP_WEBHOOK_SECRET", "test_secret")

# service account filename used consistently
SERVICE_ACCOUNT_FILE = "google_credentials.json"

# Initialize Razorpay client
razorpay_client = Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

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
DB_FILE = "site.db"

def get_db_connection():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT,
            sizes TEXT,
            price REAL NOT NULL,
            colors TEXT,
            prints TEXT,
            description TEXT,
            image_url TEXT,
            source TEXT DEFAULT 'db'
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sheet_config(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet_id TEXT NOT NULL,
            tab_name TEXT NOT NULL,
            active INTEGER DEFAULT 0
        )"""
    )
    conn.commit()
    conn.close()
def find_product_by_key(product_key):
    """
    Unified lookup:
    - if product_key starts with 'db_' -> treat as DB id (db_<id>)
    - otherwise treat as a slug (look in sheets + db using find_product_by_slug)
    Returns product dict or None.
    """
    key = str(product_key or "").strip()

    # 1) db_<id> special-case -> lookup DB by id
    if key.startswith("db_"):
        try:
            pid = int(key.replace("db_", ""))
        except ValueError:
            return None

        conn = get_db_connection()
        row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        conn.close()

        if not row:
            return None

        p = dict(row)
        # normalize fields used by templates
        p["slug"] = key
        # ✅ fix: use external placeholder, not local /static file
        p["image_url"] = (
            p.get("image_url")
            or "https://via.placeholder.com/300x300.png?text=No+Image"
        )
        # sizes stored as comma-separated string in DB; convert to list
        p["sizes"] = [s.strip() for s in (p.get("sizes") or "").split(",") if s.strip()]
        return p

    # 2) Otherwise try slug lookup from sheets and DB using the existing function
    return find_product_by_slug(key)


# ------------------ HELPERS ------------------ #
def send_telegram_message(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(url, data=data, timeout=5)
    except Exception as e:
        print("Telegram error:", e)

def normalize_prices_in_db():
    conn = get_db_connection()
    rows = conn.execute("SELECT id, price FROM products").fetchall()
    converted = 0
    for r in rows:
        try:
            p = float(r["price"])
        except Exception:
            continue
        if p >= 10000 and abs(round(p) % 100) == 0:
            new_price = p / 100.0
            conn.execute("UPDATE products SET price=? WHERE id=?", (new_price, r["id"]))
            converted += 1
    if converted > 0:
        print(f"Normalized {converted} prices from paise -> rupees in the DB.")
    conn.commit()
    conn.close()

# --------------- Sheet cache & client helpers ---------------
# TTL in seconds (default 300s = 5 minutes). You can override with env var SHEET_CACHE_TTL.
CACHE_TTL = int(os.getenv("SHEET_CACHE_TTL", "300"))

# in-memory caches + locks
SHEET_CACHE = {}  # key -> {"ts": float, "data": list}
SHEET_CACHE_LOCK = threading.Lock()
GSPREAD_CLIENT = None
GSPREAD_CLIENT_LOCK = threading.Lock()


def get_gspread_client():
    """
    Return a cached gspread client.
    Works with either:
    1) Uploaded JSON file (SERVICE_ACCOUNT_FILE)
    2) GOOGLE_CREDENTIALS env var containing JSON string
    """
    global GSPREAD_CLIENT
    if GSPREAD_CLIENT:
        return GSPREAD_CLIENT

    with GSPREAD_CLIENT_LOCK:
        if GSPREAD_CLIENT:  # double-check after lock
            return GSPREAD_CLIENT

        try:
            scope = ["https://spreadsheets.google.com/feeds",
                     "https://www.googleapis.com/auth/drive"]

            # Try env var first
            creds_json = os.environ.get("GOOGLE_CREDENTIALS")
            if creds_json:
                creds_dict = json.loads(creds_json)
                creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
                print("[INFO] gspread client created from GOOGLE_CREDENTIALS env var")
            else:
                # fallback to local JSON file
                if not os.path.exists(SERVICE_ACCOUNT_FILE):
                    print(f"[ERROR] Service account file not found: {SERVICE_ACCOUNT_FILE}")
                    return None
                creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
                print(f"[INFO] gspread client created from file: {SERVICE_ACCOUNT_FILE}")

            client = gspread.authorize(creds)
            GSPREAD_CLIENT = client
            return client

        except Exception as e:
            print("[ERROR] gspread auth failed:", e)
            return None


def get_sheet_records(sheet_id, tab_name):
    """Return cached sheet records if fresh, otherwise fetch and cache."""
    key = f"{sheet_id}::{tab_name}"
    now = time.time()

    # check cache
    with SHEET_CACHE_LOCK:
        entry = SHEET_CACHE.get(key)
        if entry and (now - entry["ts"] < CACHE_TTL):
            return entry["data"]

    # not cached or expired — fetch
    client = get_gspread_client()
    if not client:
        print(f"[ERROR] Cannot authenticate Google Sheets client for sheet {sheet_id}")
        return []  # still return empty, but log

    try:
        sh = client.open_by_key(sheet_id)
        ws = sh.worksheet(tab_name)
        data = ws.get_all_records() or []
        if not data:
            print(f"[INFO] Sheet {sheet_id} tab '{tab_name}' fetched 0 rows")
        else:
            print(f"[INFO] Sheet {sheet_id} tab '{tab_name}' fetched {len(data)} rows")

        with SHEET_CACHE_LOCK:
            SHEET_CACHE[key] = {"ts": now, "data": data}
        return data

    except Exception as e:
        print(f"[ERROR] Exception fetching sheet {sheet_id} tab '{tab_name}': {e}")
        return []



# ------------------ GOOGLE SHEETS SYNC ------------------ #
def sync_products_from_sheet():
    print("Fetching rows from Google Sheet...")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
    client = gspread.authorize(creds)
    SHEET_ID = os.getenv("SHEET_ID", "")
    TAB_NAME = os.getenv("SHEET_TAB", "")
    if not SHEET_ID or not TAB_NAME:
        print("SHEET_ID or SHEET_TAB not set in env; aborting sync.")
        return
    try:
        sh = client.open_by_key(SHEET_ID)
    except Exception as e:
        print("Error opening sheet for sync:", e)
        return
    print("Available tabs:", [ws.title for ws in sh.worksheets()])
    try:
        sheet = sh.worksheet(TAB_NAME)
    except Exception as e:
        print("Tab not found for sync:", e)
        return
    all_rows = sheet.get_all_values()
    if not all_rows or len(all_rows) < 2:
        print("No data found in the sheet/tab.")
        return
    headers = [h.strip() for h in all_rows[0]]
    data_rows = all_rows[1:]
    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE source='sheet'")
    seen = set([r['name'].strip().lower() for r in conn.execute("SELECT name FROM products").fetchall()])
    inserted = 0
    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue
        row_dict = dict(zip(headers, row))
        name = (row_dict.get("Product Type") or "").strip()
        if not name:
            continue
        n = name.lower()
        if n in seen:
            continue
        seen.add(n)
        price_raw = str(row_dict.get("Price") or "0").replace("₹", "").strip()
        try:
            price = float(price_raw)
        except:
            price = 0.0
        size = row_dict.get("Product Size") or ""
        colors = row_dict.get("Color Variants") or ""
        prints = row_dict.get("Print Variants") or ""
        description = row_dict.get("Description") or ""
        full_description = f"{description}\nSizes: {size}\nColors: {colors}\nPrints: {prints}"
        image_url = row_dict.get("Image Link") or None
        conn.execute(
            "INSERT INTO products (name, price, description, image_url, source) VALUES (?,?,?,?,?)",
            (name, price, full_description, image_url, "sheet")
        )
        inserted += 1
    conn.commit()
    conn.close()
    print(f"✅ Synced {inserted} products from Google Sheet")

# --------- Google Sheets helper (consistent credentials) ---------
def get_sheet_tabs(sheet_id):
    """Return all worksheet names for a given sheet ID using GOOGLE_CREDENTIALS env var"""
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        creds_json = os.getenv("GOOGLE_CREDENTIALS")
        if not creds_json:
            print("❌ GOOGLE_CREDENTIALS not set in environment")
            return []

        creds_dict = json.loads(creds_json)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)

        sh = client.open_by_key(sheet_id)
        return [ws.title for ws in sh.worksheets()]
    except Exception as e:
        print(f"❌ Error fetching tabs for sheet {sheet_id}: {e}")
        return []


# ------------------ ROUTES ------------------ #
@app.route("/")
def home():
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
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

@app.route("/admin")
@login_required
def admin_dashboard():
    conn = get_db_connection()
    payments = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    total_revenue = sum(row["amount"] for row in payments)
    return render_template(
        "admin_dashboard.html",
        payments=payments,
        total_revenue=total_revenue,
        total_orders=len(payments),
        last_payment_amount=payments[0]["amount"] if payments else 0
    )

# ------------------------------
# Admin products (manual + sheets)
# ------------------------------
# ------------------------------
# Admin products: view + add manually
# ------------------------------
@app.route("/admin/products", methods=["GET", "POST"])
@login_required
def admin_products():
    conn = get_db_connection()
    # ----- Manual Add -----
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        type_ = (request.form.get("type") or "").strip()
        sizes = (request.form.get("sizes") or "").strip()
        price_raw = request.form.get("price") or "0"
        colors = (request.form.get("colors") or "").strip()
        prints = (request.form.get("prints") or "").strip()
        description = (request.form.get("description") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()
        try:
            price_val = float(price_raw)
        except Exception:
            price_val = 0.0
        conn.execute(
            """INSERT INTO products (name, type, sizes, price, colors, prints, description, image_url, source)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (name, type_, sizes, price_val, colors, prints, description, image_url, "db")
        )
        conn.commit()
        flash("Product added successfully!", "success")
        return redirect(url_for("admin_products"))
    # ----- Fetch DB products -----
    db_products = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    # ----- Fetch Google Sheets products -----
    sheets_products = []
    try:
        sheets_data = fetch_sheets_data() # {tab_name: [products]}
        for tab_name, products in sheets_data.items():
            for p in products:
                try:
                    price_val = float(str(p.get("price") or "0").replace("₹","").split()[0])
                except:
                    price_val = 0.0
                sheets_products.append({
                    "id": f"sheet_{tab_name}_{p.get('name','unknown')}", # pseudo-ID
                    "name": p.get("name","Unknown"),
                    "type": p.get("type","-"),
                    "sizes": p.get("sizes","-"),
                    "price": price_val,
                    "colors": p.get("colors","-"),
                    "prints": p.get("prints","-"),
                    "description": p.get("description","-"),
                    "image_url": p.get("image_url",""),
                    "source": f"sheet:{tab_name}"
                })
    except Exception as e:
        flash(f"Failed to fetch sheets: {e}", "danger")
    # ----- Combine DB + Sheets -----
    all_products = list(db_products) + sheets_products
    conn.close()
    return render_template("admin_products.html", products=all_products)

# ------------------------------
# Edit product (DB only)
# ------------------------------
@app.route("/admin/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
def edit_product(product_id):
    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        conn.close()
        return "Product not found", 404
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        type_ = (request.form.get("type") or "").strip()
        sizes = (request.form.get("sizes") or "").strip()
        price_raw = request.form.get("price") or "0"
        colors = (request.form.get("colors") or "").strip()
        prints = (request.form.get("prints") or "").strip()
        description = (request.form.get("description") or "").strip()
        image_url = (request.form.get("image_url") or "").strip()
        try:
            price_val = float(price_raw)
        except:
            price_val = 0.0
        conn.execute(
            """UPDATE products SET name=?, type=?, sizes=?, price=?, colors=?, prints=?, description=?, image_url=? WHERE id=?""",
            (name, type_, sizes, price_val, colors, prints, description, image_url, product_id)
        )
        conn.commit()
        conn.close()
        flash("Product updated successfully!", "success")
        return redirect(url_for("admin_products"))
    conn.close()
    return render_template("edit_product.html", product=product)

# ------------------------------
# Delete product (DB only)
# ------------------------------
@app.route("/admin/products/delete/<int:product_id>", methods=["GET", "POST"])
@login_required
def delete_product(product_id):
    conn = get_db_connection()
    conn.execute("DELETE FROM products WHERE id=?", (product_id,))
    conn.commit()
    conn.close()
    flash("Product deleted successfully!", "success")
    return redirect(url_for("admin_products"))

# ------------------------------
# Sync products from Google Sheets
# ------------------------------
@app.route("/admin/sync_products", methods=["POST"])
@login_required
def sync_products():
    try:
        fetch_sheets_data() # just fetch to show in admin
        flash("Products synced from Google Sheets successfully!", "success")
    except Exception as e:
        flash(f"Failed to sync products: {e}", "danger")
    return redirect(url_for("admin_products"))

# --------- Manage Sheets (fixed to support multiple active tabs) ---------
@app.route("/admin/sheets", methods=["GET", "POST"])
@login_required
def manage_sheets():
    conn = get_db_connection()
    cur = conn.cursor()
    if request.method == "POST":
        sheet_id = request.form.get("sheet_id", "").strip()
        selected_tabs = request.form.getlist("tabs") # multiple selection possible
        if sheet_id:
            # Fetch existing entries for this sheet
            existing_rows = cur.execute("SELECT * FROM sheet_config WHERE sheet_id=?", (sheet_id,)).fetchall()
            existing_tab_names = [r["tab_name"] for r in existing_rows]
            # Update existing rows: activate if selected, deactivate if not selected
            for r in existing_rows:
                if r["tab_name"] in selected_tabs:
                    cur.execute("UPDATE sheet_config SET active=1 WHERE id=?", (r["id"],))
                else:
                    cur.execute("UPDATE sheet_config SET active=0 WHERE id=?", (r["id"],))
            # Insert any newly selected tabs that are not yet in DB
            for tab in selected_tabs:
                if tab not in existing_tab_names:
                    cur.execute(
                        "INSERT INTO sheet_config (sheet_id, tab_name, active) VALUES (?, ?, 1)", (sheet_id, tab)
                    )
        conn.commit()
        conn.close()
        return redirect(url_for("manage_sheets"))
    # GET: fetch all saved sheet rows for display
    sheets = cur.execute("SELECT * FROM sheet_config").fetchall()
    # Build a mapping sheet_id -> list of active tab names for the template (helps preselect)
    active_map = {}
    for s in sheets:
        if s["active"] == 1:
            active_map.setdefault(s["sheet_id"], []).append(s["tab_name"])
    conn.close()
    # render template with 'sheets' and 'active_map'
    return render_template("manage_sheets.html", sheets=sheets, active_map=active_map)

# --------- AJAX: Fetch tabs dynamically (no login redirect issues) ---------
@app.route("/get_tabs", methods=["POST", "GET"])
def get_tabs():
    sheet_id = request.values.get("sheet_id", "").strip()
    if not sheet_id:
        return jsonify({"tabs": [], "active": []})
    tabs = get_sheet_tabs(sheet_id)
    # Also fetch currently active tabs (if any) for this sheet from DB
    conn = get_db_connection()
    rows = conn.execute("SELECT tab_name FROM sheet_config WHERE sheet_id=? AND active=1", (sheet_id,)).fetchall()
    conn.close()
    active = [r["tab_name"] for r in rows]
    return jsonify({"tabs": tabs, "active": active})

# --------- Store (fetches from all active sheets + DB) ---------
@app.route("/store")
def store():
    sheets_data = {}   # tab_name -> list of product dicts

    # load active tabs from DB and manual products in a single connection
    conn = get_db_connection()
    active_rows = conn.execute("SELECT sheet_id, tab_name FROM sheet_config WHERE active=1").fetchall()
    db_products = conn.execute("SELECT * FROM products").fetchall()
    conn.close()

    # prepare manual products (fast)
    manual_products = []
    for p in db_products:
        manual_products.append({
            "id": p["id"],
            "slug": slugify(p["name"]),
            "name": p["name"],
            "price": p["price"],
            "image_url": p["image_url"] or "https://via.placeholder.com/300x300.png?text=No+Image",

            "description": p["description"] or "No description available",
            "sizes": [p["sizes"]] if p["sizes"] else [],
            "colors": "",   # no column in DB
            "prints": ""    # no column in DB
        })

    # if no sheets configured, return quickly
    if not active_rows:
        return render_template(
            "store.html",
            sheets_data={},
            db_products=manual_products,
            razorpay_key=RAZORPAY_KEY_ID
        )

    # For each active sheet/tab use the cached fetch function
    for row in active_rows:
        sheet_id = row["sheet_id"]
        tab_name = row["tab_name"]
        try:
            raw = get_sheet_records(sheet_id, tab_name)  # fast when cached
            products_dict = {}  # aggregate by product name

            for rec in raw:
                name = (rec.get("Product Type") or rec.get("Product") or "").strip()
                if not name:
                    continue

                price_raw = str(rec.get("Price") or "").replace("₹", "").replace(",", "").strip()
                try:
                    price = float(price_raw) if price_raw else 0.0
                except ValueError:
                    import re
                    digits = re.sub(r"[^\d.]", "", price_raw)
                    price = float(digits) if digits else 0.0

                size = (rec.get("Product Size") or "").strip()
                colors = (rec.get("Color Variants") or "").strip()
                prints = (rec.get("Print Variants") or "").strip()
                image_url = (rec.get("Image Link") or "").strip() or "https://via.placeholder.com/300x300.png?text=No+Image"
                description = (rec.get("Description") or "").strip()

                if name in products_dict:
                    if size and size not in products_dict[name]["sizes"]:
                        products_dict[name]["sizes"].append(size)
                else:
                    products_dict[name] = {
                        "id": len(products_dict) + 1,
                        "slug": slugify(name),
                        "name": name,
                        "price": price,
                        "image_url": image_url,
                        "description": description,
                        "sizes": [size] if size else [],
                        "colors": colors,
                        "prints": prints
                    }

            sheets_data[tab_name] = list(products_dict.values())

        except Exception as e:
            # keep the same behavior: log error and show empty list for this tab
            print(f"Error processing sheet {sheet_id} tab {tab_name}: {e}")
            sheets_data[tab_name] = []

    return render_template(
        "store.html",
        sheets_data=sheets_data,
        db_products=manual_products,
        razorpay_key=RAZORPAY_KEY_ID
    )


# --------- Product Detail (adjusted to show all sizes) ---------


def find_product_by_key(product_key):
    """
    Lookup product by key from:
    1) Manual DB products
    2) Active Google Sheets tabs
    Returns a dict with normalized keys or None
    """
    # --- 1. Check manual DB products ---
    conn = get_db_connection()
    db_rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()

    for p in db_rows:
        slug = slugify(p["name"])
        if product_key == slug or product_key == f"db_{p['id']}":
            return {
                "id": p["id"],
                "name": p["name"],
                "slug": slug,
                "price": p["price"],
                "image_url": p["image_url"] or "https://via.placeholder.com/300x300.png?text=No+Image",
                "description": p["description"] or "No description available",
                "sizes": [p["sizes"]] if p["sizes"] else [],
                "colors": "",
                "prints": ""
            }

    # --- 2. Check Sheets products ---
    conn = get_db_connection()
    active_rows = conn.execute("SELECT sheet_id, tab_name FROM sheet_config WHERE active=1").fetchall()
    conn.close()

    for row in active_rows:
        sheet_data = get_sheet_records(row["sheet_id"], row["tab_name"])
        for rec in sheet_data:
            name = (rec.get("Product Type") or rec.get("Product") or "").strip()
            if not name:
                continue
            slug = slugify(name)
            if slug == product_key:
                size = (rec.get("Product Size") or "").strip()
                colors = (rec.get("Color Variants") or "").strip()
                prints = (rec.get("Print Variants") or "").strip()
                image_url = (rec.get("Image Link") or "").strip() or "https://via.placeholder.com/300x300.png?text=No+Image"
                description = (rec.get("Description") or "").strip() or "No description available"
                price_raw = str(rec.get("Price") or "").replace("₹", "").replace(",", "").strip()
                try:
                    price = float(price_raw) if price_raw else 0.0
                except ValueError:
                    import re
                    digits = re.sub(r"[^\d.]", "", price_raw)
                    price = float(digits) if digits else 0.0

                return {
                    "id": None,
                    "name": name,
                    "slug": slug,
                    "price": price,
                    "image_url": image_url,
                    "description": description,
                    "sizes": [size] if size else [],
                    "colors": colors,
                    "prints": prints
                }

    # Not found
    return None


# --- Product detail route ---
@app.route("/product/<product_key>")
def product_detail(product_key):
    product = find_product_by_key(product_key)
    if not product:
        return "Product not found", 404

    # Normalize keys
    product.setdefault("slug", product_key)
    product.setdefault("image_url", "https://via.placeholder.com/300x300.png?text=No+Image")
    product.setdefault("description", "No description available")
    product.setdefault("sizes", [])

    return render_template("product_detail.html", product=product)



# CSV upload
@app.route("/admin/upload_csv", methods=["POST"])
@login_required
def upload_csv():
    file = request.files.get("file")
    if not file:
        return "No file uploaded", 400
    filename = secure_filename(file.filename)
    if not filename.endswith(".csv"):
        return "Only CSV files are allowed", 400
    stream = file.stream.read().decode("utf-8").splitlines()
    reader = csv.DictReader(stream)
    conn = get_db_connection()
    seen = set([r['name'].strip().lower() for r in conn.execute("SELECT name FROM products").fetchall()])
    for row in reader:
        name = row.get("Product Type") or row.get("name") or ""
        norm = name.lower()
        if norm in seen or not name:
            continue
        seen.add(norm)
        price_raw = row.get("Price", "0").replace("₹", "").strip()
        try:
            price = float(price_raw)
        except:
            price = 0.0
        description = row.get("Description", "")
        image_url = row.get("Image Link") or None
        conn.execute(
            "INSERT INTO products (name, price, description, image_url, source) VALUES (?,?,?,?,?)",
            (name, price, description, image_url, "csv")
        )
    conn.commit()
    conn.close()
    return redirect(url_for("admin_products"))

@app.route("/admin/payments")
@login_required
def admin_payments():
    conn = get_db_connection()
    payments = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("admin_payments.html", payments=payments)

# Delete all products
@app.route('/delete_all_products', methods=['POST'])
@login_required
def delete_all_products():
    try:
        conn = get_db_connection()
        conn.execute("DELETE FROM products")
        conn.commit()
        conn.close()
        flash("✅ All products deleted successfully!", "success")
    except Exception as e:
        flash(f"❌ Error deleting products: {e}", "danger")
    return redirect(url_for("admin_products"))

# Razorpay webhook
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
    description = pay.get("description", "")
    safe_description = description[:255]
    conn = get_db_connection()
    conn.execute(
        "INSERT INTO orders(payment_id,order_id,status,amount,currency,raw_payload) VALUES (?,?,?,?,?,?)",
        (pid, oid, status, amount_inr, pay.get("currency", "INR"), json.dumps(data)),
    )
    conn.commit()
    conn.close()
    msg = (
        f"*Razorpay Payment Alert!*\n\n"
        f"📌 Event: {data.get('event')}\n"
        f"🆔 Payment ID: {pid}\n"
        f"🛍️ Order ID: {oid or 'Not Linked'}\n"
        f"💰 Amount: ₹{amount_inr:.2f} INR\n"
        f"✅ Status: *{status.upper()}*\n"
        f"📝 Description: {safe_description}"
    )
    send_telegram_message(msg)
    return jsonify({"ok": verified})

def get_product_by_id(product_id):
    # Try Google Sheets first
    conn = get_db_connection()
    active_rows = conn.execute("SELECT sheet_id, tab_name FROM sheet_config WHERE active=1").fetchall()
    conn.close()
    if active_rows:
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
            client = gspread.authorize(creds)
            for row in active_rows:
                sheet_id = row["sheet_id"]
                tab_name = row["tab_name"]
                try:
                    sh = client.open_by_key(sheet_id)
                    ws = sh.worksheet(tab_name)
                    raw = ws.get_all_records()
                    for idx, rec in enumerate(raw, start=1):
                        if idx == product_id:
                            return {
                                "id": idx,
                                "name": (rec.get("Product Type") or rec.get("Product") or "").strip(),
                                "price": float(str(rec.get("Price") or "0").replace("₹","").replace(",","") or 0),
                                "description": (rec.get("Description") or "").strip()
                            }
                except Exception as e:
                    print(f"Error loading tab {tab_name}: {e}")
        except Exception as e:
            print("Sheets fetch error:", e)
    # Fallback to DB
    conn = get_db_connection()
    product = conn.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
    conn.close()
    return dict(product) if product else None

@app.route("/create_order/<product_key>", methods=["POST"])
def create_order(product_key):
    # Lookup product (DB or Google Sheets)
    product = find_product_by_key(product_key)
    if not product:
        return jsonify({"error": "Product not found"}), 404

    # Safe description (limit 255 chars for Razorpay)
    safe_description = (product.get("description") or product.get("name") or "No description")[:255]

    # Validate and parse price
    try:
        product_price = float(product.get("price", 0))
        if product_price <= 0:
            return jsonify({"error": "Product price must be greater than 0"}), 400
    except Exception as e:
        return jsonify({"error": f"Invalid product price: {e}"}), 500

    # Amount in paise (Razorpay expects INR in paise)
    amount_paise = int(round(product_price * 100))

    # Razorpay order data
    order_data = {
        "amount": amount_paise,
        "currency": "INR",
        "receipt": f"order_{str(product_key)[:30]}",  # Razorpay receipt max length = 40
        "payment_capture": 1,
        "notes": {"description": safe_description}
    }

    try:
        razorpay_order = razorpay_client.order.create(data=order_data)
    except Exception as e:
        return jsonify({"error": f"Razorpay order creation failed: {e}"}), 500

    # --- Telegram message for ALL products (manual + sheets) ---
    msg = (
        f"💰 *New Order Created*\n"
        f"📦 Product: {product.get('name', 'Unknown')}\n"
        f"💵 Price: ₹{product_price:.2f}\n"
        f"🗂 Type: {'Manual/DB' if product_key.startswith('db_') else 'Google Sheets'}\n"
        f"🆔 Order ID: {razorpay_order.get('id')}"
    )
    send_telegram_message(msg)

    return jsonify({
        "order_id": razorpay_order.get("id"),
        "amount": amount_paise,
        "currency": "INR",
        "product_name": product.get("name", "Unknown"),
        "description": safe_description,
        "key": RAZORPAY_KEY_ID
    })


@app.route("/admin/clear_history", methods=["POST"])
@login_required
def clear_history():
    conn = get_db_connection()
    conn.execute("DELETE FROM orders")
    conn.commit()
    conn.close()
    return redirect(url_for("admin_dashboard"))

def find_product_by_slug(slug):
    slug = slug.lower()
    # First check Google Sheets aggregated products
    conn = get_db_connection()
    active_rows = conn.execute("SELECT sheet_id, tab_name FROM sheet_config WHERE active=1").fetchall()
    conn.close()
    if active_rows:
        try:
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_FILE, scope)
            client = gspread.authorize(creds)
            for row in active_rows:
                sh = client.open_by_key(row["sheet_id"])
                ws = sh.worksheet(row["tab_name"])
                raw = ws.get_all_records()
                for rec in raw:
                    name = (rec.get("Product Type") or rec.get("Product") or "").strip()
                    if slugify(name) == slug:
                        price_raw = str(rec.get("Price") or "0").replace("₹","").replace(",","").strip()
                        try:
                            price = float(price_raw)
                        except:
                            price = 0.0
                        size = (rec.get("Product Size") or "").strip()
                        colors = (rec.get("Color Variants") or "").strip()
                        prints = (rec.get("Print Variants") or "").strip()
                        image_url = (rec.get("Image Link") or "").strip()
                        description = (rec.get("Description") or "").strip()
                        return {
                            "slug": slug,
                            "name": name,
                            "price": price,
                            "image_url": image_url,
                            "description": description,
                            "sizes": [size] if size else [],
                            "colors": colors,
                            "prints": prints
                        }
        except:
            pass
    # Fallback DB
    conn = get_db_connection()
    rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()
    for p in rows:
        if slugify(p['name']) == slug:
            p = dict(p)
            p['sizes'] = [] # DB has no sizes
            p['slug'] = slug
            return p
    return None

# ------------------ STARTUP ------------------ #
if __name__ == "__main__":
    init_db()
    try:
        normalize_prices_in_db()
    except Exception as e:
        print("Price normalization failed:", e)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
