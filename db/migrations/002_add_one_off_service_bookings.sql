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
ADD COLUMN IF NOT EXISTS user_agent TEXT;

UPDATE signups
SET address_line_1 = COALESCE(address_line_1, address_line1),
    address_line_2 = COALESCE(address_line_2, address_line2),
    fix_and_join_fee = COALESCE(
        fix_and_join_fee,
        CASE WHEN fix_and_join = 'Yes' THEN '240.99' ELSE NULL END
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
    updated_at = COALESCE(updated_at, created_at, NOW());

CREATE TABLE IF NOT EXISTS one_off_service_bookings (
    id SERIAL PRIMARY KEY,
    booking_type TEXT NOT NULL DEFAULT 'one_off_annual_service',
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
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_off_bookings_stripe_session_id
ON one_off_service_bookings (stripe_session_id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_one_off_bookings_stripe_payment_intent_id
ON one_off_service_bookings (stripe_payment_intent_id);
