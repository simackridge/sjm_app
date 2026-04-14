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
TERMS_VERSION = os.environ.get("TERMS_VERSION", "v1.0")
PRIVACY_VERSION = os.environ.get("PRIVACY_VERSION", "v1.0")

TERMS_PDF_FILENAME = "sjm_service_plan_terms_v1.pdf"
PRIVACY_PDF_FILENAME = "sjm_privacy_policy_v1.pdf"
FAIR_USAGE_PDF_FILENAME = "sjm_fair_usage_policy_v1.pdf"

ACCENT = colors.HexColor("#ff6a00")
DARK = colors.HexColor("#111111")
MUTED = colors.HexColor("#5f5f5f")
BORDER = colors.HexColor("#d8d8d8")
SOFT_BG = colors.HexColor("#fafafa")
PALE_ORANGE = colors.HexColor("#fff3eb")

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
    return bool(filename) and os.path.exists(os.path.join(DOCS_DIR, filename))


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


def money_to_pence(value):
    value = clean(value)
    if not value:
        return 0
    parts = value.split(".")
    pounds = int(parts[0])
    pence = int((parts[1] if len(parts) > 1 else "0").ljust(2, "0")[:2])
    return pounds * 100 + pence


def fetch_signup(signup_id):
    return db_execute(
        "SELECT * FROM signups WHERE id=%s",
        (signup_id,),
        fetchone=True,
    )


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
            updated_at=%s
        WHERE id=%s
        """,
        (checkout_url, checkout_session_id, now, now, signup_id),
        commit=True,
    )


def mark_signup_paid(signup_id, checkout=None):
    now = datetime.now(UTC)

    stripe_customer_id = None
    stripe_subscription_id = None

    if checkout:
        stripe_customer_id = getattr(checkout, "customer", None)
        stripe_subscription_id = getattr(checkout, "subscription", None)

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
                ADD COLUMN IF NOT EXISTS customer_email_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS admin_email_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS reminder_due_date TIMESTAMP,
                ADD COLUMN IF NOT EXISTS reminder_sent BOOLEAN DEFAULT FALSE,
                ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS stripe_checkout_url TEXT,
                ADD COLUMN IF NOT EXISTS stripe_checkout_session_id TEXT,
                ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT,
                ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT,
                ADD COLUMN IF NOT EXISTS payment_completed_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS last_payment_link_sent_at TIMESTAMP,
                ADD COLUMN IF NOT EXISTS contract_pdf BYTEA,
                ADD COLUMN IF NOT EXISTS contract_pdf_filename TEXT,
                ADD COLUMN IF NOT EXISTS contract_pdf_generated_at TIMESTAMP
                """
            )
        conn.commit()
    finally:
        conn.close()


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

# -----------------------------------------------------------------------------
# PDF GENERATION
# -----------------------------------------------------------------------------

def draw_wrapped_text(pdf, text, x, y, max_width, line_height=14, font_name="Helvetica", font_size=10):
    pdf.setFont(font_name, font_size)
    words = (text or "").split()
    if not words:
        return y

    line = ""
    for word in words:
        test_line = f"{line} {word}".strip()
        if pdf.stringWidth(test_line, font_name, font_size) <= max_width:
            line = test_line
        else:
            if line:
                pdf.drawString(x, y, line)
                y -= line_height
            line = word

    if line:
        pdf.drawString(x, y, line)
        y -= line_height

    return y


def draw_bullets(pdf, items, x, y, max_width, line_height=14, bullet_indent=12):
    for item in items:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(x, y, u"\u2022")
        y = draw_wrapped_text(
            pdf,
            item,
            x + bullet_indent,
            y,
            max_width - bullet_indent,
            line_height=line_height,
            font_name="Helvetica",
            font_size=10,
        )
        y -= 2
    return y


