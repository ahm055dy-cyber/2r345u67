import json
import os
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request, send_from_directory, redirect, url_for, session, flash
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("NEXO_DB_PATH", BASE_DIR / "nexo_store.db"))
PRODUCTS_PATH = BASE_DIR / "products.json"
COUPON_CODE = os.environ.get("NEXO_COUPON_CODE", "NEXO50").strip().upper()
COUPON_RATE = 0.50
DELIVERY_FEE = 15.0
FREE_SHIPPING_LIMIT = 100.0
WHATSAPP = os.environ.get("NEXO_WHATSAPP", "966501203710")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ORDER_STATUSES = ("جديد", "قيد التجهيز", "تم الشحن", "تم التسليم", "ملغي")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
app.permanent_session_lifetime = __import__("datetime").timedelta(hours=8)


def get_products():
    with PRODUCTS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT UNIQUE NOT NULL,
            customer_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '',
            delivery_method TEXT NOT NULL,
            payment_method TEXT NOT NULL,
            coupon TEXT NOT NULL DEFAULT '',
            original_subtotal REAL NOT NULL,
            subtotal REAL NOT NULL,
            discount REAL NOT NULL,
            delivery_fee REAL NOT NULL,
            total REAL NOT NULL,
            items_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'جديد',
            created_at TEXT NOT NULL
        )""")


@app.get("/")
def index():
    return render_template("index.html", whatsapp=WHATSAPP)


@app.get("/api/products")
def api_products():
    return jsonify(get_products())


@app.post("/api/coupon")
def validate_coupon():
    data = request.get_json(silent=True) or {}
    code = str(data.get("code", "")).strip().upper()
    return jsonify(valid=bool(code and secrets.compare_digest(code, COUPON_CODE)), discount_rate=COUPON_RATE if code and secrets.compare_digest(code, COUPON_CODE) else 0)


def clean_text(value, max_len):
    return re.sub(r"[\x00-\x1f\x7f]", "", str(value or "")).strip()[:max_len]


@app.post("/api/orders")
def create_order():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="بيانات الطلب غير صحيحة"), 400
    name = clean_text(data.get("customerName"), 100)
    phone = clean_text(data.get("phone"), 25)
    address = clean_text(data.get("address"), 400)
    delivery_method = clean_text(data.get("deliveryMethod"), 20)
    payment_method = clean_text(data.get("paymentMethod"), 50)
    coupon = clean_text(data.get("coupon"), 40).upper()
    raw_items = data.get("items")
    if not name or not phone or not re.fullmatch(r"[+0-9()\-\s]{8,25}", phone):
        return jsonify(error="يرجى إدخال الاسم ورقم جوال صحيح"), 400
    if delivery_method not in ("delivery", "pickup"):
        return jsonify(error="طريقة الاستلام غير صحيحة"), 400
    if delivery_method == "delivery" and not address:
        return jsonify(error="عنوان التوصيل مطلوب"), 400
    if not payment_method:
        return jsonify(error="اختر طريقة الدفع"), 400
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 50:
        return jsonify(error="السلة فارغة أو تحتوي على عدد غير صالح من المنتجات"), 400

    catalog = {p["id"]: p for p in get_products()}
    normalized, original_subtotal, subtotal_before_coupon = [], 0.0, 0.0
    for item in raw_items:
        if not isinstance(item, dict):
            return jsonify(error="أحد عناصر السلة غير صحيح"), 400
        product = catalog.get(str(item.get("id", "")))
        try:
            qty = int(item.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0
        if product is None or qty < 1 or qty > 99:
            return jsonify(error="يوجد منتج غير صالح أو كمية غير صحيحة"), 400
        old_price = float(product["oldPrice"])
        price = float(product["price"])
        original_subtotal += old_price * qty
        subtotal_before_coupon += price * qty
        normalized.append({"id": product["id"], "name": product["name"], "qty": qty, "unitPrice": price, "lineTotal": round(price * qty, 2)})

    valid_coupon = bool(coupon and secrets.compare_digest(coupon, COUPON_CODE))
    if coupon and not valid_coupon:
        return jsonify(error="رمز الخصم غير صحيح"), 400
    products_total = round(original_subtotal * (1 - COUPON_RATE), 2) if valid_coupon else round(subtotal_before_coupon, 2)
    discount = round(original_subtotal - products_total, 2) if valid_coupon else 0.0
    delivery_fee = 0.0 if delivery_method == "pickup" or products_total >= FREE_SHIPPING_LIMIT else DELIVERY_FEE
    total = round(products_total + delivery_fee, 2)
    if valid_coupon:
        # Reflect discounted line totals in the WhatsApp order text.
        for item in normalized:
            item["unitPrice"] = round(float(catalog[item["id"]]["oldPrice"]) * (1 - COUPON_RATE), 2)
            item["lineTotal"] = round(item["unitPrice"] * item["qty"], 2)

    order_id = "NEXO-" + secrets.token_hex(4).upper()
    created_at = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute("""INSERT INTO orders(order_id,customer_name,phone,address,delivery_method,payment_method,coupon,
            original_subtotal,subtotal,discount,delivery_fee,total,items_json,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order_id, name, phone, address, delivery_method, payment_method, COUPON_CODE if valid_coupon else "",
             round(original_subtotal, 2), round(products_total, 2), round(discount, 2), round(delivery_fee, 2), total,
             json.dumps(normalized, ensure_ascii=False), "جديد", created_at))
    return jsonify(order={"orderId": order_id, "customerName": name, "phone": phone, "address": address,
        "deliveryMethod": delivery_method, "paymentMethod": payment_method, "coupon": COUPON_CODE if valid_coupon else "",
        "originalSubtotal": round(original_subtotal, 2), "subtotal": round(products_total, 2),
        "productsTotal": products_total, "discount": round(discount, 2), "deliveryFee": round(delivery_fee, 2),
        "total": total, "items": normalized, "status": "جديد", "createdAt": created_at}), 201


