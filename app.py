from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    send_from_directory,
)
from datetime import datetime, UTC
import os
from functools import wraps

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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOCS_DIR = os.path.join(STATIC_DIR, "docs")

# -----------------------------------------------------------------------------
# CONFIG
# -----------------------------------------------------------------------------

COMPANY_NAME = os.environ.get("COMPANY_NAME", "SJM Heating")
COMPANY_REG = os.environ.get("COMPANY_REG", "")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "")
COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "")
COMPANY_WEBSITE = os.environ.get("COMPANY_WEBSITE", "")
FAVICON_PATH = os.environ.get("FAVICON_PATH", "favicon.ico")

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "")
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

TERMS_PDF_FILENAME = "sjm_service_plan_terms_v1.pdf"
PRIVACY_PDF_FILENAME = "sjm_privacy_policy_v1.pdf"

# -----------------------------------------------------------------------------
# TEMPLATE GLOBALS
# -----------------------------------------------------------------------------

@app.context_processor
def inject_company_details():
    return {
        "company_name": COMPANY_NAME,
        "company_reg": COMPANY_REG,
        "company_phone": COMPANY_PHONE,
        "company_email": COMPANY_EMAIL,
        "company_website": COMPANY_WEBSITE,
        "favicon_path": FAVICON_PATH,
    }

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

def safe_success_url():
    return STRIPE_SUCCESS_URL or (url_for("stripe_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}")

def safe_cancel_url():
    return STRIPE_CANCEL_URL or url_for("stripe_cancel", _external=True)

def validate_required_env():
    missing = []

    required = {
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY"),
    }

    for key, value in required.items():
        if not value:
            missing.append(key)

    return missing

def docs_file_exists(filename):
    return os.path.exists(os.path.join(DOCS_DIR, filename))

# -----------------------------------------------------------------------------
# ROUTES
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/signup", methods=["GET"])
def signup():
    return render_template("signup.html", plan_prices=PLAN_PRICES)

@app.route("/success")
def success():
    signup_id = request.args.get("signup_id")
    signup_row = None

    if signup_id:
        try:
            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM signups WHERE id=%s", (signup_id,))
                    signup_row = cur.fetchone()
            finally:
                conn.close()
        except Exception:
            signup_row = None

    return render_template("success.html", signup=signup_row)

@app.route("/terms")
def terms():
    if docs_file_exists(TERMS_PDF_FILENAME):
        return send_from_directory(DOCS_DIR, TERMS_PDF_FILENAME)
    return render_template("terms.html")

@app.route("/privacy")
def privacy():
    if docs_file_exists(PRIVACY_PDF_FILENAME):
        return send_from_directory(DOCS_DIR, PRIVACY_PDF_FILENAME)
    return render_template("privacy.html")

@app.route("/health")
def health():
    return {"status": "ok"}, 200

# -----------------------------------------------------------------------------
# SIGNUP -> STRIPE
# -----------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
def submit():
    missing_env = validate_required_env()
    if missing_env:
        flash(f"Server configuration error: missing {', '.join(missing_env)}", "error")
        return redirect(url_for("signup"))

    name = clean(request.form.get("full_name"))
    email = clean(request.form.get("email"))
    phone = clean(request.form.get("phone"))

    plan = clean(request.form.get("selected_plan"))
    broken = clean(request.form.get("boiler_broken"))
    under3 = clean(request.form.get("boiler_under_3_years"))
    warranty = clean(request.form.get("boiler_warranty_valid"))
    fix_join = clean(request.form.get("fix_and_join"))

    if not name or not email or not plan:
        flash("Please complete the required fields.", "error")
        return redirect(url_for("signup"))

    if plan not in PLAN_PRICES:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("signup"))

    if not STRIPE_PRICES.get(plan):
        flash(f"Stripe price is not configured for {plan}.", "error")
        return redirect(url_for("signup"))

    if plan == "Essential":
        if broken == "Yes":
            flash("Essential is not allowed for broken boilers.", "error")
            return redirect(url_for("signup"))

        if not (under3 == "Yes" or warranty == "Yes"):
            flash("Essential requires the boiler to be under 3 years old or under warranty.", "error")
            return redirect(url_for("signup"))

    if broken == "Yes":
        fix_join = "Yes"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signups (
                    full_name,
                    email,
                    phone,
                    selected_plan,
                    monthly_price,
                    boiler_broken,
                    fix_and_join,
                    payment_status,
                    created_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    name,
                    email,
                    phone,
                    plan,
                    PLAN_PRICES[plan],
                    broken,
                    fix_join,
                    "Not sent",
                    datetime.now(UTC),
                ),
            )
            signup_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    try:
        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": STRIPE_PRICES[plan],
                    "quantity": 1,
                }
            ],
            success_url=safe_success_url(),
            cancel_url=safe_cancel_url(),
            metadata={"signup_id": str(signup_id)},
        )
    except Exception as e:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE signups
                    SET payment_status='Failed'
                    WHERE id=%s
                    """,
                    (signup_id,),
                )
            conn.commit()
        finally:
            conn.close()

        flash(f"Unable to create Stripe checkout: {e}", "error")
        return redirect(url_for("signup"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signups
                SET stripe_checkout_url=%s, payment_status='Link sent'
                WHERE id=%s
                """,
                (checkout_session.url, signup_id),
            )
        conn.commit()
    finally:
        conn.close()

    return redirect(checkout_session.url)

