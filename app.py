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
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.lib.utils import ImageReader

load_dotenv()

app = Flask(__name__)

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-in-production")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-this-admin-password")
ADMIN_NOTIFICATION_EMAIL = os.environ.get("ADMIN_NOTIFICATION_EMAIL", "info@sjmheating.co.uk")

COMPANY_NAME = os.environ.get("COMPANY_NAME", "SJM Heating")
COMPANY_EMAIL = os.environ.get("COMPANY_EMAIL", "info@sjmheating.co.uk")
COMPANY_PHONE = os.environ.get("COMPANY_PHONE", "07XXXXXXXXX")
WHATSAPP_NUMBER = os.environ.get("WHATSAPP_NUMBER", "447XXXXXXXXX")
COMPANY_ADDRESS = os.environ.get("COMPANY_ADDRESS", "Your Company Address")
COMPANY_REG = os.environ.get("COMPANY_REG", "Company No. XXXXXXXX")
COMPANY_WEBSITE = os.environ.get("COMPANY_WEBSITE", "")
COMPANY_LOGO_PATH = os.environ.get("COMPANY_LOGO_PATH", "static/logo.png")
FAVICON_PATH = os.environ.get("FAVICON_PATH", "favicon.ico")

# Email / Resend
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", COMPANY_EMAIL).strip()
resend.api_key = os.environ.get("RESEND_API_KEY", "").strip()

# Stripe
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

STRIPE_PRICE_ESSENTIAL = os.environ.get("STRIPE_PRICE_ESSENTIAL", "").strip()
STRIPE_PRICE_STANDARD = os.environ.get("STRIPE_PRICE_STANDARD", "").strip()
STRIPE_PRICE_COMPLETE = os.environ.get("STRIPE_PRICE_COMPLETE", "").strip()

STRIPE_SUCCESS_URL = os.environ.get(
    "STRIPE_SUCCESS_URL",
    "http://127.0.0.1:5001/stripe/success?session_id={CHECKOUT_SESSION_ID}",
).strip()
STRIPE_CANCEL_URL = os.environ.get(
    "STRIPE_CANCEL_URL",
    "http://127.0.0.1:5001/stripe/cancel",
).strip()

DB_NAME = os.environ.get("DB_NAME", "sjm_service")
DB_USER = os.environ.get("DB_USER", "sjm_user")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "change-me")
DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = os.environ.get("DB_PORT", "5432")

TERMS_VERSION = os.environ.get("TERMS_VERSION", "v1.1")
PRIVACY_VERSION = os.environ.get("PRIVACY_VERSION", "v1.0")

TERMS_PDF_FILENAME = os.environ.get("TERMS_PDF_FILENAME", "docs/terms.pdf")
PRIVACY_PDF_FILENAME = os.environ.get("PRIVACY_PDF_FILENAME", "docs/privacy.pdf")

DUPLICATE_WINDOW_MINUTES = int(os.environ.get("DUPLICATE_WINDOW_MINUTES", "10"))

PLAN_PRICES = {
    "Essential": "11.00",
    "Standard": "17.99",
    "Complete": "23.99",
}

VALID_STATUSES = {"New", "Contacted", "Quoted", "Won", "Lost"}
VALID_BROKEN_VALUES = {"Yes", "No"}
VALID_EXISTING_CUSTOMER_VALUES = {"Yes", "No", ""}
VALID_CONTACT_TIMES = {"", "Morning", "Afternoon", "Evening", "Anytime"}
VALID_PLAN_VALUES = set(PLAN_PRICES.keys())
VALID_PAYMENT_STATUSES = {"Not sent", "Link sent", "Paid", "Failed", "Refunded"}

EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
UK_POSTCODE_REGEX = re.compile(
    r"^(GIR 0AA|[A-PR-UWYZ][A-HK-Y]?\d[\dA-Z]?\s?\d[ABD-HJLNP-UW-Z]{2})$",
    re.IGNORECASE,
)
PHONE_CLEAN_REGEX = re.compile(r"[^\d+]")


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def resend_is_configured():
    return bool(resend.api_key and get_resend_from_email())


def get_resend_from_email():
    return (RESEND_FROM_EMAIL or COMPANY_EMAIL or "").strip()


def stripe_is_configured():
    return bool(
        stripe.api_key
        and STRIPE_PRICE_ESSENTIAL
        and STRIPE_PRICE_STANDARD
        and STRIPE_PRICE_COMPLETE
    )


def get_stripe_price_id(plan_name):
    mapping = {
        "Essential": STRIPE_PRICE_ESSENTIAL,
        "Standard": STRIPE_PRICE_STANDARD,
        "Complete": STRIPE_PRICE_COMPLETE,
    }
    return mapping.get(plan_name, "")


def create_stripe_checkout_session(signup_id, full_name, email, selected_plan):
    price_id = get_stripe_price_id(selected_plan)
    if not price_id:
        raise ValueError(f"No Stripe price configured for plan: {selected_plan}")

    return stripe.checkout.Session.create(
        mode="subscription",
        success_url=STRIPE_SUCCESS_URL,
        cancel_url=STRIPE_CANCEL_URL,
        customer_email=email,
        line_items=[
            {
                "price": price_id,
                "quantity": 1,
            }
        ],
        metadata={
            "signup_id": str(signup_id),
            "full_name": full_name,
            "selected_plan": selected_plan,
        },
        subscription_data={
            "metadata": {
                "signup_id": str(signup_id),
                "full_name": full_name,
                "selected_plan": selected_plan,
            }
        },
    )


