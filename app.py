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
from datetime import datetime, UTC, timedelta
from decimal import Decimal
import os
import csv
import io
import base64
import tempfile
import logging
from functools import wraps
from urllib.parse import quote_plus

from dotenv import load_dotenv
import stripe
import resend
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import Binary
import requests
from werkzeug.middleware.proxy_fix import ProxyFix

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib import colors

# -----------------------------------------------------------------------------
# INIT
# -----------------------------------------------------------------------------

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this")
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_ENV") == "production"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("sjm_service_plan")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DOCS_DIR = os.path.join(STATIC_DIR, "docs")
LOGO_PATH = os.path.join(STATIC_DIR, "logo.png")

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
ADMIN_NOTIFICATION_EMAIL = os.environ.get("ADMIN_NOTIFICATION_EMAIL", COMPANY_EMAIL)

RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", COMPANY_EMAIL)
resend.api_key = os.environ.get("RESEND_API_KEY")

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_ONE_OFF_SERVICE = os.environ.get("STRIPE_PRICE_ONE_OFF_SERVICE")

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
ONE_OFF_SERVICE_PRICE = "96.00"
ONE_OFF_SERVICE_BOOKING_TYPE = "one_off_annual_service"
ONE_OFF_BOOKING_STATUSES = ["New", "Contacted", "Booked", "Completed", "Cancelled"]
ONE_OFF_APPOINTMENT_STATUSES = ["To arrange", "Date offered", "Confirmed", "Completed"]
DEFAULT_ASSIGNED_ENGINEER = "Sean"
TERMS_VERSION = os.environ.get("TERMS_VERSION", "v1.0")
PRIVACY_VERSION = os.environ.get("PRIVACY_VERSION", "v1.0")

TERMS_PDF_FILENAME = "sjm_service_plan_terms_v1.pdf"
PRIVACY_PDF_FILENAME = "sjm_privacy_policy_v1.pdf"
FAIR_USAGE_PDF_FILENAME = "sjm_fair_usage_policy_v1.pdf"

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
        "one_off_service_price": ONE_OFF_SERVICE_PRICE,
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "build_full_address": build_full_address,
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


def db_execute(query, params=None, fetchone=False, fetchall=False, commit=False):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params or ())
            result = None
            if fetchone:
                result = cur.fetchone()
            elif fetchall:
                result = cur.fetchall()

            if commit:
                conn.commit()

            return result
    finally:
        conn.close()

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


def validate_required_env(include_monthly_prices=True, include_one_off_price=False):
    missing = []
    required = {
        "DB_NAME": DB_NAME,
        "DB_USER": DB_USER,
        "DB_PASSWORD": DB_PASSWORD,
        "DB_HOST": DB_HOST,
        "DB_PORT": DB_PORT,
        "STRIPE_SECRET_KEY": os.environ.get("STRIPE_SECRET_KEY"),
    }

    if include_monthly_prices:
        required.update(
            {
                "STRIPE_PRICE_ESSENTIAL": STRIPE_PRICES.get("Essential"),
                "STRIPE_PRICE_STANDARD": STRIPE_PRICES.get("Standard"),
                "STRIPE_PRICE_COMPLETE": STRIPE_PRICES.get("Complete"),
            }
        )

    if include_one_off_price:
        required["STRIPE_PRICE_ONE_OFF_SERVICE"] = STRIPE_PRICE_ONE_OFF_SERVICE

    for key, value in required.items():
        if not value:
            missing.append(key)
    return missing


def docs_file_exists(filename):
    return bool(filename) and os.path.exists(os.path.join(DOCS_DIR, filename))


def value_or_first(row, *keys):
    if not row:
        return ""

    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def boolish_to_bool(value):
    if isinstance(value, bool):
        return value
    if value in (1, "1", "true", "True", "TRUE", "yes", "Yes", "YES", "on"):
        return True
    if value in (0, "0", "false", "False", "FALSE", "no", "No", "NO", "off"):
        return False
    return False


def build_full_name(row):
    full_name = clean(value_or_first(row, "full_name"))
    if full_name:
        return full_name

    first_name = clean(value_or_first(row, "first_name"))
    last_name = clean(value_or_first(row, "last_name"))
    return " ".join(part for part in [first_name, last_name] if part)


def normalize_signup_record(signup):
    if not signup:
        return signup

    row = dict(signup)
    row["address_line_1"] = value_or_first(row, "address_line_1", "address_line1")
    row["address_line_2"] = value_or_first(row, "address_line_2", "address_line2")
    row["city"] = value_or_first(row, "city", "town")
    row["full_name"] = build_full_name(row)
    row["signature_name"] = value_or_first(row, "signature_name") or row["full_name"]
    row["signature_data"] = value_or_first(row, "signature_data", "signature")
    row["accepted_terms"] = boolish_to_bool(value_or_first(row, "accepted_terms", "terms_accepted"))
    row["accepted_privacy"] = boolish_to_bool(value_or_first(row, "accepted_privacy", "privacy_accepted"))
    row["accepted_fair_usage"] = boolish_to_bool(value_or_first(row, "accepted_fair_usage"))
    row["last_payment_link_sent_at"] = value_or_first(
        row, "last_payment_link_sent_at", "stripe_payment_link_sent_at"
    )
    row["boiler_under_3_years"] = value_or_first(row, "boiler_under_3_years")
    row["boiler_warranty_valid"] = value_or_first(row, "boiler_warranty_valid")
    row["contract_pdf_filename"] = value_or_first(row, "contract_pdf_filename")
    return row


def normalize_signup_rows(rows):
    return [normalize_signup_record(row) for row in rows or []]


def normalize_one_off_booking(booking):
    if not booking:
        return booking

    row = dict(booking)
    row["full_name"] = build_full_name(row)
    row["booking_type"] = value_or_first(row, "booking_type") or ONE_OFF_SERVICE_BOOKING_TYPE
    row["payment_status"] = value_or_first(row, "payment_status") or "Pending"
    row["status"] = value_or_first(row, "status") or "New"
    row["appointment_status"] = value_or_first(row, "appointment_status") or "To arrange"
    row["assigned_engineer"] = value_or_first(row, "assigned_engineer") or DEFAULT_ASSIGNED_ENGINEER
    return row


def normalize_one_off_rows(rows):
    return [normalize_one_off_booking(row) for row in rows or []]