# -----------------------------------------------------------------------------
# STRIPE SUCCESS / CANCEL
# -----------------------------------------------------------------------------

@app.route("/stripe/success")
def stripe_success():
    session_id = request.args.get("session_id")

    if not session_id:
        return render_template("stripe_success.html")

    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
        signup_id = checkout.metadata.get("signup_id")
    except Exception as e:
        flash(f"Could not verify payment session: {e}", "error")
        return render_template("stripe_success.html")

    if signup_id:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE signups
                    SET payment_status='Paid'
                    WHERE id=%s
                    """,
                    (signup_id,),
                )
            conn.commit()
        finally:
            conn.close()

    return render_template("stripe_success.html")

@app.route("/stripe/cancel")
def stripe_cancel():
    return render_template("stripe_cancel.html")

# -----------------------------------------------------------------------------
# ADMIN LOGIN / LOGOUT
# -----------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            flash("Logged in successfully.", "success")
            return redirect(url_for("admin"))
        flash("Wrong password", "error")

    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("Logged out successfully.", "success")
    return redirect(url_for("admin_login"))

# -----------------------------------------------------------------------------
# ADMIN DASHBOARD
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
        "conversion": round((paid / total) * 100, 1) if total else 0,
    }

    return render_template("admin.html", signups=rows, stats=stats)

# -----------------------------------------------------------------------------
# RESEND PAYMENT LINK
# -----------------------------------------------------------------------------

@app.route("/admin/resend/<int:id>", methods=["POST"])
@login_required
def resend_payment(id):
    missing_env = validate_required_env()
    if missing_env:
        flash(f"Server configuration error: missing {', '.join(missing_env)}", "error")
        return redirect(url_for("admin"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups WHERE id=%s", (id,))
            row = cur.fetchone()

        if not row:
            flash("Signup not found.", "error")
            return redirect(url_for("admin"))

        plan = row["selected_plan"]

        if not STRIPE_PRICES.get(plan):
            flash(f"Stripe price is not configured for {plan}.", "error")
            return redirect(url_for("admin"))

        checkout_session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[
                {
                    "price": STRIPE_PRICES[plan],
                    "quantity": 1,
                }
            ],
            success_url=safe_success_url(),
            cancel_url=safe_cancel_url(),
            metadata={"signup_id": str(id)},
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signups
                SET stripe_checkout_url=%s, payment_status='Link sent'
                WHERE id=%s
                """,
                (checkout_session.url, id),
            )
        conn.commit()
    finally:
        conn.close()

    flash("Payment link resent.", "success")
    return redirect(url_for("admin"))

# -----------------------------------------------------------------------------
# ERROR PAGES
# -----------------------------------------------------------------------------

@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(error):
    return render_template("500.html"), 500

# -----------------------------------------------------------------------------
# START
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)