def draw_section_box(pdf, x, y_top, width, title, body_lines=None, bullet_lines=None):
    body_lines = body_lines or []
    bullet_lines = bullet_lines or []

    cursor_y = y_top - 14
    content_height = 30 + (len(body_lines) * 16) + (len(bullet_lines) * 18)
    if content_height < 58:
        content_height = 58

    box_bottom = y_top - content_height

    pdf.setFillColor(SOFT_BG)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(x, box_bottom, width, content_height, 8, stroke=1, fill=1)

    pdf.setFillColor(PALE_ORANGE)
    pdf.roundRect(x, y_top - 28, width, 28, 8, stroke=0, fill=1)

    pdf.setFillColor(DARK)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(x + 12, y_top - 18, title)

    cursor_y = y_top - 42
    for line in body_lines:
        cursor_y = draw_wrapped_text(pdf, line, x + 12, cursor_y, width - 24)
        cursor_y -= 2

    if bullet_lines:
        cursor_y = draw_bullets(pdf, bullet_lines, x + 12, cursor_y, width - 24)

    return box_bottom - 14


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
    buffer = io.BytesIO()
    pdf = pdf_canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    margin = 42
    content_width = width - (margin * 2)
    y = height - 42

    # Header bar
    pdf.setFillColor(ACCENT)
    pdf.rect(margin, y - 24, content_width, 24, fill=1, stroke=0)

    if os.path.exists(LOGO_PATH):
        try:
            pdf.drawImage(
                LOGO_PATH,
                margin,
                y - 72,
                width=92,
                height=46,
                preserveAspectRatio=True,
                mask="auto",
            )
        except Exception:
            logger.exception("Could not draw logo in PDF.")

    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin + 10, y - 16, "SJM HEATING SERVICE PLAN")

    pdf.setFillColor(DARK)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawRightString(width - margin, y - 42, "Service Plan Agreement")
    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(MUTED)
    pdf.drawRightString(width - margin, y - 58, f"Issued: {datetime.now(UTC).strftime('%d/%m/%Y')}")
    y -= 92

    # Company / customer summary
    pdf.setStrokeColor(BORDER)
    pdf.setFillColor(SOFT_BG)
    pdf.roundRect(margin, y - 88, content_width, 88, 10, stroke=1, fill=1)

    pdf.setFillColor(DARK)
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

    # Address
    address = build_full_address(signup) or "-"
    y = draw_section_box(
        pdf,
        margin,
        y,
        content_width,
        "Property Address",
        body_lines=[address],
    )

    # Plan details
    plan_body = [
        f"Selected Plan: {signup.get('selected_plan') or '-'}",
        f"Monthly Price: £{signup.get('monthly_price') or '-'}",
        f"Boiler Broken: {signup.get('boiler_broken') or '-'}",
        f"Boiler Under 3 Years: {signup.get('boiler_under_3_years') or '-'}",
        f"Warranty Valid: {signup.get('boiler_warranty_valid') or '-'}",
        f"Fix & Join: {signup.get('fix_and_join') or 'No'}",
    ]

    if signup.get("fix_and_join") == "Yes":
        plan_body.append(f"Fix & Join Fee: £{signup.get('fix_and_join_fee') or FIX_AND_JOIN_FEE}")

    y = draw_section_box(
        pdf,
        margin,
        y,
        content_width,
        "Plan Summary",
        body_lines=plan_body,
    )

    # Terms summary
    y = draw_section_box(
        pdf,
        margin,
        y,
        content_width,
        "Key Cover Summary",
        bullet_lines=[
            "This agreement confirms the customer's application for the selected SJM Heating service plan.",
            "Cover is subject to the plan terms, privacy policy and fair usage policy in force at the time of sign-up.",
            "All work remains subject to safe access, inspection, diagnosis, parts availability and system suitability.",
            "Fix & Join work does not guarantee full repair within the initial charge.",
        ],
    )

    # Legal acceptance
    y = draw_section_box(
        pdf,
        margin,
        y,
        content_width,
        "Legal Acceptance",
        body_lines=[
            f"Terms Accepted: {'Yes' if signup.get('accepted_terms') else 'No'} ({TERMS_VERSION})",
            f"Privacy Accepted: {'Yes' if signup.get('accepted_privacy') else 'No'} ({PRIVACY_VERSION})",
            f"Fair Usage Accepted: {'Yes' if signup.get('accepted_fair_usage') else 'No'}",
        ],
    )

    # Signature block
    sig_height = 108
    pdf.setFillColor(colors.white)
    pdf.setStrokeColor(BORDER)
    pdf.roundRect(margin, y - sig_height, content_width, sig_height, 10, stroke=1, fill=1)

    pdf.setFillColor(PALE_ORANGE)
    pdf.roundRect(margin, y - 28, content_width, 28, 10, stroke=0, fill=1)

    pdf.setFillColor(DARK)
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
                y - 86,
                width=160,
                height=42,
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

    y -= (sig_height + 22)

    # Footer
    pdf.setStrokeColor(BORDER)
    pdf.line(margin, 26, width - margin, 26)
    pdf.setFont("Helvetica", 8.5)
    pdf.setFillColor(MUTED)
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
    if not resend.api_key or not RESEND_FROM_EMAIL or not signup.get("email"):
        logger.warning("Customer confirmation email skipped for signup %s", signup.get("id"))
        return False

    fix_join_html = ""
    if signup.get("fix_and_join") == "Yes":
        fix_join_html = f"""
        <p><strong>Fix &amp; Join applies:</strong> A one-off fee of £{signup.get('fix_and_join_fee') or FIX_AND_JOIN_FEE}
        applies and work remains subject to inspection and suitability.</p>
        """

    html = f"""
    <div style="font-family:Arial,sans-serif;line-height:1.6;color:#222;">
      <h2>Thank you for choosing {COMPANY_NAME}</h2>
      <p>Your service plan signup and payment have been received.</p>

      <p><strong>Customer:</strong> {signup.get('full_name') or '-'}</p>
      <p><strong>Plan:</strong> {signup.get('selected_plan') or '-'} - £{signup.get('monthly_price') or '-'} / month</p>
      {fix_join_html}

      <p>Attached to this email you will find:</p>
      <ul>
        <li>Your signed service plan agreement</li>
        <li>Our Terms and Conditions PDF</li>
        <li>Our Privacy Policy PDF</li>
        <li>Our Fair Usage Policy PDF</li>
      </ul>

      <p><strong>What happens next:</strong></p>
      <ul>
        <li>We review your application</li>
        <li>We contact you if anything else is needed</li>
        <li>Your cover proceeds in line with the agreed terms</li>
      </ul>

      <p>If you need anything, reply to this email or call {COMPANY_PHONE}.</p>
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

# -----------------------------------------------------------------------------
# STRIPE EVENT HANDLING
# -----------------------------------------------------------------------------

def handle_completed_checkout_session(checkout):
    signup_id = None
    try:
        signup_id = checkout.metadata.get("signup_id")
    except Exception:
        pass

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
            signup_row = fetch_signup(signup_id)
        except Exception:
            logger.exception("Could not fetch signup %s for success page.", signup_id)
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

    now = datetime.now(UTC)
    reminder_due_date = now + timedelta(days=335)

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
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
                    updated_at,
                    reminder_due_date,
                    reminder_sent
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
                    reminder_due_date,
                    False,
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
        return redirect(url_for("signup"))

    mark_payment_link_generated(signup_id, checkout_session.url, checkout_session.id)
    return redirect(checkout_session.url)


@app.route("/stripe/success")
def stripe_success():
    ensure_extra_columns()
    session_id = request.args.get("session_id")

    if not session_id:
        return render_template("stripe_success.html")

    try:
        checkout = stripe.checkout.Session.retrieve(session_id)
        signup_id = checkout.metadata.get("signup_id")
    except Exception as e:
        logger.exception("Could not verify Stripe success session.")
        flash(f"Could not verify payment session: {e}", "error")
        return render_template("stripe_success.html")

    if not signup_id:
        flash("Could not match this payment to a signup.", "error")
        return render_template("stripe_success.html")

    paid_statuses = {"paid", "no_payment_required"}
    checkout_status = getattr(checkout, "payment_status", None)

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


@app.route("/stripe/cancel")
def stripe_cancel():
    return render_template("stripe_cancel.html")


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