def build_full_address(row):
    parts = [
        value_or_first(row, "address_line_1", "address_line1"),
        value_or_first(row, "address_line_2", "address_line2"),
        value_or_first(row, "city", "town"),
        value_or_first(row, "county"),
        value_or_first(row, "postcode"),
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


def money_to_pence(value):
    value = clean(value)
    if not value:
        return 0
    parts = value.split(".")
    pounds = int(parts[0])
    pence = int((parts[1] if len(parts) > 1 else "0").ljust(2, "0")[:2])
    return pounds * 100 + pence


def pence_to_money(amount):
    if amount in (None, ""):
        return ""

    return format((Decimal(str(amount)) / Decimal("100")), ".2f")


def get_checkout_value(checkout, key, default=None):
    if isinstance(checkout, dict):
        return checkout.get(key, default)
    return getattr(checkout, key, default)


def get_checkout_metadata(checkout):
    metadata = get_checkout_value(checkout, "metadata", {}) or {}
    return dict(metadata)


def fetch_signup(signup_id):
    row = db_execute(
        "SELECT * FROM signups WHERE id=%s",
        (signup_id,),
        fetchone=True,
    )
    return normalize_signup_record(row)


def fetch_one_off_booking(booking_id):
    row = db_execute(
        "SELECT * FROM one_off_service_bookings WHERE id=%s",
        (booking_id,),
        fetchone=True,
    )
    return normalize_one_off_booking(row)


def update_signup_email_status(signup_id, customer_sent=None, admin_sent=None):
    updates = []
    values = []

    if customer_sent is not None:
        updates.append("customer_email_sent=%s")
        values.append(customer_sent)

    if admin_sent is not None:
        updates.append("admin_email_sent=%s")
        values.append(admin_sent)

    if not updates:
        return

    updates.append("updated_at=%s")
    values.append(datetime.now(UTC))
    values.append(signup_id)

    db_execute(
        f"""
        UPDATE signups
        SET {", ".join(updates)}
        WHERE id=%s
        """,
        tuple(values),
        commit=True,
    )


def mark_reminder_sent(signup_id):
    now = datetime.now(UTC)
    db_execute(
        """
        UPDATE signups
        SET reminder_sent=TRUE,
            reminder_sent_at=%s,
            updated_at=%s
        WHERE id=%s
        """,
        (now, now, signup_id),
        commit=True,
    )


def mark_payment_link_generated(signup_id, checkout_url, checkout_session_id):
    now = datetime.now(UTC)
    db_execute(
        """
        UPDATE signups
        SET stripe_checkout_url=%s,
            stripe_checkout_session_id=%s,
            payment_status='Link sent',
            last_payment_link_sent_at=%s,
            stripe_payment_link_sent_at=%s,
            updated_at=%s
        WHERE id=%s
        """,
        (checkout_url, checkout_session_id, now, now, now, signup_id),
        commit=True,
    )


def mark_signup_paid(signup_id, checkout=None):
    now = datetime.now(UTC)

    stripe_customer_id = None
    stripe_subscription_id = None

    if checkout:
        stripe_customer_id = get_checkout_value(checkout, "customer")
        stripe_subscription_id = get_checkout_value(checkout, "subscription")

    db_execute(
        """
        UPDATE signups
        SET payment_status='Paid',
            payment_completed_at=%s,
            stripe_customer_id=COALESCE(%s, stripe_customer_id),
            stripe_subscription_id=COALESCE(%s, stripe_subscription_id),
            updated_at=%s
        WHERE id=%s
        """,
        (now, stripe_customer_id, stripe_subscription_id, now, signup_id),
        commit=True,
    )


def update_one_off_booking_email_status(booking_id, customer_sent=None, admin_sent=None):
    updates = []
    values = []

    if customer_sent is not None:
        updates.append("customer_email_sent=%s")
        values.append(customer_sent)

    if admin_sent is not None:
        updates.append("admin_email_sent=%s")
        values.append(admin_sent)

    if not updates:
        return

    updates.append("updated_at=%s")
    values.append(datetime.now(UTC))
    values.append(booking_id)

    db_execute(
        f"""
        UPDATE one_off_service_bookings
        SET {", ".join(updates)}
        WHERE id=%s
        """,
        tuple(values),
        commit=True,
    )


def mark_one_off_checkout_created(booking_id, checkout_session):
    db_execute(
        """
        UPDATE one_off_service_bookings
        SET stripe_session_id=%s,
            payment_status='Checkout created',
            updated_at=%s
        WHERE id=%s
        """,
        (checkout_session.id, datetime.now(UTC), booking_id),
        commit=True,
    )


def mark_one_off_booking_cancelled(booking_id):
    db_execute(
        """
        UPDATE one_off_service_bookings
        SET payment_status=CASE
                WHEN payment_status='Paid' THEN payment_status
                ELSE 'Cancelled'
            END,
            updated_at=%s
        WHERE id=%s
        """,
        (datetime.now(UTC), booking_id),
        commit=True,
    )


def mark_one_off_booking_paid(booking_id, checkout):
    now = datetime.now(UTC)
    stripe_payment_intent_id = get_checkout_value(checkout, "payment_intent")
    amount_paid = pence_to_money(get_checkout_value(checkout, "amount_total"))

    db_execute(
        """
        UPDATE one_off_service_bookings
        SET payment_status='Paid',
            stripe_session_id=COALESCE(%s, stripe_session_id),
            stripe_payment_intent_id=COALESCE(%s, stripe_payment_intent_id),
            amount_paid=COALESCE(%s, amount_paid),
            updated_at=%s
        WHERE id=%s
        """,
        (
            get_checkout_value(checkout, "id"),
            stripe_payment_intent_id,
            amount_paid or None,
            now,
            booking_id,
        ),
        commit=True,
    )


def get_stored_contract_pdf_bytes(signup):
    pdf_value = signup.get("contract_pdf")
    if not pdf_value:
        return None

    if isinstance(pdf_value, memoryview):
        return pdf_value.tobytes()

    if isinstance(pdf_value, bytes):
        return pdf_value

    return bytes(pdf_value)


def save_contract_pdf_to_db(signup_id, pdf_bytes):
    now = datetime.now(UTC)
    filename = f"sjm-service-plan-{signup_id}.pdf"

    db_execute(
        """
        UPDATE signups
        SET contract_pdf=%s,
            contract_pdf_filename=%s,
            contract_pdf_generated_at=%s,
            updated_at=%s
        WHERE id=%s
        """,
        (Binary(pdf_bytes), filename, now, now, signup_id),
        commit=True,
    )


def get_or_create_contract_pdf_bytes(signup_id):
    signup = fetch_signup(signup_id)
    if not signup:
        return None, None

    stored_pdf = get_stored_contract_pdf_bytes(signup)
    if stored_pdf:
        return stored_pdf, signup

    pdf_bytes = build_contract_pdf_bytes(signup)
    save_contract_pdf_to_db(signup_id, pdf_bytes)

    refreshed_signup = fetch_signup(signup_id)
    return pdf_bytes, refreshed_signup


def ensure_extra_columns():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                ALTER TABLE signups
                ADD COLUMN IF NOT EXISTS address_line1 TEXT,
                ADD COLUMN IF NOT EXISTS address_line2 TEXT,
                ADD COLUMN IF NOT EXISTS address_line_1 TEXT,
                ADD COLUMN IF NOT EXISTS address_line_2 TEXT,
                ADD COLUMN IF NOT EXISTS boiler_make TEXT,
                ADD COLUMN IF NOT EXISTS boiler_model TEXT,
                ADD COLUMN IF NOT EXISTS boiler_age TEXT,
                ADD COLUMN IF NOT EXISTS boiler_working TEXT,
                ADD COLUMN IF NOT EXISTS boiler_under_3_years TEXT,
                ADD COLUMN IF NOT EXISTS boiler_warranty_valid TEXT,
                ADD COLUMN IF NOT EXISTS existing_customer TEXT,
                ADD COLUMN IF NOT EXISTS fix_and_join_fee TEXT,
                ADD COLUMN IF NOT EXISTS signature TEXT,
                ADD COLUMN IF NOT EXISTS signed_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS signature_name TEXT,
                ADD COLUMN IF NOT EXISTS signature_data TEXT,
                ADD COLUMN IF NOT EXISTS terms_accepted INTEGER,
                ADD COLUMN IF NOT EXISTS privacy_accepted INTEGER,
                ADD COLUMN IF NOT EXISTS accepted_terms BOOLEAN,
                ADD COLUMN IF NOT EXISTS accepted_privacy BOOLEAN,
                ADD COLUMN IF NOT EXISTS accepted_fair_usage BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS terms_version TEXT,
                ADD COLUMN IF NOT EXISTS privacy_version TEXT,
                ADD COLUMN IF NOT EXISTS customer_email_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS admin_email_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS reminder_due_date TIMESTAMP,
                ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS status TEXT,
                ADD COLUMN IF NOT EXISTS payment_status TEXT,
                ADD COLUMN IF NOT EXISTS stripe_checkout_url TEXT,
                ADD COLUMN IF NOT EXISTS stripe_checkout_session_id TEXT,
                ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
                ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
                ADD COLUMN IF NOT EXISTS stripe_payment_link_sent_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS payment_completed_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS last_payment_link_sent_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS contract_pdf BYTEA,
                ADD COLUMN IF NOT EXISTS contract_pdf_filename TEXT,
                ADD COLUMN IF NOT EXISTS contract_pdf_generated_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS ip_address TEXT,
                ADD COLUMN IF NOT EXISTS user_agent TEXT
                """
            )

            cur.execute(
                """
                UPDATE signups
                SET address_line_1 = COALESCE(address_line_1, address_line1),
                    address_line_2 = COALESCE(address_line_2, address_line2),
                    fix_and_join_fee = COALESCE(
                        fix_and_join_fee,
                        CASE WHEN fix_and_join = 'Yes' THEN %s ELSE NULL END
                    ),
                    signature_name = COALESCE(signature_name, NULLIF(full_name, '')),
                    signature_data = COALESCE(signature_data, signature),
                    accepted_terms = COALESCE(
                        accepted_terms,
                        CASE
                            WHEN terms_accepted IS NULL THEN NULL
                            WHEN terms_accepted = 1 THEN TRUE
                            ELSE FALSE
                        END
                    ),
                    accepted_privacy = COALESCE(
                        accepted_privacy,
                        CASE
                            WHEN privacy_accepted IS NULL THEN NULL
                            WHEN privacy_accepted = 1 THEN TRUE
                            ELSE FALSE
                        END
                    ),
                    boiler_under_3_years = COALESCE(
                        boiler_under_3_years,
                        CASE
                            WHEN boiler_age ~ '^[0-9]+$' AND boiler_age::INTEGER < 3 THEN 'Yes'
                            WHEN boiler_age ~ '^[0-9]+$' THEN 'No'
                            ELSE NULL
                        END
                    ),
                    customer_email_sent = COALESCE(customer_email_sent, FALSE),
                    admin_email_sent = COALESCE(admin_email_sent, FALSE),
                    reminder_sent = COALESCE(reminder_sent, FALSE),
                    last_payment_link_sent_at = COALESCE(last_payment_link_sent_at, stripe_payment_link_sent_at),
                    stripe_payment_link_sent_at = COALESCE(stripe_payment_link_sent_at, last_payment_link_sent_at),
                    updated_at = COALESCE(updated_at, created_at, NOW())
                """
            ,
                (FIX_AND_JOIN_FEE,),
            )

            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS one_off_service_bookings (
                    id SERIAL PRIMARY KEY,
                    booking_type TEXT NOT NULL DEFAULT '{ONE_OFF_SERVICE_BOOKING_TYPE}',
                    first_name TEXT NOT NULL,
                    last_name TEXT NOT NULL,
                    full_name TEXT,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    address_line_1 TEXT NOT NULL,
                    address_line_2 TEXT,
                    town TEXT NOT NULL,
                    county TEXT,
                    postcode TEXT NOT NULL,
                    boiler_make TEXT,
                    boiler_model TEXT,
                    customer_notes TEXT,
                    access_notes TEXT,
                    preferred_dates TEXT,
                    acknowledged_contact BOOLEAN DEFAULT FALSE,
                    payment_status TEXT DEFAULT 'Pending',
                    stripe_session_id TEXT,
                    stripe_payment_intent_id TEXT,
                    amount_paid NUMERIC(10, 2),
                    status TEXT DEFAULT 'New',
                    appointment_status TEXT DEFAULT 'To arrange',
                    assigned_engineer TEXT,
                    appointment_date DATE,
                    appointment_time TEXT,
                    customer_email_sent BOOLEAN DEFAULT FALSE,
                    admin_email_sent BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
                    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
                )
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_off_bookings_stripe_session_id
                ON one_off_service_bookings (stripe_session_id)
                """
            )

            cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_off_bookings_stripe_payment_intent_id
                ON one_off_service_bookings (stripe_payment_intent_id)
                """
            )
        conn.commit()
    finally:
        conn.close()


def regenerate_all_contract_pdfs():
    rows = db_execute("SELECT id FROM signups ORDER BY id ASC", fetchall=True) or []
    for row in rows:
        signup = fetch_signup(row["id"])
        if signup:
            pdf_bytes = build_contract_pdf_bytes(signup)
            save_contract_pdf_to_db(row["id"], pdf_bytes)


def create_checkout_session(signup_id, email, plan, fix_join="No", fix_and_join_fee=""):
    line_items = [
        {
            "price": STRIPE_PRICES[plan],
            "quantity": 1,
        }
    ]

    if fix_join == "Yes" and fix_and_join_fee:
        line_items.append(
            {
                "price_data": {
                    "currency": "gbp",
                    "product_data": {
                        "name": "Fix & Join fee",
                        "description": "One-off Fix & Join charge",
                    },
                    "unit_amount": money_to_pence(fix_and_join_fee),
                },
                "quantity": 1,
            }
        )

    session_kwargs = {
        "payment_method_types": ["card"],
        "mode": "subscription",
        "line_items": line_items,
        "success_url": safe_success_url(),
        "cancel_url": safe_cancel_url(),
        "customer_email": email,
        "metadata": {
            "signup_id": str(signup_id),
            "fix_and_join": fix_join,
            "fix_and_join_fee": fix_and_join_fee,
            "selected_plan": plan,
        },
        "subscription_data": {
            "metadata": {
                "signup_id": str(signup_id),
                "fix_and_join": fix_join,
                "selected_plan": plan,
            }
        },
    }

    return stripe.checkout.Session.create(**session_kwargs)


def create_one_off_service_checkout_session(booking):
    return stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[
            {
                "price": STRIPE_PRICE_ONE_OFF_SERVICE,
                "quantity": 1,
            }
        ],
        success_url=safe_success_url(),
        cancel_url=url_for("annual_service_cancel", booking_id=booking["id"], _external=True),
        customer_email=booking.get("email"),
        client_reference_id=str(booking["id"]),
        metadata={
            "booking_type": ONE_OFF_SERVICE_BOOKING_TYPE,
            "booking_id": str(booking["id"]),
        },
        payment_intent_data={
            "metadata": {
                "booking_type": ONE_OFF_SERVICE_BOOKING_TYPE,
                "booking_id": str(booking["id"]),
            }
        },
    )

# -----------------------------------------------------------------------------
# PDF GENERATION
# -----------------------------------------------------------------------------

def decode_signature_to_tempfile(signature_data):
    if not signature_data or "," not in signature_data:
        return None

    try:
        _, encoded = signature_data.split(",", 1)
        image_bytes = base64.b64decode(encoded)
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
        tmp.write(image_bytes)
        tmp.flush()
        tmp.close()
        return tmp.name
    except Exception:
        logger.exception("Could not decode signature image.")
        return None


def build_contract_pdf_bytes(signup):
    signup = normalize_signup_record(signup)
    buffer = io.BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    margin = 42
    content_width = width - (margin * 2)
    y = height - 42

    accent_rgb = (1.0, 106 / 255, 0.0)

    def box(title, lines, top_y, min_height=70):
        line_height = 14
        body_height = max(min_height, 36 + len(lines) * line_height)
        bottom_y = top_y - body_height

        pdf.setFillColorRGB(0.98, 0.98, 0.98)
        pdf.setStrokeColorRGB(0.84, 0.84, 0.84)
        pdf.roundRect(margin, bottom_y, content_width, body_height, 8, stroke=1, fill=1)

        pdf.setFillColorRGB(1.0, 0.95, 0.92)
        pdf.roundRect(margin, top_y - 28, content_width, 28, 8, stroke=0, fill=1)

        pdf.setFillColorRGB(0.07, 0.07, 0.07)
        pdf.setFont("Helvetica-Bold", 11)
        pdf.drawString(margin + 12, top_y - 18, title)

        current_y = top_y - 44
        pdf.setFont("Helvetica", 10)
        for line in lines:
            pdf.drawString(margin + 12, current_y, line)
            current_y -= line_height

        return bottom_y - 14

    # Top accent bar
    pdf.setFillColorRGB(*accent_rgb)
    pdf.rect(margin, y - 22, content_width, 22, fill=1, stroke=0)

    if os.path.exists(LOGO_PATH):
        try:
            pdf.drawImage(
                LOGO_PATH,
                margin,
                y - 72,
                width=90,
                height=45,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            logger.exception("Could not draw logo in PDF.")

    pdf.setFillColorRGB(1, 1, 1)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(margin + 10, y - 14, "SJM HEATING SERVICE PLAN")

    pdf.setFillColorRGB(0.07, 0.07, 0.07)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawRightString(width - margin, y - 40, "Service Plan Agreement")
    pdf.setFont("Helvetica", 9)
    pdf.setFillColorRGB(0.4, 0.4, 0.4)
    pdf.drawRightString(width - margin, y - 56, f"Issued: {datetime.now(UTC).strftime('%d/%m/%Y')}")

    y -= 92

    # Company / customer panel
    pdf.setFillColorRGB(0.98, 0.98, 0.98)
    pdf.setStrokeColorRGB(0.84, 0.84, 0.84)
    pdf.roundRect(margin, y - 88, content_width, 88, 10, stroke=1, fill=1)

    pdf.setFillColorRGB(0.07, 0.07, 0.07)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin + 12, y - 18, COMPANY_NAME)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margin + 12, y - 34, f"Company Reg: {COMPANY_REG}")
    pdf.drawString(margin + 12, y - 48, f"Phone: {COMPANY_PHONE}")
    pdf.drawString(margin + 12, y - 62, f"Email: {COMPANY_EMAIL}")

    right_x = margin + (content_width / 2)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(right_x, y - 18, "Customer")
    pdf.setFont("Helvetica", 9)
    pdf.drawString(right_x, y - 34, f"Name: {signup.get('full_name') or '-'}")
    pdf.drawString(right_x, y - 48, f"Email: {signup.get('email') or '-'}")
    pdf.drawString(right_x, y - 62, f"Phone: {signup.get('phone') or '-'}")

    y -= 108

    y = box("Property Address", [build_full_address(signup) or "-"], y, min_height=58)

    plan_lines = [
        f"Selected Plan: {signup.get('selected_plan') or '-'}",
        f"Monthly Price: £{signup.get('monthly_price') or '-'}",
        f"Boiler Broken: {signup.get('boiler_broken') or '-'}",
        f"Boiler Under 3 Years: {signup.get('boiler_under_3_years') or '-'}",
        f"Warranty Valid: {signup.get('boiler_warranty_valid') or '-'}",
        f"Fix & Join: {signup.get('fix_and_join') or 'No'}",
    ]
    if signup.get("fix_and_join") == "Yes":
        plan_lines.append(f"Fix & Join Fee: £{signup.get('fix_and_join_fee') or FIX_AND_JOIN_FEE}")

    y = box("Plan Summary", plan_lines, y, min_height=110)

    y = box(
        "Important Cover Notes",
        [
            "This agreement confirms the customer's application for the selected SJM Heating service plan.",
            "Cover is subject to the plan terms, privacy policy and fair usage policy.",
            "All work remains subject to access, inspection, diagnosis, parts availability and system suitability.",
            "Fix & Join work does not guarantee full repair within the initial charge.",
        ],
        y,
        min_height=104,
    )

    y = box(
        "Legal Acceptance",
        [
            f"Terms Accepted: {'Yes' if signup.get('accepted_terms') else 'No'} ({TERMS_VERSION})",
            f"Privacy Accepted: {'Yes' if signup.get('accepted_privacy') else 'No'} ({PRIVACY_VERSION})",
            f"Fair Usage Accepted: {'Yes' if signup.get('accepted_fair_usage') else 'No'}",
        ],
        y,
        min_height=78,
    )

    # Signature block
    sig_height = 106
    pdf.setFillColorRGB(1, 1, 1)
    pdf.setStrokeColorRGB(0.84, 0.84, 0.84)
    pdf.roundRect(margin, y - sig_height, content_width, sig_height, 10, stroke=1, fill=1)

    pdf.setFillColorRGB(1.0, 0.95, 0.92)
    pdf.roundRect(margin, y - 28, content_width, 28, 10, stroke=0, fill=1)

    pdf.setFillColorRGB(0.07, 0.07, 0.07)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin + 12, y - 18, "Customer Signature")

    pdf.setFont("Helvetica", 10)
    pdf.drawString(margin + 12, y - 48, f"Typed Name: {signup.get('signature_name') or '-'}")
    pdf.drawString(margin + 12, y - 64, f"Signed At: {signup.get('created_at') or datetime.now(UTC)}")

    sig_path = decode_signature_to_tempfile(signup.get("signature_data"))
    if sig_path and os.path.exists(sig_path):
        try:
            pdf.drawImage(
                sig_path,
                width - margin - 190,
                y - 74,
                width=160,
                height=40,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            logger.exception("Could not render signature image in PDF.")
        finally:
            try:
                os.unlink(sig_path)
            except Exception:
                logger.exception("Could not delete temporary signature file.")

    # Footer
    pdf.setStrokeColorRGB(0.84, 0.84, 0.84)
    pdf.line(margin, 26, width - margin, 26)
    pdf.setFont("Helvetica", 8.5)
    pdf.setFillColorRGB(0.4, 0.4, 0.4)
    pdf.drawString(margin, 12, f"{COMPANY_NAME}  |  {COMPANY_PHONE}  |  {COMPANY_EMAIL}")
    pdf.drawRightString(width - margin, 12, "Service Plan Agreement")

    pdf.save()
    buffer.seek(0)
    return buffer.read()

# -----------------------------------------------------------------------------
# EMAIL ATTACHMENTS
# -----------------------------------------------------------------------------

def load_pdf_attachment_from_docs(filename):
    if not filename:
        return None

    path = os.path.join(DOCS_DIR, filename)
    if not os.path.exists(path):
        logger.warning("Attachment file missing: %s", path)
        return None

    with open(path, "rb") as f:
        return {
            "filename": filename,
            "content": base64.b64encode(f.read()).decode("utf-8"),
        }


def build_email_attachments(signup, contract_pdf_bytes):
    attachments = [
        {
            "filename": signup.get("contract_pdf_filename") or f"sjm-service-plan-{signup.get('id')}.pdf",
            "content": base64.b64encode(contract_pdf_bytes).decode("utf-8"),
        }
    ]

    terms_attachment = load_pdf_attachment_from_docs(TERMS_PDF_FILENAME)
    if terms_attachment:
        attachments.append(terms_attachment)

    privacy_attachment = load_pdf_attachment_from_docs(PRIVACY_PDF_FILENAME)
    if privacy_attachment:
        attachments.append(privacy_attachment)

    fair_usage_attachment = load_pdf_attachment_from_docs(FAIR_USAGE_PDF_FILENAME)
    if fair_usage_attachment:
        attachments.append(fair_usage_attachment)

    return attachments

# -----------------------------------------------------------------------------
# EMAIL
# -----------------------------------------------------------------------------

def send_customer_confirmation_email(signup, pdf_bytes):
    signup = normalize_signup_record(signup)
    if not resend.api_key or not RESEND_FROM_EMAIL or not signup.get("email"):
        logger.warning("Customer confirmation email skipped for signup %s", signup.get("id"))
        return False

    fix_join_html = ""
    if signup.get("fix_and_join") == "Yes":
        fix_join_html = f"""
        <p style="margin:0 0 10px;"><strong>Fix &amp; Join applies:</strong> A one-off fee of £{signup.get('fix_and_join_fee') or FIX_AND_JOIN_FEE}
        applies and work remains subject to inspection and suitability.</p>
        """

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#0b0b0b;padding:30px;">
      <div style="max-width:560px;margin:auto;background:#171717;border-radius:16px;padding:30px;color:#ffffff;border:1px solid #2a2a2a;">

        <h2 style="color:#ff6a00;margin:0 0 12px;">You're covered</h2>

        <p style="color:#c5c5c5;line-height:1.6;margin:0 0 20px;">
          Thanks for choosing {COMPANY_NAME}. Your service plan is now live.
        </p>

        <div style="background:#202020;padding:16px;border-radius:12px;margin:20px 0;line-height:1.8;">
          <p style="margin:0 0 8px;"><strong>Name:</strong> {signup.get('full_name') or '-'}</p>
          <p style="margin:0 0 8px;"><strong>Plan:</strong> {signup.get('selected_plan') or '-'}</p>
          <p style="margin:0;"><strong>Monthly:</strong> £{signup.get('monthly_price') or '-'}</p>
        </div>

        {fix_join_html}

        <p style="margin:20px 0 10px;"><strong>What happens next:</strong></p>
        <ul style="margin:0 0 20px 18px;line-height:1.8;color:#c5c5c5;">
          <li>We review your signup</li>
          <li>We’ll contact you if anything is needed</li>
          <li>Your cover is now active</li>
        </ul>

        <p style="margin:20px 0 10px;"><strong>Attached to this email:</strong></p>
        <ul style="margin:0 0 20px 18px;line-height:1.8;color:#c5c5c5;">
          <li>Your signed service plan agreement</li>
          <li>Our Terms and Conditions PDF</li>
          <li>Our Privacy Policy PDF</li>
          <li>Our Fair Usage Policy PDF</li>
        </ul>

        <p style="margin:20px 0 0;color:#c5c5c5;">
          Need anything? Call us on <strong style="color:#ffffff;">{COMPANY_PHONE}</strong>
        </p>

      </div>
    </div>
    """

    attachments = build_email_attachments(signup, pdf_bytes)

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [signup["email"]],
            "subject": f"Your {COMPANY_NAME} Service Plan Confirmation",
            "html": html,
            "attachments": attachments,
        }
    )
    return True


