ALTER TABLE signups
ADD COLUMN IF NOT EXISTS admin_notes TEXT,
ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP,
ADD COLUMN IF NOT EXISTS archived_by TEXT,
ADD COLUMN IF NOT EXISTS archive_reason TEXT,
ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP,
ADD COLUMN IF NOT EXISTS deleted_by TEXT,
ADD COLUMN IF NOT EXISTS delete_reason TEXT;

UPDATE signups
SET is_archived = COALESCE(is_archived, FALSE),
    is_deleted = COALESCE(is_deleted, FALSE),
    updated_at = COALESCE(updated_at, created_at, NOW());

ALTER TABLE one_off_service_bookings
ADD COLUMN IF NOT EXISTS is_archived BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP,
ADD COLUMN IF NOT EXISTS archived_by TEXT,
ADD COLUMN IF NOT EXISTS archive_reason TEXT,
ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE,
ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP,
ADD COLUMN IF NOT EXISTS deleted_by TEXT,
ADD COLUMN IF NOT EXISTS delete_reason TEXT,
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;

UPDATE one_off_service_bookings
SET is_archived = COALESCE(is_archived, FALSE),
    is_deleted = COALESCE(is_deleted, FALSE),
    updated_at = COALESCE(updated_at, created_at, NOW());

CREATE INDEX IF NOT EXISTS idx_signups_admin_state
ON signups (is_deleted, is_archived, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_one_off_bookings_admin_state
ON one_off_service_bookings (is_deleted, is_archived, created_at DESC);

CREATE TABLE IF NOT EXISTS admin_action_log (
    id SERIAL PRIMARY KEY,
    customer_id INTEGER NOT NULL,
    customer_table TEXT NOT NULL,
    action TEXT NOT NULL,
    action_by TEXT,
    action_timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW(),
    details TEXT
);
