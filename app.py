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

RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "onboarding@resend.dev")
resend.api_key = os.environ.get("RESEND_API_KEY")

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
                    stripe_subscription_id TEXT
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

        with conn.cursor() as cur:
            cur.execute("UPDATE signups SET updated_at = created_at WHERE updated_at IS NULL")
            cur.execute("UPDATE signups SET status = 'New' WHERE status IS NULL OR status = ''")
            cur.execute("UPDATE signups SET admin_notes = '' WHERE admin_notes IS NULL")
            cur.execute("UPDATE signups SET payment_status = 'Not sent' WHERE payment_status IS NULL OR payment_status = ''")
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
    if not resend.api_key:
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
        "from": f"{COMPANY_NAME} <{RESEND_FROM_EMAIL}>",
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
            <p style="margin:0 0 16px 0;">Our team will review your enquiry and contact you to confirm the next steps. No payment is taken at this stage.</p>
            <p style="margin:0 0 16px 0;">A copy of your signed agreement is attached to this email.</p>
          </div>
        </div>
        """,
    }

    if pdf_attachment:
        payload["attachments"] = [pdf_attachment]

    resend.Emails.send(payload)


def send_admin_email(full_name, email, phone, selected_plan, priority, boiler_broken, fix_and_join, contact_time, pdf_attachment=None):
    if not resend.api_key:
        return

    subject_prefix = "HIGH priority signup" if priority == "HIGH" else "New signup"

    payload = {
        "from": f"{COMPANY_NAME} <{RESEND_FROM_EMAIL}>",
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

    resend.Emails.send(payload)


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
    panel = HexColor("#171717")
    light_text = HexColor("#666666")
    light_border = HexColor("#d9d9d9")
    soft_bg = HexColor("#f7f7f7")

    left = 20 * mm
    right = width - 20 * mm
    value_x = 72 * mm

    # Page background
    pdf.setFillColor(HexColor("#ffffff"))
    pdf.rect(0, 0, width, height, fill=1, stroke=0)

    # Header band
    header_height = 42 * mm
    pdf.setFillColor(panel)
    pdf.rect(0, height - header_height, width, header_height, fill=1, stroke=0)

    # Logo
    logo_drawn = False
    if COMPANY_LOGO_PATH and os.path.exists(COMPANY_LOGO_PATH):
        try:
            logo_reader = ImageReader(COMPANY_LOGO_PATH)
            pdf.drawImage(
                logo_reader,
                left,
                height - 23 * mm,
                width=20 * mm,
                height=14 * mm,
                preserveAspectRatio=True,
                mask='auto'
            )
            logo_drawn = True
        except Exception:
            logo_drawn = False

    header_x = left + (24 * mm if logo_drawn else 0)

    # Header text
    pdf.setFillColor(accent)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(header_x, height - 14 * mm, COMPANY_NAME)

    pdf.setFillColor(HexColor("#ffffff"))
    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawString(left, height - 24 * mm, "Signed Service Plan Agreement")

    pdf.setFont("Helvetica", 9)
    pdf.drawString(left, height - 31 * mm, f"Generated: {datetime.now(UTC).strftime('%d %b %Y %H:%M UTC')}")

    # Main content panel
    content_top = height - 50 * mm
    content_bottom = 62 * mm
    pdf.setFillColor(soft_bg)
    pdf.roundRect(
        left - 4 * mm,
        content_bottom,
        width - 32 * mm,
        content_top - content_bottom,
        4 * mm,
        fill=1,
        stroke=0
    )

    y = height - 60 * mm

    def draw_label_value(pdf_obj, x_label, x_val, y_val, label, value):
        pdf_obj.setFillColor(HexColor("#111111"))
        pdf_obj.setFont("Helvetica-Bold", 10)
        pdf_obj.drawString(x_label, y_val, label)
        pdf_obj.setFont("Helvetica", 10)
        pdf_obj.drawString(x_val, y_val, value if value else "")

    def draw_section_heading(pdf_obj, x_left, y_val, heading):
        pdf_obj.setFillColor(HexColor("#111111"))
        pdf_obj.setFont("Helvetica-Bold", 14)
        pdf_obj.drawString(x_left, y_val, heading)
        pdf_obj.setStrokeColor(HexColor("#e0e0e0"))
        pdf_obj.setLineWidth(0.8)
        pdf_obj.line(x_left, y_val - 4, 185 * mm, y_val - 4)

    # Customer details
    draw_section_heading(pdf, left, y, "Customer details")
    y -= 12 * mm

    draw_label_value(pdf, left, value_x, y, "Name:", row["full_name"]); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Email:", row["email"]); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Phone:", row["phone"]); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Address:", row["address_line1"]); y -= 7 * mm
    address_2 = " ".join(filter(None, [row.get("address_line2", ""), row.get("city", ""), row.get("postcode", "")]))
    draw_label_value(pdf, left, value_x, y, "Town/Postcode:", address_2); y -= 11 * mm

    # Plan details
    draw_section_heading(pdf, left, y, "Plan details")
    y -= 12 * mm

    draw_label_value(pdf, left, value_x, y, "Selected plan:", row["selected_plan"]); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Monthly price:", f"£{row['monthly_price']}"); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Boiler broken:", row.get("boiler_broken", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Fix & Join:", row.get("fix_and_join", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Priority:", row.get("priority", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Preferred contact time:", row.get("contact_time", "")); y -= 11 * mm

    # Boiler information
    draw_section_heading(pdf, left, y, "Boiler information")
    y -= 12 * mm

    draw_label_value(pdf, left, value_x, y, "Boiler make:", row.get("boiler_make", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Boiler model:", row.get("boiler_model", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Boiler age:", row.get("boiler_age", "")); y -= 11 * mm

    # Legal acceptance
    draw_section_heading(pdf, left, y, "Legal acceptance")
    y -= 12 * mm

    terms_text = "Yes" if row.get("terms_accepted") == 1 else "No"
    privacy_text = "Yes" if row.get("privacy_accepted") == 1 else "No"
    marketing_text = "Yes" if row.get("marketing_opt_in") == 1 else "No"

    draw_label_value(pdf, left, value_x, y, "Terms accepted:", terms_text); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Terms version:", row.get("terms_version", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Privacy accepted:", privacy_text); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Privacy version:", row.get("privacy_version", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "Marketing opt-in:", marketing_text); y -= 7 * mm
    draw_label_value(
        pdf,
        left,
        value_x,
        y,
        "Signed at:",
        row["signed_at"].strftime("%d %b %Y %H:%M") if row.get("signed_at") else ""
    ); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "IP address:", row.get("ip_address", "")); y -= 7 * mm
    draw_label_value(pdf, left, value_x, y, "User agent:", (row.get("user_agent", "") or "")[:62]); y -= 10 * mm

    # Legal paragraph
    pdf.setFillColor(light_text)
    pdf.setFont("Helvetica", 9)
    legal_lines = [
        "By signing this document, the customer confirms that the information provided is correct to the best of their",
        "knowledge and that they have read and agreed to the linked Service Plan Terms & Conditions and Privacy Policy.",
        "Fix & Join is subject to inspection and eligibility."
    ]

    text_obj = pdf.beginText(left, y)
    text_obj.setLeading(11)
    for line in legal_lines:
        text_obj.textLine(line)
    pdf.drawText(text_obj)

    # Signature area - fully below main content panel
    box_x = left
    box_y = 28 * mm
    box_w = 82 * mm
    box_h = 22 * mm

    pdf.setFillColor(dark)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(left, box_y + box_h + 7 * mm, "Customer signature")

    pdf.setFillColor(light_text)
    pdf.setFont("Helvetica", 8)
    pdf.drawString(left, box_y - 4 * mm, "Signed electronically")

    pdf.setStrokeColor(light_border)
    pdf.setLineWidth(1)
    pdf.roundRect(box_x, box_y, box_w, box_h, 4 * mm, stroke=1, fill=0)

    pdf.setStrokeColor(HexColor("#e8e8e8"))
    pdf.setLineWidth(0.8)
    pdf.line(box_x + 5 * mm, box_y + 6 * mm, box_x + box_w - 5 * mm, box_y + 6 * mm)

    signature = row.get("signature")
    if signature and signature.startswith("data:image/png;base64,"):
        try:
            sig_bytes = base64.b64decode(signature.split(",", 1)[1])
            sig_reader = ImageReader(io.BytesIO(sig_bytes))
            pdf.drawImage(
                sig_reader,
                box_x + 6 * mm,
                box_y + 4 * mm,
                width=68 * mm,
                height=13 * mm,
                preserveAspectRatio=True,
                mask='auto'
            )
        except Exception:
            pass

    # Footer
    pdf.setStrokeColor(HexColor("#dddddd"))
    pdf.setLineWidth(0.8)
    pdf.line(left, 20 * mm, right, 20 * mm)

    pdf.setFillColor(light_text)
    pdf.setFont("Helvetica", 9)
    footer = f"{COMPANY_NAME} | {COMPANY_PHONE} | {COMPANY_EMAIL} | {COMPANY_REG}"
    pdf.drawString(left, 14 * mm, footer)

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

    conn = get_db_connection()
    try:
        if is_duplicate_submission(conn, email, phone, postcode, selected_plan):
            flash("It looks like this enquiry was already submitted recently. Please wait a moment or contact us directly.", "error")
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

        safe_name = "".join(c for c in full_name if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
        pdf_filename = f"{safe_name or 'signup'}_agreement.pdf"
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
    }

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
    safe_name = "".join(c for c in row["full_name"] if c.isalnum() or c in (" ", "-", "_")).strip().replace(" ", "_")
    filename = f"{safe_name or 'signup'}_agreement_{signup_id}.pdf"

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