def send_admin_notification_email(signup, pdf_bytes):
    signup = normalize_signup_record(signup)
    if not resend.api_key or not RESEND_FROM_EMAIL or not ADMIN_NOTIFICATION_EMAIL:
        logger.warning("Admin notification email skipped for signup %s", signup.get("id"))
        return False

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222;">
      <h2>New service plan signup received</h2>

      <p><strong>Name:</strong> {signup.get('full_name') or '-'}</p>
      <p><strong>Email:</strong> {signup.get('email') or '-'}</p>
      <p><strong>Phone:</strong> {signup.get('phone') or '-'}</p>
      <p><strong>Address:</strong> {build_full_address(signup) or '-'}</p>

      <p><strong>Plan:</strong> {signup.get('selected_plan') or '-'} - £{signup.get('monthly_price') or '-'}/month</p>
      <p><strong>Boiler Broken:</strong> {signup.get('boiler_broken') or '-'}</p>
      <p><strong>Fix &amp; Join:</strong> {signup.get('fix_and_join') or 'No'}</p>
      <p><strong>Fix &amp; Join Fee:</strong> £{signup.get('fix_and_join_fee') or '-'}</p>

      <p>Attached:</p>
      <ul>
        <li>Signed agreement PDF</li>
        <li>Terms PDF</li>
        <li>Privacy PDF</li>
        <li>Fair Usage PDF</li>
      </ul>
    </div>
    """

    attachments = build_email_attachments(signup, pdf_bytes)

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [ADMIN_NOTIFICATION_EMAIL],
            "subject": f"New Service Plan Signup - {signup.get('full_name') or 'Customer'}",
            "html": html,
            "attachments": attachments,
        }
    )
    return True


def send_service_reminder_email(signup):
    signup = normalize_signup_record(signup)
    if not resend.api_key or not RESEND_FROM_EMAIL or not signup.get("email"):
        logger.warning("Reminder email skipped for signup %s", signup.get("id"))
        return False

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222;">
      <h2>Your annual service is coming up</h2>

      <p>Hello {signup.get('full_name') or ''},</p>

      <p>This is a reminder that your service plan annual service is due in around 1 month.</p>

      <p><strong>Plan:</strong> {signup.get('selected_plan') or '-'}</p>
      <p><strong>Address:</strong> {build_full_address(signup) or '-'}</p>

      <p>Please contact us on {COMPANY_PHONE} or reply to this email to arrange a convenient date.</p>

      <p>Thanks,<br>{COMPANY_NAME}</p>
    </div>
    """

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [signup["email"]],
            "subject": f"{COMPANY_NAME} annual service reminder",
            "html": html,
        }
    )
    return True