def safe_text(value):
    return str(value or "").strip()


# -----------------------------------------------------------------------------
# Database
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


def add_column_if_missing(conn, table_name, column_name, column_definition):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = %s AND column_name = %s
            """,
            (table_name, column_name),
        )
        if not cur.fetchone():
            cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS signups (
                    id SERIAL PRIMARY KEY,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    address_line1 TEXT NOT NULL,
                    address_line2 TEXT,
                    city TEXT NOT NULL,
                    postcode TEXT NOT NULL,
                    boiler_make TEXT,
                    boiler_model TEXT,
                    boiler_age TEXT,
                    existing_customer TEXT,
                    boiler_broken TEXT,
                    fix_and_join TEXT,
                    marketing_opt_in INTEGER DEFAULT 0,
                    selected_plan TEXT NOT NULL,
                    monthly_price TEXT NOT NULL,
                    contact_time TEXT,
                    priority TEXT,
                    signature TEXT,
                    signed_at TIMESTAMP,
                    terms_accepted INTEGER NOT NULL,
                    privacy_accepted INTEGER NOT NULL,
                    terms_version TEXT,
                    privacy_version TEXT,
                    ip_address TEXT,
                    user_agent TEXT,
                    status TEXT DEFAULT 'New',
                    admin_notes TEXT DEFAULT '',
                    payment_status TEXT DEFAULT 'Not sent',
                    stripe_checkout_url TEXT,
                    stripe_customer_id TEXT,
                    stripe_subscription_id TEXT,
                    stripe_checkout_session_id TEXT,
                    stripe_payment_link_sent_at TIMESTAMP
                )
                """
            )

        add_column_if_missing(conn, "signups", "updated_at", "TIMESTAMP")
        add_column_if_missing(conn, "signups", "contact_time", "TEXT")
        add_column_if_missing(conn, "signups", "priority", "TEXT")
        add_column_if_missing(conn, "signups", "signature", "TEXT")
        add_column_if_missing(conn, "signups", "signed_at", "TIMESTAMP")
        add_column_if_missing(conn, "signups", "terms_version", "TEXT")
        add_column_if_missing(conn, "signups", "privacy_version", "TEXT")
        add_column_if_missing(conn, "signups", "ip_address", "TEXT")
        add_column_if_missing(conn, "signups", "user_agent", "TEXT")
        add_column_if_missing(conn, "signups", "status", "TEXT DEFAULT 'New'")
        add_column_if_missing(conn, "signups", "admin_notes", "TEXT DEFAULT ''")
        add_column_if_missing(conn, "signups", "payment_status", "TEXT DEFAULT 'Not sent'")
        add_column_if_missing(conn, "signups", "stripe_checkout_url", "TEXT")
        add_column_if_missing(conn, "signups", "stripe_customer_id", "TEXT")
        add_column_if_missing(conn, "signups", "stripe_subscription_id", "TEXT")
        add_column_if_missing(conn, "signups", "stripe_checkout_session_id", "TEXT")
        add_column_if_missing(conn, "signups", "stripe_payment_link_sent_at", "TIMESTAMP")

        with conn.cursor() as cur:
            cur.execute("UPDATE signups SET updated_at = created_at WHERE updated_at IS NULL")
            cur.execute("UPDATE signups SET status = 'New' WHERE status IS NULL OR status = ''")
            cur.execute("UPDATE signups SET admin_notes = '' WHERE admin_notes IS NULL")
            cur.execute(
                "UPDATE signups SET payment_status = 'Not sent' WHERE payment_status IS NULL OR payment_status = ''"
            )
            cur.execute(
                "UPDATE signups SET terms_version = %s WHERE terms_version IS NULL OR terms_version = ''",
                (TERMS_VERSION,),
            )
            cur.execute(
                "UPDATE signups SET privacy_version = %s WHERE privacy_version IS NULL OR privacy_version = ''",
                (PRIVACY_VERSION,),
            )

        conn.commit()
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Auth / CSRF
# -----------------------------------------------------------------------------

def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)
    return wrapped_view


def generate_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf():
    sent_token = request.form.get("csrf_token", "")
    session_token = session.get("_csrf_token", "")
    if not sent_token or not session_token or not secrets.compare_digest(sent_token, session_token):
        abort(400, description="Invalid CSRF token.")


@app.context_processor
def inject_global_template_vars():
    return {
        "company_name": COMPANY_NAME,
        "company_email": COMPANY_EMAIL,
        "company_phone": COMPANY_PHONE,
        "whatsapp_number": WHATSAPP_NUMBER,
        "company_address": COMPANY_ADDRESS,
        "company_reg": COMPANY_REG,
        "company_website": COMPANY_WEBSITE,
        "terms_version": TERMS_VERSION,
        "privacy_version": PRIVACY_VERSION,
        "terms_pdf_filename": TERMS_PDF_FILENAME,
        "privacy_pdf_filename": PRIVACY_PDF_FILENAME,
        "csrf_token": generate_csrf_token(),
        "current_year": datetime.now(UTC).year,
        "favicon_path": FAVICON_PATH,
    }


# -----------------------------------------------------------------------------
# Validation / normalization
# -----------------------------------------------------------------------------

def clean_text(value, max_length=None):
    value = (value or "").strip()
    value = re.sub(r"\s+", " ", value)
    if max_length:
        value = value[:max_length]
    return value


