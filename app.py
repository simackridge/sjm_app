from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    Response,
    send_file,
    abort,
    after_this_request,
)
from datetime import datetime, UTC, timedelta
import os
import re
import secrets
from functools import wraps
import csv
import io
import base64
import tempfile

from dotenv import load_dotenv
import resend
import stripe
import psycopg2
from psycopg2.extras import RealDictCursor

# -----------------------------------------------------------------------------
# INIT
# -----------------------------------------------------------------------------

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this")

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL")
resend.api_key = os.environ.get("RESEND_API_KEY")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")

STRIPE_PRICES = {
    "Essential": os.environ.get("STRIPE_PRICE_ESSENTIAL"),
    "Standard": os.environ.get("STRIPE_PRICE_STANDARD"),
    "Complete": os.environ.get("STRIPE_PRICE_COMPLETE"),
}

STRIPE_SUCCESS_URL = os.environ.get("STRIPE_SUCCESS_URL")
STRIPE_CANCEL_URL = os.environ.get("STRIPE_CANCEL_URL")

DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_HOST = os.environ.get("DB_HOST")
DB_PORT = os.environ.get("DB_PORT")

PLAN_PRICES = {
    "Essential": "11.00",
    "Standard": "17.99",
    "Complete": "23.99",
}

VALID_STATUSES = {"New", "Contacted", "Won", "Lost"}
VALID_PAYMENT_STATUSES = {"Not sent", "Link sent", "Paid", "Failed"}

# -----------------------------------------------------------------------------
# DB
# -----------------------------------------------------------------------------

def get_db_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        cursor_factory=RealDictCursor,
    )

# -----------------------------------------------------------------------------
# HELPERS
# -----------------------------------------------------------------------------

def clean(value):
    return (value or "").strip()

def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper

# -----------------------------------------------------------------------------
# ROUTES
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/signup")
def signup():
    return render_template("signup.html")

# -----------------------------------------------------------------------------
# SIGNUP → STRIPE
# -----------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
def submit():
    name = clean(request.form.get("full_name"))
    email = clean(request.form.get("email"))
    phone = clean(request.form.get("phone"))

    plan = clean(request.form.get("selected_plan"))
    broken = clean(request.form.get("boiler_broken"))
    under3 = clean(request.form.get("boiler_under_3_years"))
    warranty = clean(request.form.get("boiler_warranty_valid"))
    fix_join = clean(request.form.get("fix_and_join"))

    # RULE: Essential validation
    if plan == "Essential":
        if broken == "Yes":
            flash("Essential not allowed for broken boilers", "error")
            return redirect(url_for("signup"))

        if not (under3 == "Yes" or warranty == "Yes"):
            flash("Essential requires boiler under 3 years or warranty", "error")
            return redirect(url_for("signup"))

    # Force Fix & Join if broken
    if broken == "Yes":
        fix_join = "Yes"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO signups (
                    full_name, email, phone,
                    selected_plan, monthly_price,
                    boiler_broken, fix_and_join,
                    payment_status, created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, (
                name,
                email,
                phone,
                plan,
                PLAN_PRICES[plan],
                broken,
                fix_join,
                "Not sent",
                datetime.now(UTC)
            ))
            signup_id = cur.fetchone()["id"]

        conn.commit()
    finally:
        conn.close()

    # STRIPE
    session_checkout = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{
            "price": STRIPE_PRICES[plan],
            "quantity": 1
        }],
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        metadata={"signup_id": signup_id}
    )

    # Save link
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signups
                SET stripe_checkout_url=%s, payment_status='Link sent'
                WHERE id=%s
            """, (session_checkout.url, signup_id))
        conn.commit()
    finally:
        conn.close()

    return redirect(session_checkout.url)

# -----------------------------------------------------------------------------
# STRIPE SUCCESS
# -----------------------------------------------------------------------------

@app.route("/stripe/success")
def stripe_success():
    session_id = request.args.get("session_id")

    checkout = stripe.checkout.Session.retrieve(session_id)
    signup_id = checkout.metadata.get("signup_id")

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signups
                SET payment_status='Paid'
                WHERE id=%s
            """, (signup_id,))
        conn.commit()
    finally:
        conn.close()

    return render_template("stripe_success.html")

@app.route("/stripe/cancel")
def stripe_cancel():
    return "Payment cancelled"

# -----------------------------------------------------------------------------
# ADMIN LOGIN
# -----------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == os.environ.get("ADMIN_PASSWORD"):
            session["admin"] = True
            return redirect(url_for("admin"))
        flash("Wrong password", "error")
    return render_template("admin_login.html")

# -----------------------------------------------------------------------------
# ADMIN DASHBOARD (WITH STATS)
# -----------------------------------------------------------------------------

@app.route("/admin")
@login_required
def admin():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups ORDER BY id DESC")
            rows = cur.fetchall()

            cur.execute("SELECT COUNT(*) FROM signups")
            total = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) FROM signups WHERE payment_status='Paid'")
            paid = cur.fetchone()["count"]

    finally:
        conn.close()

    stats = {
        "total": total,
        "paid": paid,
        "conversion": round((paid/total)*100,1) if total else 0
    }

    return render_template("admin.html", signups=rows, stats=stats)

# -----------------------------------------------------------------------------
# RESEND PAYMENT
# -----------------------------------------------------------------------------

@app.route("/admin/resend/<int:id>", methods=["POST"])
@login_required
def resend_payment(id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups WHERE id=%s", (id,))
            row = cur.fetchone()

        session_checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{
                "price": STRIPE_PRICES[row["selected_plan"]],
                "quantity": 1
            }],
            success_url=STRIPE_SUCCESS_URL,
            cancel_url=STRIPE_CANCEL_URL,
            metadata={"signup_id": id}
        )

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE signups
                SET stripe_checkout_url=%s, payment_status='Link sent'
                WHERE id=%s
            """, (session_checkout.url, id))
        conn.commit()
    finally:
        conn.close()

    flash("Payment link resent", "success")
    return redirect(url_for("admin"))

# -----------------------------------------------------------------------------
# START
# ------------------------- ----------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)