def send_payment_link_email(signup, payment_url):
    signup = normalize_signup_record(signup)
    if not resend.api_key or not RESEND_FROM_EMAIL or not signup.get("email") or not payment_url:
        logger.warning("Payment link email skipped for signup %s", signup.get("id"))
        return False

    fix_join_html = ""
    if signup.get("fix_and_join") == "Yes":
        fix_join_html = f"""
        <p><strong>Fix &amp; Join fee:</strong> £{signup.get('fix_and_join_fee') or FIX_AND_JOIN_FEE}</p>
        """

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222;">
      <h2>{COMPANY_NAME} payment link</h2>

      <p>Hello {signup.get('full_name') or ''},</p>
      <p>Please use the link below to continue with your service plan payment.</p>

      <p><strong>Plan:</strong> {signup.get('selected_plan') or '-'}</p>
      <p><strong>Monthly price:</strong> £{signup.get('monthly_price') or '-'}</p>
      {fix_join_html}

      <p>
        <a href="{payment_url}" style="display:inline-block;padding:12px 18px;background:#ff6a00;color:#ffffff;text-decoration:none;border-radius:8px;font-weight:bold;">
          Continue to payment
        </a>
      </p>

      <p>If the button does not work, copy and paste this link into your browser:</p>
      <p>{payment_url}</p>

      <p>Thanks,<br>{COMPANY_NAME}</p>
    </div>
    """

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [signup["email"]],
            "subject": f"{COMPANY_NAME} payment link",
            "html": html,
        }
    )
    return True


def send_one_off_customer_confirmation_email(booking):
    booking = normalize_one_off_booking(booking)
    if not resend.api_key or not RESEND_FROM_EMAIL or not booking.get("email"):
        logger.warning("Customer confirmation email skipped for one-off booking %s", booking.get("id"))
        return False

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#0b0b0b;padding:30px;">
      <div style="max-width:560px;margin:auto;background:#171717;border-radius:16px;padding:30px;color:#ffffff;border:1px solid #2a2a2a;">
        <h2 style="color:#ff6a00;margin:0 0 12px;">Booking received</h2>

        <p style="color:#c5c5c5;line-height:1.6;margin:0 0 20px;">
          Thanks for booking with {COMPANY_NAME}. Your £{ONE_OFF_SERVICE_PRICE} one-off annual boiler service has been received.
        </p>

        <div style="background:#202020;padding:16px;border-radius:12px;margin:20px 0;line-height:1.8;">
          <p style="margin:0 0 8px;"><strong>Name:</strong> {booking.get('full_name') or '-'}</p>
          <p style="margin:0 0 8px;"><strong>Booking:</strong> One-Off Annual Boiler Service</p>
          <p style="margin:0 0 8px;"><strong>Payment status:</strong> {booking.get('payment_status') or '-'}</p>
          <p style="margin:0;"><strong>Address:</strong> {build_full_address(booking) or '-'}</p>
        </div>

        <p style="margin:0 0 12px;color:#c5c5c5;">
          We’ll contact you to confirm the appointment once we’ve reviewed your preferred dates and access notes.
        </p>

        <p style="margin:0 0 8px;"><strong>Phone:</strong> {booking.get('phone') or '-'}</p>
        <p style="margin:0 0 8px;"><strong>Email:</strong> {booking.get('email') or '-'}</p>
        <p style="margin:0 0 8px;"><strong>Preferred dates / notes:</strong> {booking.get('preferred_dates') or booking.get('customer_notes') or '-'}</p>
        <p style="margin:0 0 20px;"><strong>Access notes:</strong> {booking.get('access_notes') or '-'}</p>

        <p style="margin:20px 0 0;color:#c5c5c5;">
          Need anything? Call us on <strong style="color:#ffffff;">{COMPANY_PHONE}</strong> or email
          <strong style="color:#ffffff;">{COMPANY_EMAIL}</strong>.
        </p>
      </div>
    </div>
    """

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [booking["email"]],
            "subject": "Your SJM Heating annual boiler service booking",
            "html": html,
        }
    )
    return True