def normalise_postcode(value):
    value = clean_text(value, 10).upper().replace(" ", "")
    if len(value) > 3:
        return f"{value[:-3]} {value[-3:]}"
    return value


def normalise_phone(value):
    value = clean_text(value, 30)
    return PHONE_CLEAN_REGEX.sub("", value)


def is_valid_email(value):
    return bool(EMAIL_REGEX.match(value or ""))


def is_valid_phone(value):
    digits = re.sub(r"\D", "", value or "")
    return 10 <= len(digits) <= 15


def is_valid_uk_postcode(value):
    return bool(UK_POSTCODE_REGEX.match((value or "").strip()))


def parse_signature(signature):
    if not signature.startswith("data:image/png;base64,"):
        return False
    try:
        base64.b64decode(signature.split(",", 1)[1], validate=True)
        return True
    except Exception:
        return False


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or ""


def determine_priority(boiler_broken, fix_and_join):
    if boiler_broken == "Yes":
        return "HIGH"
    if fix_and_join == "Yes":
        return "HIGH"
    return "NORMAL"


def is_duplicate_submission(conn, email, phone, postcode, selected_plan):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id
            FROM signups
            WHERE lower(email) = lower(%s)
              AND phone = %s
              AND upper(postcode) = upper(%s)
              AND selected_plan = %s
              AND created_at >= %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                email,
                phone,
                postcode,
                selected_plan,
                datetime.now(UTC) - timedelta(minutes=DUPLICATE_WINDOW_MINUTES),
            ),
        )
        return cur.fetchone() is not None


def build_pdf_attachment(pdf_path, filename):
    with open(pdf_path, "rb") as f:
        encoded_pdf = base64.b64encode(f.read()).decode("utf-8")
    return {
        "filename": filename,
        "content": encoded_pdf,
    }


# -----------------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------------

def send_customer_email(full_name, email, selected_plan, monthly_price, contact_time, priority, pdf_attachment=None):
    if not resend_is_configured():
        print("Resend not configured. Skipping customer email.")
        return

    urgency_block = ""
    if priority == "HIGH":
        urgency_block = """
        <p style="margin:0 0 16px 0;color:#ffb380;">
            We’ve marked this as a priority enquiry and a member of the team will review it as soon as possible.
        </p>
        """

    preferred_contact = contact_time or "Not specified"

    payload = {
        "from": f"{COMPANY_NAME} <{get_resend_from_email()}>",
        "to": [email],
        "subject": f"We’ve received your {COMPANY_NAME} enquiry",
        "html": f"""
        <div style="font-family:Arial,sans-serif;background:#0d0d0d;padding:32px;color:#ffffff;">
          <div style="max-width:640px;margin:0 auto;background:#171717;border:1px solid #2c2c2c;border-radius:20px;padding:32px;">
            <h1 style="margin:0 0 12px 0;color:#ff6a00;">{COMPANY_NAME}</h1>
            <h2 style="margin:0 0 16px 0;">Thanks for your enquiry, {full_name}</h2>
            <p style="margin:0 0 16px 0;">We’ve received your service plan enquiry successfully.</p>
            {urgency_block}
            <div style="background:#101010;border:1px solid #2b2b2b;border-radius:14px;padding:18px;margin:0 0 20px 0;">
              <p style="margin:0 0 10px 0;"><strong>Selected plan:</strong> {selected_plan}</p>
              <p style="margin:0 0 10px 0;"><strong>Monthly price:</strong> £{monthly_price}</p>
              <p style="margin:0;"><strong>Preferred contact time:</strong> {preferred_contact}</p>
            </div>
            <p style="margin:0 0 10px 0;"><strong>What happens next?</strong></p>
            <p style="margin:0 0 16px 0;">Our team will review your enquiry and contact you to confirm the next steps.</p>
            <p style="margin:0 0 16px 0;">A copy of your signed agreement is attached to this email.</p>
          </div>
        </div>
        """,
    }

    if pdf_attachment:
        payload["attachments"] = [pdf_attachment]

    try:
        response = resend.Emails.send(payload)
        print("Customer email sent:", response)
    except Exception as e:
        print("Customer email failed:", e)


def send_admin_email(full_name, email, phone, selected_plan, priority, boiler_broken, fix_and_join, contact_time, pdf_attachment=None):
    if not resend_is_configured():
        print("Resend not configured. Skipping admin email.")
        return

    subject_prefix = "HIGH priority signup" if priority == "HIGH" else "New signup"

    payload = {
        "from": f"{COMPANY_NAME} <{get_resend_from_email()}>",
        "to": [ADMIN_NOTIFICATION_EMAIL],
        "subject": f"{subject_prefix} – {full_name}",
        "html": f"""
        <div style="font-family:Arial,sans-serif;background:#0d0d0d;padding:32px;color:#ffffff;">
          <div style="max-width:640px;margin:0 auto;background:#171717;border:1px solid #2c2c2c;border-radius:20px;padding:32px;">
            <h1 style="margin-top:0;color:#ff6a00;">New Service Plan Signup</h1>
            <p><strong>Priority:</strong> {priority}</p>
            <p><strong>Name:</strong> {full_name}</p>
            <p><strong>Email:</strong> {email}</p>
            <p><strong>Phone:</strong> {phone}</p>
            <p><strong>Plan:</strong> {selected_plan}</p>
            <p><strong>Boiler broken:</strong> {boiler_broken}</p>
            <p><strong>Fix & Join:</strong> {fix_and_join}</p>
            <p><strong>Preferred contact time:</strong> {contact_time or "Not specified"}</p>
            <p><strong>Payment status:</strong> Not sent</p>
          </div>
        </div>
        """,
    }

    if pdf_attachment:
        payload["attachments"] = [pdf_attachment]

    try:
        response = resend.Emails.send(payload)
        print("Admin email sent:", response)
    except Exception as e:
        print("Admin email failed:", e)


