from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    flash,
    session,
    send_from_directory,
    Response,
)
from datetime import datetime, UTC
import os
import csv
import io
from functools import wraps
from urllib.parse import quote_plus

from dotenv import load_dotenv
import stripe
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

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
COMPANY_REG = os.environ.get("COMPANY_REG", "10947654")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "07826848858")
COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "info@sjmheating.co.uk")
COMPANY_WEBSITE = os.environ.get("COMPANY_WEBSITE", "")
FAVICON_PATH = os.environ.get("FAVICON_PATH", "favicon.ico")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

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

FIX_AND_JOIN_FEE = "240.99"
TERMS_VERSION = "v1.0"
PRIVACY_VERSION = "v1.0"

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
        "fix_and_join_fee": FIX_AND_JOIN_FEE,
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
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


def checkbox_to_bool(value):
    return value in ("on", "true", "True", "1", True)


def login_required(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return func(*args, **kwargs)
    return wrapper


def safe_success_url():
    return STRIPE_SUCCESS_URL or (
        url_for("stripe_success", _external=True) + "?session_id={CHECKOUT_SESSION_ID}"
    )


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
        "STRIPE_PRICE_STANDARD": STRIPE_PRICES.get("Standard"),
        "STRIPE_PRICE_COMPLETE": STRIPE_PRICES.get("Complete"),
    }
    for key, value in required.items():
        if not value:
            missing.append(key)
    return missing


def docs_file_exists(filename):
    return os.path.exists(os.path.join(DOCS_DIR, filename))


def build_full_address(row):
    parts = [
        row.get("address_line_1"),
        row.get("address_line_2"),
        row.get("city"),
        row.get("postcode"),
    ]
    return ", ".join([p for p in parts if p])


def build_maps_link(row):
    address = build_full_address(row)
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}"


def build_directions_link(row):
    address = build_full_address(row)
    return f"https://www.google.com/maps/dir/?api=1&destination={quote_plus(address)}"


def get_eligible_plans(broken, under3, warranty):
    if broken == "Yes":
        return ["Standard", "Complete"], True

    if under3 == "Yes" or warranty == "Yes":
        return ["Essential", "Standard", "Complete"], False

    return ["Standard", "Complete"], False


def looks_like_bot_submission(form):
    honeypot = clean(form.get("contact_reference"))
    return bool(honeypot)

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
# POSTCODE LOOKUP
# -----------------------------------------------------------------------------

@app.route("/api/postcode-lookup")
def postcode_lookup():
    postcode = clean(request.args.get("postcode")).upper()

    if not postcode:
        return {"error": "No postcode provided"}, 400

    try:
        response = requests.get(
            f"https://api.postcodes.io/postcodes/{quote_plus(postcode)}",
            timeout=10,
        )
        data = response.json()

        if response.status_code != 200 or data.get("status") != 200:
            return {"error": "Invalid postcode"}, 400

        result = data.get("result", {}) or {}

        return {
            "postcode": result.get("postcode", postcode),
            "city": result.get("admin_district") or result.get("admin_ward") or "",
            "region": result.get("region") or "",
            "country": result.get("country") or "",
        }
    except Exception:
        return {"error": "Lookup failed"}, 500