def send_one_off_admin_notification_email(booking):
    booking = normalize_one_off_booking(booking)
    if not resend.api_key or not RESEND_FROM_EMAIL or not ADMIN_NOTIFICATION_EMAIL:
        logger.warning("Admin notification email skipped for one-off booking %s", booking.get("id"))
        return False

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222;">
      <h2>New one-off annual boiler service booking</h2>

      <p><strong>Type:</strong> One-Off Annual Boiler Service</p>
      <p><strong>Amount:</strong> £{ONE_OFF_SERVICE_PRICE}</p>
      <p><strong>Payment status:</strong> {booking.get('payment_status') or '-'}</p>
      <p><strong>Stripe session:</strong> {booking.get('stripe_session_id') or '-'}</p>
      <p><strong>Stripe payment reference:</strong> {booking.get('stripe_payment_intent_id') or '-'}</p>

      <hr>

      <p><strong>Name:</strong> {booking.get('full_name') or '-'}</p>
      <p><strong>Email:</strong> {booking.get('email') or '-'}</p>
      <p><strong>Phone:</strong> {booking.get('phone') or '-'}</p>
      <p><strong>Address:</strong> {build_full_address(booking) or '-'}</p>
      <p><strong>Boiler make:</strong> {booking.get('boiler_make') or '-'}</p>
      <p><strong>Boiler model:</strong> {booking.get('boiler_model') or '-'}</p>
      <p><strong>Preferred dates / notes:</strong> {booking.get('preferred_dates') or '-'}</p>
      <p><strong>Customer notes:</strong> {booking.get('customer_notes') or '-'}</p>
      <p><strong>Access notes:</strong> {booking.get('access_notes') or '-'}</p>
    </div>
    """

    resend.Emails.send(
        {
            "from": RESEND_FROM_EMAIL,
            "to": [ADMIN_NOTIFICATION_EMAIL],
            "subject": "New one-off annual boiler service booking",
            "html": html,
        }
    )
    return True


def send_post_payment_emails(signup_id):
    ensure_extra_columns()
    signup = fetch_signup(signup_id)
    if not signup:
        logger.warning("Signup %s not found for post-payment emails.", signup_id)
        return

    pdf_bytes, signup = get_or_create_contract_pdf_bytes(signup_id)
    if not pdf_bytes or not signup:
        logger.error("Could not build or fetch contract PDF for signup %s", signup_id)
        return

    if not signup.get("customer_email_sent"):
        try:
            if send_customer_confirmation_email(signup, pdf_bytes):
                update_signup_email_status(signup_id, customer_sent=True)
        except Exception:
            logger.exception("Failed sending customer confirmation email for signup %s", signup_id)

    if not signup.get("admin_email_sent"):
        try:
            if send_admin_notification_email(signup, pdf_bytes):
                update_signup_email_status(signup_id, admin_sent=True)
        except Exception:
            logger.exception("Failed sending admin notification email for signup %s", signup_id)


def send_one_off_post_payment_emails(booking_id):
    ensure_extra_columns()
    booking = fetch_one_off_booking(booking_id)
    if not booking:
        logger.warning("One-off booking %s not found for post-payment emails.", booking_id)
        return

    if not booking.get("customer_email_sent"):
        try:
            if send_one_off_customer_confirmation_email(booking):
                update_one_off_booking_email_status(booking_id, customer_sent=True)
        except Exception:
            logger.exception("Failed sending customer confirmation email for one-off booking %s", booking_id)

    if not booking.get("admin_email_sent"):
        try:
            if send_one_off_admin_notification_email(booking):
                update_one_off_booking_email_status(booking_id, admin_sent=True)
        except Exception:
            logger.exception("Failed sending admin notification email for one-off booking %s", booking_id)

# -----------------------------------------------------------------------------
# STRIPE EVENT HANDLING
# -----------------------------------------------------------------------------

def handle_completed_checkout_session(checkout):
    ensure_extra_columns()
    metadata = get_checkout_metadata(checkout)
    booking_type = metadata.get("booking_type")

    if booking_type == ONE_OFF_SERVICE_BOOKING_TYPE:
        booking_id = metadata.get("booking_id")
        if not booking_id:
            logger.warning("Completed one-off Stripe session had no booking_id metadata.")
            return

        booking = fetch_one_off_booking(booking_id)
        if not booking:
            logger.warning("One-off booking %s not found for completed Stripe session.", booking_id)
            return

        if booking.get("payment_status") != "Paid":
            mark_one_off_booking_paid(booking_id, checkout)

        send_one_off_post_payment_emails(booking_id)
        return

    signup_id = metadata.get("signup_id")
    if not signup_id:
        logger.warning("Completed Stripe session had no signup_id metadata.")
        return

    signup = fetch_signup(signup_id)
    if not signup:
        logger.warning("Signup %s not found for completed Stripe session.", signup_id)
        return

    if signup.get("payment_status") != "Paid":
        mark_signup_paid(signup_id, checkout=checkout)

    send_post_payment_emails(signup_id)

# -----------------------------------------------------------------------------
# ROUTES
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", plan_prices=PLAN_PRICES)


@app.route("/signup", methods=["GET"])
def signup():
    return render_template(
        "signup.html",
        plan_prices=PLAN_PRICES,
        journey_mode="signup",
    )


@app.route("/fix-and-join", methods=["GET"])
def fix_and_join():
    return render_template(
        "signup.html",
        plan_prices=PLAN_PRICES,
        journey_mode="fix_and_join",
    )


@app.route("/book-annual-service", methods=["GET"])
def book_annual_service():
    return render_template("book_annual_service.html")

@app.route("/success")
def success():
    signup_id = request.args.get("signup_id")
    signup_row = None

    if signup_id:
        try:
            signup_row = fetch_signup(signup_id)
        except Exception:
            logger.exception("Could not fetch signup %s for success page.", signup_id)
            signup_row = None

    return render_template("success.html", signup=signup_row)


@app.route("/annual-service/success")
def annual_service_success():
    booking_id = request.args.get("booking_id")
    booking = None

    if booking_id:
        try:
            booking = fetch_one_off_booking(booking_id)
        except Exception:
            logger.exception("Could not fetch one-off booking %s for success page.", booking_id)
            booking = None

    return render_template("annual_service_success.html", booking=booking)


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


@app.route("/fair-usage")
def fair_usage():
    if docs_file_exists(FAIR_USAGE_PDF_FILENAME):
        return send_from_directory(DOCS_DIR, FAIR_USAGE_PDF_FILENAME)
    return render_template("fair_usage.html")


@app.route("/health")
def health():
    return {"status": "ok"}, 200


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
        logger.exception("Postcode lookup failed for %s", postcode)
        return {"error": "Lookup failed"}, 500


@app.route("/submit", methods=["POST"])
def submit():
    ensure_extra_columns()
    journey_mode = clean(request.form.get("journey_mode")) or "signup"
    error_route = "fix_and_join" if journey_mode == "fix_and_join" else "signup"

    if looks_like_bot_submission(request.form):
        flash("We could not verify your submission. Please try again.", "error")
        return redirect(url_for(error_route))

    missing_env = validate_required_env()
    if missing_env:
        flash(f"Server configuration error: missing {', '.join(missing_env)}", "error")
        return redirect(url_for(error_route))

    name = clean(request.form.get("full_name"))
    email = clean(request.form.get("email"))
    phone = clean(request.form.get("phone"))

    address_line_1 = clean(request.form.get("address_line_1"))
    address_line_2 = clean(request.form.get("address_line_2"))
    city = clean(request.form.get("city"))
    postcode = clean(request.form.get("postcode")).upper()

    broken = "Yes" if journey_mode == "fix_and_join" else clean(request.form.get("boiler_broken"))
    under3 = clean(request.form.get("boiler_under_3_years"))
    warranty = clean(request.form.get("boiler_warranty_valid"))
    boiler_make = clean(request.form.get("boiler_make"))
    boiler_model = clean(request.form.get("boiler_model"))
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
        return redirect(url_for(error_route))

    if broken not in ["Yes", "No"]:
        flash("Please answer whether the boiler is currently broken.", "error")
        return redirect(url_for(error_route))

    if broken == "No":
        if under3 not in ["Yes", "No"] or warranty not in ["Yes", "No"]:
            flash("Please answer the age and warranty questions.", "error")
            return redirect(url_for(error_route))
    else:
        under3 = ""
        warranty = ""

    eligible_plans, _ = get_eligible_plans(broken, under3, warranty)

    if plan not in eligible_plans:
        if broken == "Yes":
            flash(
                "Because your boiler is currently broken, please choose Standard or Complete through Fix & Join.",
                "error",
            )
            return redirect(url_for("fix_and_join"))

        if under3 == "No" and warranty == "No":
            flash(
                "Because your boiler is over 3 years old or outside manufacturer warranty, the Essential Plan isn’t available. Please choose Standard or Complete.",
                "error",
            )
        else:
            flash("Please choose one of the available service plans.", "error")
        return redirect(url_for("signup"))

    if not signature_name:
        flash("Please enter your typed signature name.", "error")
        return redirect(url_for(error_route))

    if not signature_data or not signature_data.startswith("data:image/png;base64,"):
        flash("Please provide your drawn signature.", "error")
        return redirect(url_for(error_route))

    if not accepted_terms or not accepted_privacy or not accepted_fair_usage:
        flash("Please accept the Terms, Privacy Policy, and Fair Usage Policy.", "error")
        return redirect(url_for(error_route))

    if not STRIPE_PRICES.get(plan):
        flash(f"Stripe price is not configured for {plan}.", "error")
        return redirect(url_for(error_route))

    now = datetime.now(UTC)
    reminder_due_date = now + timedelta(days=335)
    boiler_age = ""
    if under3 == "Yes":
        boiler_age = "Under 3 years"
    elif under3 == "No":
        boiler_age = "Over 3 years"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signups (
                    full_name,
                    email,
                    phone,
                    address_line1,
                    address_line2,
                    address_line_1,
                    address_line_2,
                    city,
                    postcode,
                    boiler_make,
                    boiler_model,
                    boiler_age,
                    boiler_working,
                    selected_plan,
                    monthly_price,
                    boiler_broken,
                    boiler_under_3_years,
                    boiler_warranty_valid,
                    existing_customer,
                    fix_and_join,
                    fix_and_join_fee,
                    signature_name,
                    signature_data,
                    signature,
                    signed_at,
                    accepted_terms,
                    accepted_privacy,
                    accepted_fair_usage,
                    terms_accepted,
                    privacy_accepted,
                    terms_version,
                    privacy_version,
                    status,
                    payment_status,
                    created_at,
                    updated_at,
                    reminder_due_date,
                    reminder_sent,
                    ip_address,
                    user_agent
                )
                VALUES (
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                )
                RETURNING id
                """,
                (
                    name,
                    email,
                    phone,
                    address_line_1,
                    address_line_2,
                    address_line_1,
                    address_line_2,
                    city,
                    postcode,
                    boiler_make,
                    boiler_model,
                    boiler_age,
                    "No" if broken == "Yes" else "Yes",
                    plan,
                    PLAN_PRICES[plan],
                    broken,
                    under3,
                    warranty,
                    "",
                    fix_join,
                    fix_and_join_fee,
                    signature_name,
                    signature_data,
                    signature_data,
                    now,
                    accepted_terms,
                    accepted_privacy,
                    accepted_fair_usage,
                    1 if accepted_terms else 0,
                    1 if accepted_privacy else 0,
                    TERMS_VERSION,
                    PRIVACY_VERSION,
                    "New",
                    "Not sent",
                    now,
                    now,
                    reminder_due_date,
                    False,
                    request.headers.get("X-Forwarded-For", request.remote_addr or ""),
                    request.headers.get("User-Agent", ""),
                ),
            )
            signup_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    try:
        checkout_session = create_checkout_session(
            signup_id=signup_id,
            email=email,
            plan=plan,
            fix_join=fix_join,
            fix_and_join_fee=fix_and_join_fee,
        )
    except Exception as e:
        db_execute(
            """
            UPDATE signups
            SET payment_status='Failed',
                updated_at=%s
            WHERE id=%s
            """,
            (datetime.now(UTC), signup_id),
            commit=True,
        )
        logger.exception("Unable to create Stripe checkout for signup %s", signup_id)
        flash(f"Unable to create Stripe checkout: {e}", "error")
        return redirect(url_for(error_route))

    mark_payment_link_generated(signup_id, checkout_session.url, checkout_session.id)
    return redirect(checkout_session.url)