# -----------------------------------------------------------------------------
# PDF generation
# -----------------------------------------------------------------------------

def build_contract_pdf(row):
    temp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    temp.close()

    pdf = pdf_canvas.Canvas(temp.name, pagesize=A4)
    width, height = A4

    accent = HexColor("#ff6a00")
    dark = HexColor("#111111")
    charcoal = HexColor("#1a1a1a")
    soft_bg = HexColor("#f6f6f6")
    mid_grey = HexColor("#666666")
    line_grey = HexColor("#dddddd")
    white = HexColor("#ffffff")

    left = 18 * mm
    right = width - 18 * mm
    content_width = right - left

    header_height = 34 * mm
    footer_line_y = 18 * mm
    footer_text_y = 12 * mm

    label_x = left + 6 * mm
    value_x = left + 62 * mm

    def draw_page_background():
        pdf.setFillColor(white)
        pdf.rect(0, 0, width, height, fill=1, stroke=0)

    def draw_header():
        pdf.setFillColor(charcoal)
        pdf.rect(0, height - header_height, width, header_height, fill=1, stroke=0)

        pdf.setFillColor(accent)
        pdf.setFont("Helvetica-Bold", 20)
        pdf.drawString(left, height - 13 * mm, COMPANY_NAME)

        pdf.setFillColor(white)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(left, height - 21 * mm, "Signed Service Plan Agreement")

        pdf.setFont("Helvetica", 8.5)
        pdf.drawString(left, height - 27 * mm, f"Generated: {datetime.now(UTC).strftime('%d %b %Y %H:%M UTC')}")

        if COMPANY_LOGO_PATH and os.path.exists(COMPANY_LOGO_PATH):
            try:
                logo_reader = ImageReader(COMPANY_LOGO_PATH)
                logo_w = 24 * mm
                logo_h = 14 * mm
                logo_x = right - logo_w
                logo_y = height - 22 * mm
                pdf.drawImage(
                    logo_reader,
                    logo_x,
                    logo_y,
                    width=logo_w,
                    height=logo_h,
                    preserveAspectRatio=True,
                    mask="auto",
                )
            except Exception as e:
                print("Logo draw error:", e)

    def draw_footer():
        pdf.setStrokeColor(line_grey)
        pdf.setLineWidth(0.8)
        pdf.line(left, footer_line_y, right, footer_line_y)

        pdf.setFillColor(mid_grey)
        pdf.setFont("Helvetica", 8)
        footer = f"{COMPANY_NAME} | {COMPANY_PHONE} | {COMPANY_EMAIL} | {COMPANY_REG}"
        pdf.drawString(left, footer_text_y, footer)

    def draw_section_title(y, title):
        pdf.setFillColor(dark)
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(left + 4 * mm, y, title)
        pdf.setStrokeColor(line_grey)
        pdf.setLineWidth(0.6)
        pdf.line(left + 4 * mm, y - 3, right - 4 * mm, y - 3)

    def draw_label_value(y, label, value):
        pdf.setFillColor(dark)
        pdf.setFont("Helvetica-Bold", 9.5)
        pdf.drawString(label_x, y, label)
        pdf.setFont("Helvetica", 9.5)
        pdf.drawString(value_x, y, safe_text(value))

    def draw_card(x, y, w, h, fill=soft_bg):
        pdf.setFillColor(fill)
        pdf.roundRect(x, y, w, h, 4 * mm, fill=1, stroke=0)

    # PAGE 1
    draw_page_background()
    draw_header()

    main_card_x = left
    main_card_y = 30 * mm
    main_card_w = content_width
    main_card_h = height - header_height - 42 * mm
    draw_card(main_card_x, main_card_y, main_card_w, main_card_h)

    y = height - header_height - 12 * mm

    draw_section_title(y, "Customer details")
    y -= 10 * mm
    draw_label_value(y, "Name:", row.get("full_name", "")); y -= 5.8 * mm
    draw_label_value(y, "Email:", row.get("email", "")); y -= 5.8 * mm
    draw_label_value(y, "Phone:", row.get("phone", "")); y -= 5.8 * mm
    draw_label_value(y, "Address:", row.get("address_line1", "")); y -= 5.8 * mm

    address_tail = " ".join(
        part for part in [
            safe_text(row.get("address_line2", "")),
            safe_text(row.get("city", "")),
            safe_text(row.get("postcode", "")),
        ] if part
    )
    draw_label_value(y, "Town/Postcode:", address_tail)
    y -= 9 * mm

    draw_section_title(y, "Plan details")
    y -= 10 * mm
    draw_label_value(y, "Selected plan:", row.get("selected_plan", "")); y -= 5.8 * mm
    draw_label_value(y, "Monthly price:", f"£{safe_text(row.get('monthly_price', ''))}"); y -= 5.8 * mm
    draw_label_value(y, "Boiler broken:", row.get("boiler_broken", "")); y -= 5.8 * mm
    draw_label_value(y, "Fix & Join:", row.get("fix_and_join", "")); y -= 5.8 * mm
    draw_label_value(y, "Priority:", row.get("priority", "")); y -= 5.8 * mm
    draw_label_value(y, "Preferred contact time:", row.get("contact_time", "")); y -= 9 * mm

    draw_section_title(y, "Boiler information")
    y -= 10 * mm
    draw_label_value(y, "Boiler make:", row.get("boiler_make", "")); y -= 5.8 * mm
    draw_label_value(y, "Boiler model:", row.get("boiler_model", "")); y -= 5.8 * mm
    draw_label_value(y, "Boiler age:", row.get("boiler_age", "")); y -= 9 * mm

    draw_section_title(y, "Legal acceptance")
    y -= 10 * mm

    terms_text = "Yes" if row.get("terms_accepted") == 1 else "No"
    privacy_text = "Yes" if row.get("privacy_accepted") == 1 else "No"
    marketing_text = "Yes" if row.get("marketing_opt_in") == 1 else "No"

    signed_at = row.get("signed_at")
    signed_at_text = signed_at.strftime("%d %b %Y %H:%M") if signed_at else ""

    draw_label_value(y, "Terms accepted:", terms_text); y -= 5.4 * mm
    draw_label_value(y, "Terms version:", row.get("terms_version", "")); y -= 5.4 * mm
    draw_label_value(y, "Privacy accepted:", privacy_text); y -= 5.4 * mm
    draw_label_value(y, "Privacy version:", row.get("privacy_version", "")); y -= 5.4 * mm
    draw_label_value(y, "Marketing opt-in:", marketing_text); y -= 5.4 * mm
    draw_label_value(y, "Signed at:", signed_at_text); y -= 5.4 * mm
    draw_label_value(y, "IP address:", row.get("ip_address", "")); y -= 5.4 * mm

    user_agent_short = safe_text(row.get("user_agent", ""))[:42]
    draw_label_value(y, "User agent:", user_agent_short)
    y -= 9 * mm

    summary_h = 22 * mm
    summary_y = 24 * mm
    draw_card(left + 4 * mm, summary_y, content_width - 8 * mm, summary_h, fill=white)

    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 10)
    pdf.drawString(left + 8 * mm, summary_y + 16 * mm, "Agreement summary")

    pdf.setFillColor(mid_grey)
    pdf.setFont("Helvetica", 8.2)
    summary_lines = [
        "The customer confirms the information provided is correct to the best of their knowledge.",
        "By signing, the customer agrees to the Service Plan Terms & Conditions and Privacy Policy.",
        "Fix & Join remains subject to inspection and eligibility where applicable.",
    ]

    text_obj = pdf.beginText(left + 8 * mm, summary_y + 11 * mm)
    text_obj.setLeading(9)
    for line in summary_lines:
        text_obj.textLine(line)
    pdf.drawText(text_obj)

    draw_footer()

    # PAGE 2
    pdf.showPage()
    draw_page_background()
    draw_header()

    sig_card_x = left
    sig_card_y = 30 * mm
    sig_card_w = content_width
    sig_card_h = height - header_height - 42 * mm
    draw_card(sig_card_x, sig_card_y, sig_card_w, sig_card_h)

    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 15)
    pdf.drawString(left + 6 * mm, height - header_height - 18 * mm, "Customer signature")

    pdf.setFillColor(mid_grey)
    pdf.setFont("Helvetica", 9.5)
    pdf.drawString(
        left + 6 * mm,
        height - header_height - 25 * mm,
        "Signed electronically as part of the service plan agreement."
    )

    box_x = left + 6 * mm
    box_y = height - header_height - 58 * mm
    box_w = 105 * mm
    box_h = 30 * mm

    pdf.setStrokeColor(line_grey)
    pdf.setLineWidth(1)
    pdf.roundRect(box_x, box_y, box_w, box_h, 3 * mm, stroke=1, fill=0)

    signature = row.get("signature")
    if signature and signature.startswith("data:image/png;base64,"):
        try:
            sig_bytes = base64.b64decode(signature.split(",", 1)[1])
            sig_reader = ImageReader(io.BytesIO(sig_bytes))
            pdf.drawImage(
                sig_reader,
                box_x + 6 * mm,
                box_y + 5 * mm,
                width=80 * mm,
                height=16 * mm,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            pass

    name_y = box_y - 14 * mm
    date_y = box_y - 22 * mm

    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawString(left + 6 * mm, name_y, "Signed by:")
    pdf.setFont("Helvetica", 9.5)
    pdf.drawString(left + 30 * mm, name_y, safe_text(row.get("full_name", "")))

    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawString(left + 6 * mm, date_y, "Date:")
    pdf.setFont("Helvetica", 9.5)
    pdf.drawString(left + 30 * mm, date_y, signed_at_text)

    draw_footer()

    pdf.save()
    return temp.name


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/signup")
def signup():
    return render_template("signup.html")


@app.route("/submit", methods=["POST"])
def submit():
    validate_csrf()

    if request.form.get("website"):
        return redirect(url_for("signup"))

    full_name = clean_text(request.form.get("full_name"), 120)
    email = clean_text(request.form.get("email"), 254).lower()
    phone = normalise_phone(request.form.get("phone"))
    address_line1 = clean_text(request.form.get("address_line1"), 120)
    address_line2 = clean_text(request.form.get("address_line2"), 120)
    city = clean_text(request.form.get("city"), 80)
    postcode = normalise_postcode(request.form.get("postcode"))

    boiler_make = clean_text(request.form.get("boiler_make"), 80)
    boiler_model = clean_text(request.form.get("boiler_model"), 80)
    boiler_age = clean_text(request.form.get("boiler_age"), 40)
    existing_customer = clean_text(request.form.get("existing_customer"), 10)

    boiler_broken = clean_text(request.form.get("boiler_broken"), 10)
    fix_and_join = clean_text(request.form.get("fix_and_join"), 10)
    contact_time = clean_text(request.form.get("contact_time"), 20)
    marketing_opt_in = 1 if request.form.get("marketing_opt_in") == "on" else 0

    selected_plan = clean_text(request.form.get("selected_plan"), 20)
    monthly_price = PLAN_PRICES.get(selected_plan, "")

    signature = clean_text(request.form.get("signature"))
    signed_at = datetime.now(UTC)

    terms_accepted = 1 if request.form.get("terms_accepted") == "on" else 0
    privacy_accepted = 1 if request.form.get("privacy_accepted") == "on" else 0

    ip_address = get_client_ip()
    user_agent = request.headers.get("User-Agent", "")[:500]

    if not all([full_name, email, phone, address_line1, city, postcode, selected_plan, boiler_broken]):
        flash("Please complete all required fields.", "error")
        return redirect(url_for("signup"))

    if not is_valid_email(email):
        flash("Please enter a valid email address.", "error")
        return redirect(url_for("signup"))

    if not is_valid_phone(phone):
        flash("Please enter a valid phone number.", "error")
        return redirect(url_for("signup"))

    if not is_valid_uk_postcode(postcode):
        flash("Please enter a valid postcode.", "error")
        return redirect(url_for("signup"))

    if selected_plan not in VALID_PLAN_VALUES:
        flash("Invalid plan selected.", "error")
        return redirect(url_for("signup"))

    if boiler_broken not in VALID_BROKEN_VALUES:
        flash("Please confirm whether the boiler is currently broken.", "error")
        return redirect(url_for("signup"))

    if existing_customer not in VALID_EXISTING_CUSTOMER_VALUES:
        flash("Please choose a valid existing customer option.", "error")
        return redirect(url_for("signup"))

    if contact_time not in VALID_CONTACT_TIMES:
        flash("Please choose a valid contact time.", "error")
        return redirect(url_for("signup"))

    if boiler_broken == "Yes" and selected_plan == "Essential":
        flash(
            "Essential is not available if your boiler is currently broken. Please choose Standard or Complete.",
            "error",
        )
        return redirect(url_for("signup"))

    if boiler_broken == "Yes":
        fix_and_join = "Yes"

    if fix_and_join not in {"Yes", "No", ""}:
        flash("Invalid Fix & Join selection.", "error")
        return redirect(url_for("signup"))

    if not parse_signature(signature):
        flash("Please add your signature before submitting.", "error")
        return redirect(url_for("signup"))

    if not terms_accepted or not privacy_accepted:
        flash("You must accept the terms and privacy policy.", "error")
        return redirect(url_for("signup"))

    priority = determine_priority(boiler_broken, fix_and_join)

    signup_id = None
    conn = get_db_connection()
    try:
        if is_duplicate_submission(conn, email, phone, postcode, selected_plan):
            flash(
                "It looks like this enquiry was already submitted recently. Please wait a moment or contact us directly.",
                "error",
            )
            return redirect(url_for("signup"))

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO signups (
                    created_at,
                    updated_at,
                    full_name,
                    email,
                    phone,
                    address_line1,
                    address_line2,
                    city,
                    postcode,
                    boiler_make,
                    boiler_model,
                    boiler_age,
                    existing_customer,
                    boiler_broken,
                    fix_and_join,
                    marketing_opt_in,
                    selected_plan,
                    monthly_price,
                    contact_time,
                    priority,
                    signature,
                    signed_at,
                    terms_accepted,
                    privacy_accepted,
                    terms_version,
                    privacy_version,
                    ip_address,
                    user_agent,
                    status,
                    admin_notes,
                    payment_status
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    datetime.now(UTC),
                    datetime.now(UTC),
                    full_name,
                    email,
                    phone,
                    address_line1,
                    address_line2,
                    city,
                    postcode,
                    boiler_make,
                    boiler_model,
                    boiler_age,
                    existing_customer,
                    boiler_broken,
                    fix_and_join,
                    marketing_opt_in,
                    selected_plan,
                    monthly_price,
                    contact_time,
                    priority,
                    signature,
                    signed_at,
                    terms_accepted,
                    privacy_accepted,
                    TERMS_VERSION,
                    PRIVACY_VERSION,
                    ip_address,
                    user_agent,
                    "New",
                    "",
                    "Not sent",
                ),
            )
            signup_id = cur.fetchone()["id"]
        conn.commit()
    finally:
        conn.close()

    pdf_path = None
    try:
        signup_row = {
            "full_name": full_name,
            "email": email,
            "phone": phone,
            "address_line1": address_line1,
            "address_line2": address_line2,
            "city": city,
            "postcode": postcode,
            "boiler_make": boiler_make,
            "boiler_model": boiler_model,
            "boiler_age": boiler_age,
            "existing_customer": existing_customer,
            "boiler_broken": boiler_broken,
            "fix_and_join": fix_and_join,
            "marketing_opt_in": marketing_opt_in,
            "selected_plan": selected_plan,
            "monthly_price": monthly_price,
            "contact_time": contact_time,
            "priority": priority,
            "signature": signature,
            "signed_at": signed_at,
            "terms_accepted": terms_accepted,
            "privacy_accepted": privacy_accepted,
            "terms_version": TERMS_VERSION,
            "privacy_version": PRIVACY_VERSION,
            "ip_address": ip_address,
            "user_agent": user_agent,
        }

        pdf_path = build_contract_pdf(signup_row)

        safe_name_for_pdf = "".join(c for c in full_name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
        pdf_filename = f"{safe_name_for_pdf or 'signup'}_agreement.pdf"
        pdf_attachment = build_pdf_attachment(pdf_path, pdf_filename)

        send_customer_email(
            full_name,
            email,
            selected_plan,
            monthly_price,
            contact_time,
            priority,
            pdf_attachment=pdf_attachment,
        )

        send_admin_email(
            full_name,
            email,
            phone,
            selected_plan,
            priority,
            boiler_broken,
            fix_and_join,
            contact_time,
            pdf_attachment=pdf_attachment,
        )

    except Exception as e:
        print("Email/PDF error:", e)
    finally:
        if pdf_path and os.path.exists(pdf_path):
            os.remove(pdf_path)

    session["last_signup"] = {
        "full_name": full_name,
        "selected_plan": selected_plan,
        "monthly_price": monthly_price,
        "priority": priority,
        "contact_time": contact_time,
        "signup_id": signup_id,
    }

    if stripe_is_configured() and signup_id:
        try:
            checkout_session = create_stripe_checkout_session(
                signup_id=signup_id,
                full_name=full_name,
                email=email,
                selected_plan=selected_plan,
            )

            conn = get_db_connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE signups
                        SET stripe_checkout_session_id = %s,
                            stripe_checkout_url = %s,
                            payment_status = %s,
                            updated_at = %s,
                            stripe_payment_link_sent_at = %s
                        WHERE id = %s
                        """,
                        (
                            checkout_session.id,
                            checkout_session.url,
                            "Link sent",
                            datetime.now(UTC),
                            datetime.now(UTC),
                            signup_id,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()

            return redirect(checkout_session.url)

        except Exception as e:
            print("Stripe checkout error:", e)
            flash("Your enquiry was saved, but we couldn't open the payment page just now.", "error")

    return redirect(url_for("success"))


@app.route("/success")
def success():
    signup_summary = session.get("last_signup")
    return render_template("success.html", signup_summary=signup_summary)


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/stripe/success")
def stripe_success():
    session_id = request.args.get("session_id", "").strip()
    return render_template("stripe_success.html", session_id=session_id)


@app.route("/stripe/cancel")
def stripe_cancel():
    flash("Payment was cancelled. You can try again when you're ready.", "error")
    return redirect(url_for("success"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data(as_text=True)
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload,
            sig_header,
            STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    event_type = event["type"]
    data_object = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            metadata = data_object.get("metadata", {}) or {}
            signup_id = metadata.get("signup_id")

            if signup_id:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE signups
                            SET payment_status = %s,
                                stripe_customer_id = %s,
                                stripe_subscription_id = %s,
                                stripe_checkout_session_id = %s,
                                updated_at = %s
                            WHERE id = %s
                            """,
                            (
                                "Paid",
                                data_object.get("customer"),
                                data_object.get("subscription"),
                                data_object.get("id"),
                                datetime.now(UTC),
                                int(signup_id),
                            ),
                        )
                    conn.commit()
                finally:
                    conn.close()

        elif event_type == "invoice.payment_failed":
            subscription_id = data_object.get("subscription")
            if subscription_id:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE signups
                            SET payment_status = %s,
                                updated_at = %s
                            WHERE stripe_subscription_id = %s
                            """,
                            (
                                "Failed",
                                datetime.now(UTC),
                                subscription_id,
                            ),
                        )
                    conn.commit()
                finally:
                    conn.close()

        elif event_type == "customer.subscription.deleted":
            subscription_id = data_object.get("id")
            if subscription_id:
                conn = get_db_connection()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE signups
                            SET status = %s,
                                updated_at = %s
                            WHERE stripe_subscription_id = %s
                            """,
                            (
                                "Lost",
                                datetime.now(UTC),
                                subscription_id,
                            ),
                        )
                    conn.commit()
                finally:
                    conn.close()

    except Exception as e:
        print("Stripe webhook handling error:", e)
        return "Webhook handler error", 500

    return "OK", 200


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        validate_csrf()
        submitted = request.form.get("password", "")
        if submitted and secrets.compare_digest(submitted, ADMIN_PASSWORD):
            session["admin_logged_in"] = True
            session.permanent = True
            return redirect(url_for("admin"))
        flash("Incorrect password.", "login_error")
        return redirect(url_for("admin_login"))

    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