@app.get("/admin/login")
def admin_login_page():
    if session.get("nexo_admin"):
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html", password_configured=bool(ADMIN_PASSWORD))


@app.post("/admin/login")
def admin_login():
    password = str(request.form.get("password", ""))
    if not ADMIN_PASSWORD:
        flash("لم تُضبط كلمة مرور الإدارة. أضف ADMIN_PASSWORD في ملف .env ثم أعد تشغيل المتجر.", "error")
        return redirect(url_for("admin_login_page"))
    if secrets.compare_digest(password, ADMIN_PASSWORD):
        session.clear()
        session["nexo_admin"] = True
        session.permanent = True
        return redirect(url_for("admin_dashboard"))
    flash("كلمة المرور غير صحيحة.", "error")
    return redirect(url_for("admin_login_page"))


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login_page"))


def admin_required():
    return bool(session.get("nexo_admin"))


@app.get("/admin")
def admin_dashboard():
    if not admin_required():
        return redirect(url_for("admin_login_page"))
    return render_template("admin.html", statuses=ORDER_STATUSES, whatsapp=WHATSAPP)


@app.get("/api/admin/orders")
def admin_orders():
    if not admin_required():
        return jsonify(error="يلزم تسجيل الدخول"), 401
    with db() as conn:
        rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 500").fetchall()
        summary = conn.execute("""SELECT COUNT(*) AS count,
            COALESCE(SUM(CASE WHEN status != 'ملغي' THEN total ELSE 0 END), 0) AS sales,
            SUM(CASE WHEN status = 'جديد' THEN 1 ELSE 0 END) AS new_count,
            SUM(CASE WHEN status = 'قيد التجهيز' THEN 1 ELSE 0 END) AS preparing_count,
            SUM(CASE WHEN status = 'تم الشحن' THEN 1 ELSE 0 END) AS shipped_count,
            SUM(CASE WHEN status = 'تم التسليم' THEN 1 ELSE 0 END) AS delivered_count,
            SUM(CASE WHEN status = 'ملغي' THEN 1 ELSE 0 END) AS cancelled_count
            FROM orders""").fetchone()
    orders = []
    for row in rows:
        item = dict(row)
        try:
            item["items"] = json.loads(item.pop("items_json"))
        except (TypeError, json.JSONDecodeError):
            item["items"] = []
            item.pop("items_json", None)
        orders.append(item)
    return jsonify(orders=orders, summary={k: (v or 0) for k, v in dict(summary).items()}), 200, {"Cache-Control": "no-store"}


@app.patch("/api/admin/orders/<order_id>/status")
def admin_update_order_status(order_id):
    if not admin_required():
        return jsonify(error="يلزم تسجيل الدخول"), 401
    data = request.get_json(silent=True) or {}
    status = clean_text(data.get("status"), 30)
    if status not in ORDER_STATUSES:
        return jsonify(error="حالة الطلب غير صحيحة"), 400
    with db() as conn:
        cursor = conn.execute("UPDATE orders SET status = ? WHERE order_id = ?", (status, order_id))
        if cursor.rowcount == 0:
            return jsonify(error="لم يتم العثور على الطلب"), 404
    return jsonify(success=True, orderId=order_id, status=status)


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.get("/sw.js")
def service_worker():
    return send_from_directory(app.static_folder, "sw.js", mimetype="application/javascript")


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nAllow: /\n", 200, {"Content-Type": "text/plain; charset=utf-8"}


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=os.environ.get("FLASK_DEBUG") == "1")