@app.route("/annual-service/submit", methods=["POST"])
def submit_annual_service():
    ensure_extra_columns()

    if looks_like_bot_submission(request.form):
        flash("We could not verify your submission. Please try again.", "error")
        return redirect(url_for("book_annual_service"))

    missing_env = validate_required_env(include_monthly_prices=False, include_one_off_price=True)
    if missing_env:
        flash(
            "Online payment for annual services is not configured yet. Please contact us and we’ll book it in manually.",
            "error",
        )
        logger.error("One-off service booking blocked by missing env: %s", ", ".join(missing_env))
        return redirect(url_for("book_annual_service"))

    first_name = clean(request.form.get("first_name"))
    last_name = clean(request.form.get("last_name"))
    full_name = " ".join(part for part in [first_name, last_name] if part)
    email = clean(request.form.get("email"))
    phone = clean(request.form.get("phone"))
    address_line_1 = clean(request.form.get("address_line_1"))
    address_line_2 = clean(request.form.get("address_line_2"))
    town = clean(request.form.get("town"))
    county = clean(request.form.get("county"))
    postcode = clean(request.form.get("postcode")).upper()
    boiler_make = clean(request.form.get("boiler_make"))
    boiler_model = clean(request.form.get("boiler_model"))
    preferred_dates = clean(request.form.get("preferred_dates"))
    customer_notes = clean(request.form.get("customer_notes"))
    access_notes = clean(request.form.get("access_notes"))
    acknowledged_contact = checkbox_to_bool(request.form.get("acknowledged_contact"))

    if not first_name or not last_name or not email or not phone or not address_line_1 or not town or not postcode:
        flash("Please complete the required booking details.", "error")
        return redirect(url_for("book_annual_service"))

    if not acknowledged_contact:
        flash("Please confirm that we’ll contact you to arrange the appointment.", "error")
        return redirect(url_for("book_annual_service"))

    now = datetime.now(UTC)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO one_off_service_bookings (
                    booking_type,
                    first_name,
                    last_name,
                    full_name,
                    email,
                    phone,
                    address_line_1,
                    address_line_2,
                    town,
                    county,
                    postcode,
                    boiler_make,
                    boiler_model,
                    customer_notes,
                    access_notes,
                    preferred_dates,
                    acknowledged_contact,
                    payment_status,
                    status,
                    appointment_status,
                    assigned_engineer,
                    created_at,
                    updated_at
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
                """,
                (
                    ONE_OFF_SERVICE_BOOKING_TYPE,
                    first_name,
                    last_name,
                    full_name,
                    email,
                    phone,
                    address_line_1,
                    address_line_2,
                    town,
                    county,
                    postcode,
                    boiler_make,
                    boiler_model,
                    customer_notes,
                    access_notes,
                    preferred_dates,
                    acknowledged_contact,
                    "Pending",
                    "New",
                    "To arrange",
                    DEFAULT_ASSIGNED_ENGINEER,
                    now,
                    now,
                ),
            )
            booking_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    booking = fetch_one_off_booking(booking_id)

    try:
        checkout_session = create_one_off_service_checkout_session(booking)
    except Exception:
        db_execute(
            """
            UPDATE one_off_service_bookings
            SET payment_status='Failed',
                updated_at=%s
            WHERE id=%s
            """,
            (datetime.now(UTC), booking_id),
            commit=True,
        )
        logger.exception("Unable to create Stripe checkout for one-off booking %s", booking_id)
        flash(
            "We couldn’t start online payment just now. Please try again or contact us and we’ll arrange the booking manually.",
            "error",
        )
        return redirect(url_for("book_annual_service"))

    mark_one_off_checkout_created(booking_id, checkout_session)
    return redirect(checkout_session.url)


@app.route("/stripe/success")
def stripe_success():
    session_id = request.args.get("session_id")

    if not session_id:
        return render_template("stripe_success.html")

    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
    except Exception as e:
        logger.exception("Could not verify Stripe success session.")
        flash(f"Could not verify payment session: {e}", "error")
        return render_template("stripe_success.html")

    metadata = get_checkout_metadata(checkout)
    booking_type = metadata.get("booking_type")
    signup_id = metadata.get("signup_id")
    booking_id = metadata.get("booking_id")

    paid_statuses = {"paid", "no_payment_required"}
    checkout_status = get_checkout_value(checkout, "payment_status")

    if checkout_status in paid_statuses:
        try:
            handle_completed_checkout_session(checkout)
        except Exception:
            logger.exception("Failed handling completed Stripe checkout session %s", session_id)
    else:
        logger.warning(
            "Stripe success page reached for session %s but payment_status was %s",
            session_id,
            checkout_status,
        )

    if booking_type == ONE_OFF_SERVICE_BOOKING_TYPE:
        if not booking_id:
            flash("Could not match this payment to your annual service booking.", "error")
            return render_template("stripe_success.html")
        return redirect(url_for("annual_service_success", booking_id=booking_id))

    if not signup_id:
        flash("Could not match this payment to a signup.", "error")
        return render_template("stripe_success.html")

    return redirect(url_for("success", signup_id=signup_id))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")

    if not STRIPE_WEBHOOK_SECRET:
        logger.error("Stripe webhook called but STRIPE_WEBHOOK_SECRET is not configured.")
        return {"error": "Webhook secret not configured"}, 500

    try:
        event = stripe.Webhook.construct_event(
            payload=payload,
            sig_header=sig_header,
            secret=STRIPE_WEBHOOK_SECRET,
        )
    except ValueError:
        logger.exception("Invalid Stripe webhook payload.")
        return {"error": "Invalid payload"}, 400
    except stripe.error.SignatureVerificationError:
        logger.exception("Invalid Stripe webhook signature.")
        return {"error": "Invalid signature"}, 400

    event_type = event["type"]
    logger.info("Stripe webhook received: %s", event_type)

    try:
        if event_type == "checkout.session.completed":
            checkout = event["data"]["object"]
            handle_completed_checkout_session(checkout)
    except Exception:
        logger.exception("Error processing Stripe webhook event %s", event_type)
        return {"error": "Webhook processing failed"}, 500

    return {"received": True}, 200


@app.route("/annual-service/cancel")
def annual_service_cancel():
    booking_id = request.args.get("booking_id")
    if booking_id:
        try:
            mark_one_off_booking_cancelled(booking_id)
        except Exception:
            logger.exception("Could not mark one-off booking %s as cancelled.", booking_id)

    flash("Your payment was cancelled. You can review your annual service booking and try again.", "warning")
    return render_template(
        "stripe-cancel.html",
        return_url=url_for("book_annual_service"),
        return_text="Back to annual service form",
        cancel_title="Payment cancelled",
        cancel_message="Your annual service payment was not completed.",
    )


@app.route("/stripe/cancel")
def stripe_cancel():
    return render_template(
        "stripe-cancel.html",
        return_url=url_for("signup"),
        return_text="Try again",
        cancel_title="Payment cancelled",
        cancel_message="Your payment was not completed.",
    )


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
    ensure_extra_columns()

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups ORDER BY id DESC")
            rows = normalize_signup_rows(cur.fetchall())

            cur.execute("SELECT COUNT(*) FROM signups")
            total = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) FROM signups WHERE payment_status='Paid'")
            paid = cur.fetchone()["count"]

            cur.execute("SELECT * FROM one_off_service_bookings ORDER BY id DESC")
            one_off_rows = normalize_one_off_rows(cur.fetchall())

            cur.execute("SELECT COUNT(*) FROM one_off_service_bookings")
            one_off_total = cur.fetchone()["count"]

            cur.execute("SELECT COUNT(*) FROM one_off_service_bookings WHERE payment_status='Paid'")
            one_off_paid = cur.fetchone()["count"]
    finally:
        conn.close()

    stats = {
        "total": total,
        "paid": paid,
        "conversion": round((paid / total) * 100, 1) if total else 0,
        "one_off_total": one_off_total,
        "one_off_paid": one_off_paid,
    }

    return render_template(
        "admin.html",
        signups=rows,
        one_off_bookings=one_off_rows,
        stats=stats,
        one_off_statuses=ONE_OFF_BOOKING_STATUSES,
        appointment_statuses=ONE_OFF_APPOINTMENT_STATUSES,
        build_maps_link=build_maps_link,
        build_directions_link=build_directions_link,
        build_full_address=build_full_address,
    )


@app.route("/admin/annual-service/<int:booking_id>/update", methods=["POST"])
@login_required
def update_annual_service_booking(booking_id):
    booking = fetch_one_off_booking(booking_id)
    if not booking:
        flash("Annual service booking not found.", "error")
        return redirect(url_for("admin"))

    status = clean(request.form.get("status"))
    appointment_status = clean(request.form.get("appointment_status"))
    assigned_engineer = clean(request.form.get("assigned_engineer")) or DEFAULT_ASSIGNED_ENGINEER
    appointment_date = clean(request.form.get("appointment_date")) or None
    appointment_time = clean(request.form.get("appointment_time")) or None

    if status not in ONE_OFF_BOOKING_STATUSES:
        flash("Please choose a valid booking status.", "error")
        return redirect(url_for("admin"))

    if appointment_status not in ONE_OFF_APPOINTMENT_STATUSES:
        flash("Please choose a valid appointment status.", "error")
        return redirect(url_for("admin"))

    db_execute(
        """
        UPDATE one_off_service_bookings
        SET status=%s,
            appointment_status=%s,
            assigned_engineer=%s,
            appointment_date=%s,
            appointment_time=%s,
            updated_at=%s
        WHERE id=%s
        """,
        (
            status,
            appointment_status,
            assigned_engineer,
            appointment_date,
            appointment_time,
            datetime.now(UTC),
            booking_id,
        ),
        commit=True,
    )

    flash("Annual service booking updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/resend/<int:id>", methods=["POST"])
@login_required
def resend_payment(id):
    missing_env = validate_required_env()
    if missing_env:
        flash(f"Server configuration error: missing {', '.join(missing_env)}", "error")
        return redirect(url_for("admin"))

    signup = fetch_signup(id)
    if not signup:
        flash("Signup not found.", "error")
        return redirect(url_for("admin"))

    plan = signup["selected_plan"]

    if not STRIPE_PRICES.get(plan):
        flash(f"Stripe price is not configured for {plan}.", "error")
        return redirect(url_for("admin"))

    try:
        checkout_session = create_checkout_session(
            signup_id=id,
            email=signup.get("email"),
            plan=plan,
            fix_join=signup.get("fix_and_join") or "No",
            fix_and_join_fee=signup.get("fix_and_join_fee") or "",
        )
        mark_payment_link_generated(id, checkout_session.url, checkout_session.id)
    except Exception:
        logger.exception("Failed generating payment link for signup %s", id)
        flash("Could not generate a new payment link.", "error")
        return redirect(url_for("admin"))

    email_note = ""
    try:
        if send_payment_link_email(signup, checkout_session.url):
            email_note = " and emailed to the customer"
    except Exception:
        logger.exception("Failed emailing payment link for signup %s", id)
        email_note = " but email delivery failed"

    flash(f"New payment link generated{email_note}.", "success")
    return redirect(url_for("admin"))


@app.route("/admin/run-reminders", methods=["POST", "GET"])
@login_required
def run_reminders():
    ensure_extra_columns()

    rows = db_execute(
        """
        SELECT * FROM signups
        WHERE reminder_sent = FALSE
          AND reminder_due_date IS NOT NULL
          AND reminder_due_date <= %s
          AND payment_status = 'Paid'
        ORDER BY id ASC
        """,
        (datetime.now(UTC),),
        fetchall=True,
    ) or []

    sent_count = 0
    for row in rows:
        try:
            if send_service_reminder_email(row):
                mark_reminder_sent(row["id"])
                sent_count += 1
        except Exception:
            logger.exception("Failed sending reminder for signup %s", row.get("id"))

    flash(f"Reminder run complete. Emails sent: {sent_count}", "success")
    return redirect(url_for("admin"))


@app.route("/admin/regenerate-pdfs", methods=["POST"])
@login_required
def regenerate_pdfs():
    try:
        regenerate_all_contract_pdfs()
        flash("All contract PDFs regenerated successfully.", "success")
    except Exception:
        logger.exception("Failed regenerating PDFs.")
        flash("Could not regenerate all PDFs.", "error")
    return redirect(url_for("admin"))

def regenerate_all_contract_pdfs():
    rows = db_execute("SELECT id FROM signups ORDER BY id ASC", fetchall=True) or []
    for row in rows:
        signup = fetch_signup(row["id"])
        if signup:
            pdf_bytes = build_contract_pdf_bytes(signup)
            save_contract_pdf_to_db(row["id"], pdf_bytes)

@app.route("/admin/export.csv")
@login_required
def export_csv():
    ensure_extra_columns()

    rows = db_execute(
        "SELECT * FROM signups ORDER BY id DESC",
        fetchall=True,
    ) or []

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
        "Customer Email Sent",
        "Admin Email Sent",
        "Reminder Due Date",
        "Reminder Sent",
        "Reminder Sent At",
        "Payment Completed At",
        "Last Payment Link Sent At",
        "Contract PDF Stored",
        "Contract PDF Filename",
        "Contract PDF Generated At",
        "Stripe Checkout URL",
        "Stripe Checkout Session ID",
        "Stripe Customer ID",
        "Stripe Subscription ID",
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
            row.get("customer_email_sent"),
            row.get("admin_email_sent"),
            row.get("reminder_due_date"),
            row.get("reminder_sent"),
            row.get("reminder_sent_at"),
            row.get("payment_completed_at"),
            row.get("last_payment_link_sent_at"),
            "Yes" if row.get("contract_pdf") else "No",
            row.get("contract_pdf_filename"),
            row.get("contract_pdf_generated_at"),
            row.get("stripe_checkout_url"),
            row.get("stripe_checkout_session_id"),
            row.get("stripe_customer_id"),
            row.get("stripe_subscription_id"),
        ])

    response = Response(output.getvalue(), mimetype="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=sjm_signups.csv"
    return response


@app.route("/admin/contract/<int:signup_id>")
@login_required
def admin_contract(signup_id):
    signup = fetch_signup(signup_id)
    if not signup:
        flash("Signup not found.", "error")
        return redirect(url_for("admin"))

    try:
        pdf_bytes = get_stored_contract_pdf_bytes(signup)
        if not pdf_bytes:
            pdf_bytes = build_contract_pdf_bytes(signup)
            save_contract_pdf_to_db(signup_id, pdf_bytes)
            signup = fetch_signup(signup_id)

        filename = signup.get("contract_pdf_filename") or f"sjm-service-plan-{signup_id}.pdf"
    except Exception:
        logger.exception("Failed building or loading PDF for signup %s", signup_id)
        flash("Could not generate contract PDF.", "error")
        return redirect(url_for("admin"))

    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.errorhandler(404)
def page_not_found(error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(error):
    logger.exception("Internal server error: %s", error)
    return render_template("500.html"), 500


if __name__ == "__main__":
    ensure_extra_columns()
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