@login_required
def admin_logout():
    validate_csrf()
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin/export")
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
        "Name",
        "Email",
        "Phone",
        "Address Line 1",
        "Address Line 2",
        "City",
        "Postcode",
        "Boiler Make",
        "Boiler Model",
        "Boiler Age",
        "Existing Customer",
        "Boiler Broken",
        "Fix & Join",
        "Marketing Opt-in",
        "Plan",
        "Monthly Price",
        "Preferred Contact Time",
        "Priority",
        "Status",
        "Admin Notes",
        "Payment Status",
        "Stripe Checkout URL",
        "Stripe Checkout Session ID",
        "Stripe Customer ID",
        "Stripe Subscription ID",
        "Signed At",
        "Terms Accepted",
        "Privacy Accepted",
        "Terms Version",
        "Privacy Version",
        "IP Address",
        "User Agent",
    ])

    for row in rows:
        writer.writerow([
            row["id"],
            row["created_at"],
            row.get("updated_at"),
            row["full_name"],
            row["email"],
            row["phone"],
            row["address_line1"],
            row["address_line2"],
            row["city"],
            row["postcode"],
            row["boiler_make"],
            row["boiler_model"],
            row["boiler_age"],
            row["existing_customer"],
            row["boiler_broken"],
            row["fix_and_join"],
            row["marketing_opt_in"],
            row["selected_plan"],
            row["monthly_price"],
            row["contact_time"],
            row["priority"],
            row.get("status"),
            row.get("admin_notes"),
            row.get("payment_status"),
            row.get("stripe_checkout_url"),
            row.get("stripe_checkout_session_id"),
            row.get("stripe_customer_id"),
            row.get("stripe_subscription_id"),
            row["signed_at"],
            row["terms_accepted"],
            row["privacy_accepted"],
            row.get("terms_version"),
            row.get("privacy_version"),
            row.get("ip_address"),
            row.get("user_agent"),
        ])

    csv_data = output.getvalue()
    output.close()

    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sjm_signups.csv"},
    )