# -----------------------------------------------------------------------------
# SIGNUP -> STRIPE
# -----------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
def submit():
    if looks_like_bot_submission(request.form):
        flash("We could not verify your submission. Please try again.", "error")
        return redirect(url_for("signup"))

    missing_env = validate_required_env()
    if missing_env:
        flash(f"Server configuration error: missing {', '.join(missing_env)}", "error")
        return redirect(url_for("signup"))

    name = clean(request.form.get("full_name"))
    email = clean(request.form.get("email"))
    phone = clean(request.form.get("phone"))

    address_line_1 = clean(request.form.get("address_line_1"))
    address_line_2 = clean(request.form.get("address_line_2"))
    city = clean(request.form.get("city"))
    postcode = clean(request.form.get("postcode")).upper()

    broken = clean(request.form.get("boiler_broken"))
    under3 = clean(request.form.get("boiler_under_3_years"))
    warranty = clean(request.form.get("boiler_warranty_valid"))

    plan = clean(request.form.get("selected_plan"))
    fix_join = "Yes" if broken == "Yes" else "No"
    fix_and_join_fee = FIX_AND_JOIN_FEE if fix_join == "Yes" else ""

    signature_name = clean(request.form.get("signature_name"))
    signature_data = clean(request.form.get("signature_data"))
    accepted_terms = checkbox_to_bool(request.form.get("accepted_terms"))
    accepted_privacy = checkbox_to_bool(request.form.get("accepted_privacy"))
    accepted_fair_usage = checkbox_to_bool(request.form.get("accepted_fair_usage"))

    if not name or not email or not address_line_1 or not city or not postcode:
        flash("Please complete all required fields.", "error")
        return redirect(url_for("signup"))

    if broken not in ["Yes", "No"]:
        flash("Please answer whether the boiler is currently broken.", "error")
        return redirect(url_for("signup"))

    if broken == "No":
        if under3 not in ["Yes", "No"] or warranty not in ["Yes", "No"]:
            flash("Please answer the boiler age and warranty questions.", "error")
            return redirect(url_for("signup"))
    else:
        under3 = ""
        warranty = ""

    eligible_plans, _ = get_eligible_plans(broken, under3, warranty)

    if plan not in eligible_plans:
        flash("The selected plan is not valid for the answers given.", "error")
        return redirect(url_for("signup"))

    if not signature_name:
        flash("Please enter your typed signature name.", "error")
        return redirect(url_for("signup"))

    if not signature_data or not signature_data.startswith("data:image/png;base64,"):
        flash("Please provide your drawn signature.", "error")
        return redirect(url_for("signup"))

    if not accepted_terms or not accepted_privacy or not accepted_fair_usage:
        flash("Please accept the Terms, Privacy Policy, and Fair Usage Policy.", "error")
        return redirect(url_for("signup"))

    if not STRIPE_PRICES.get(plan):
        flash(f"Stripe price is not configured for {plan}.", "error")
        return redirect(url_for("signup"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            now = datetime.now(UTC)
            cur.execute(
                """
                INSERT INTO signups (
                    full_name,
                    email,
                    phone,
                    address_line_1,
                    address_line_2,
                    city,
                    postcode,
                    selected_plan,
                    monthly_price,
                    boiler_broken,
                    boiler_under_3_years,
                    boiler_warranty_valid,
                    fix_and_join,
                    fix_and_join_fee,
                    signature_name,
                    signature_data,
                    accepted_terms,
                    accepted_privacy,
                    accepted_fair_usage,
                    status,
                    payment_status,
                    created_at,
                    updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    name,
                    email,
                    phone,
                    address_line_1,
                    address_line_2,
                    city,
                    postcode,
                    plan,
                    PLAN_PRICES[plan],
                    broken,
                    under3,
                    warranty,
                    fix_join,
                    fix_and_join_fee,
                    signature_name,
                    signature_data,
                    accepted_terms,
                    accepted_privacy,
                    accepted_fair_usage,
                    "New",
                    "Not sent",
                    now,
                    now,
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
            customer_email=email,
            metadata={
                "signup_id": str(signup_id),
                "fix_and_join": fix_join,
                "fix_and_join_fee": fix_and_join_fee,
                "selected_plan": plan,
            },
        )
    except Exception as e:
        conn = get_db_connection()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE signups
                    SET payment_status='Failed',
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (datetime.now(UTC), signup_id),
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
                SET stripe_checkout_url=%s,
                    payment_status='Link sent',
                    updated_at=%s
                WHERE id=%s
                """,
                (checkout_session.url, datetime.now(UTC), signup_id),
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
                    SET payment_status='Paid',
                        updated_at=%s
                    WHERE id=%s
                    """,
                    (datetime.now(UTC), signup_id),
                )
            conn.commit()
        finally:
            conn.close()

    return render_template("stripe_success.html")


@app.route("/stripe/cancel")
def stripe_cancel():
    return render_template("stripe_cancel.html")

# -----------------------------------------------------------------------------
# ADMIN
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

    return render_template(
        "admin.html",
        signups=rows,
        stats=stats,
        build_maps_link=build_maps_link,
        build_directions_link=build_directions_link,
        build_full_address=build_full_address,
    )


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
            customer_email=row.get("email") or None,
            metadata={
                "signup_id": str(id),
                "fix_and_join": row.get("fix_and_join") or "No",
                "fix_and_join_fee": row.get("fix_and_join_fee") or "",
                "selected_plan": row.get("selected_plan") or "",
            },
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signups
                SET stripe_checkout_url=%s,
                    payment_status='Link sent',
                    updated_at=%s
                WHERE id=%s
                """,
                (checkout_session.url, datetime.now(UTC), id),
            )
        conn.commit()
    finally:
        conn.close()

    flash("Payment link resent.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/export.csv")
@login_required
def export_csv():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups ORDER BY id DESC")
            rows = cur.fetchall()
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow([
        "ID",
        "Created At",
        "Updated At",
        "Full Name",
        "Email",
        "Phone",
        "Address Line 1",
        "Address Line 2",
        "City",
        "Postcode",
        "Selected Plan",
        "Monthly Price",
        "Boiler Broken",
        "Boiler Under 3 Years",
        "Boiler Warranty Valid",
        "Fix And Join",
        "Fix And Join Fee",
        "Signature Name",
        "Has Drawn Signature",
        "Accepted Terms",
        "Accepted Privacy",
        "Accepted Fair Usage",
        "Status",
        "Payment Status",
        "Stripe Checkout URL",
    ])

    for row in rows:
        writer.writerow([
            row.get("id"),
            row.get("created_at"),
            row.get("updated_at"),
            row.get("full_name"),
            row.get("email"),
            row.get("phone"),
            row.get("address_line_1"),
            row.get("address_line_2"),
            row.get("city"),
            row.get("postcode"),
            row.get("selected_plan"),
            row.get("monthly_price"),
            row.get("boiler_broken"),
            row.get("boiler_under_3_years"),
            row.get("boiler_warranty_valid"),
            row.get("fix_and_join"),
            row.get("fix_and_join_fee"),
            row.get("signature_name"),
            "Yes" if row.get("signature_data") else "No",
            row.get("accepted_terms"),
            row.get("accepted_privacy"),
            row.get("accepted_fair_usage"),
            row.get("status"),
            row.get("payment_status"),
            row.get("stripe_checkout_url"),
        ])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=sjm_signups.csv"
    return response

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