@app.route("/admin/contract/<int:signup_id>")
@login_required
def download_contract(signup_id):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM signups WHERE id = %s", (signup_id,))
            row = cur.fetchone()
    finally:
        conn.close()

    if not row:
        return "Signup not found", 404

    pdf_path = build_contract_pdf(row)
    safe_name_for_pdf = "".join(c for c in row["full_name"] if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    filename = f"{safe_name_for_pdf or 'signup'}_agreement_{signup_id}.pdf"

    @after_this_request
    def cleanup(response):
        try:
            if os.path.exists(pdf_path):
                os.remove(pdf_path)
        except Exception as e:
            print("Temp PDF cleanup error:", e)
        return response

    return send_file(pdf_path, as_attachment=True, download_name=filename, mimetype="application/pdf")


@app.route("/admin/update/<int:signup_id>", methods=["POST"])
@login_required
def admin_update_signup(signup_id):
    validate_csrf()

    status = clean_text(request.form.get("status"), 20)
    admin_notes = clean_text(request.form.get("admin_notes"), 5000)
    payment_status = clean_text(request.form.get("payment_status"), 20)

    if status not in VALID_STATUSES:
        flash("Invalid status.", "error")
        return redirect(url_for("admin"))

    if payment_status not in VALID_PAYMENT_STATUSES:
        flash("Invalid payment status.", "error")
        return redirect(url_for("admin"))

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE signups
                SET status = %s,
                    admin_notes = %s,
                    payment_status = %s,
                    updated_at = %s
                WHERE id = %s
                """,
                (status, admin_notes, payment_status, datetime.now(UTC), signup_id),
            )
        conn.commit()
    finally:
        conn.close()

    flash("Signup updated.", "success")
    return redirect(url_for("admin"))


@app.route("/admin")
@login_required
def admin():
    search = clean_text(request.args.get("search"), 100)
    status = clean_text(request.args.get("status"), 20)
    priority = clean_text(request.args.get("priority"), 20)
    broken_only = request.args.get("broken_only", "").strip()
    fix_join_only = request.args.get("fix_join_only", "").strip()
    marketing_only = request.args.get("marketing_only", "").strip()
    payment_status = clean_text(request.args.get("payment_status"), 20)

    query = "SELECT * FROM signups WHERE 1=1"
    params = []

    if search:
        query += """
            AND (
                full_name ILIKE %s OR
                email ILIKE %s OR
                phone ILIKE %s OR
                postcode ILIKE %s
            )
        """
        search_term = f"%{search}%"
        params.extend([search_term, search_term, search_term, search_term])

    if status in VALID_STATUSES:
        query += " AND status = %s"
        params.append(status)

    if priority in {"HIGH", "NORMAL"}:
        query += " AND priority = %s"
        params.append(priority)

    if payment_status in VALID_PAYMENT_STATUSES:
        query += " AND payment_status = %s"
        params.append(payment_status)

    if broken_only == "1":
        query += " AND boiler_broken = 'Yes'"

    if fix_join_only == "1":
        query += " AND fix_and_join = 'Yes'"

    if marketing_only == "1":
        query += " AND marketing_opt_in = 1"

    query += " ORDER BY CASE WHEN priority = 'HIGH' THEN 0 ELSE 1 END, id DESC"

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            signups = cur.fetchall()
    finally:
        conn.close()

    return render_template(
        "admin.html",
        signups=signups,
        filters={
            "search": search,
            "status": status,
            "priority": priority,
            "payment_status": payment_status,
            "broken_only": broken_only,
            "fix_join_only": fix_join_only,
            "marketing_only": marketing_only,
        },
        valid_statuses=sorted(VALID_STATUSES),
        valid_payment_statuses=sorted(VALID_PAYMENT_STATUSES),
    )


# -----------------------------------------------------------------------------
# Error handlers
# -----------------------------------------------------------------------------

@app.errorhandler(400)
def bad_request(error):
    return render_template("error.html", message=getattr(error, "description", "Bad request.")), 400


@app.errorhandler(413)
def request_entity_too_large(error):
    return render_template("error.html", message="The uploaded request was too large."), 413


@app.errorhandler(500)
def internal_error(error):
    return render_template("error.html", message="Something went wrong. Please try again or contact us directly."), 500


# -----------------------------------------------------------------------------
# Startup
# -----------------------------------------------------------------------------

try:
    init_db()
except Exception as e:
    print("Database init error:", e)

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5001")),
        debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true",
    )