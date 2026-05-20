-- ═══════════════════════════════════════════════════════════════════════════════
-- Agentic Email Response System — Complete Supabase Setup
--
-- This is the single source of truth for the database schema.
-- Run this entire script in: Supabase Dashboard → SQL Editor → New Query
--
-- Covers (in order):
--   1. Core tables
--   2. Indexes
--   3. Seed data (default templates)
--   4. Views
--   5. Row Level Security (admin-only policies)
--
-- Prerequisites before running:
--   Set app_metadata.role = 'admin' on your admin user(s) first, otherwise
--   the RLS policies will lock you out of direct Supabase API access.
--
--   Option A — Dashboard:
--     Authentication → Users → click user → Edit → App Metadata
--     → enter: {"role": "admin"} → Save
--
--   Option B — SQL (run this line separately, before the rest of the script):
--     UPDATE auth.users
--     SET raw_app_meta_data = raw_app_meta_data || '{"role":"admin"}'::jsonb
--     WHERE email = 'your-admin@example.com';
--
-- Note: the FastAPI backend always uses the service-role key which bypasses
-- RLS entirely, so none of these policies affect the application pipeline.
-- They only restrict direct Supabase REST API access to admin users.
-- ═══════════════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 1 — TABLES
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 1a. emails ────────────────────────────────────────────────────────────────
-- Stores every incoming client email.
-- uid         = IMAP message UID used for deduplication
-- sender      = client email address
-- body        = full client message
-- query_type  = AI-classified category (billing, technical, general, etc.)
-- status      = pipeline state for this email

CREATE TABLE IF NOT EXISTS emails (
    id           SERIAL PRIMARY KEY,
    uid          VARCHAR(128) UNIQUE NOT NULL,
    sender       VARCHAR(256) NOT NULL,
    subject      VARCHAR(512),
    body         TEXT,
    received_at  TIMESTAMPTZ DEFAULT NOW(),
    is_repeat    BOOLEAN DEFAULT FALSE,
    sender_count INTEGER DEFAULT 1,
    status       VARCHAR(32) DEFAULT 'pending'
                 CHECK (status IN ('pending','manual','approved','rejected','sent')),
    query_type   VARCHAR(128)
);


-- ── 1b. draft_responses ───────────────────────────────────────────────────────
-- Stores AI-generated (and optionally admin-edited) draft replies.
-- draft_body        = original AI output
-- edited_body       = admin-edited version; used in place of draft_body if set
-- confidence        = AI classification confidence (0.0–1.0)
-- scheduled_send_at = populated when a template send_delay_seconds is set;
--                     NULL means send immediately on approval
-- sending_started_at = stamped by both claim paths (approve_and_send and
--                     get_and_claim_scheduled_drafts) at the moment status
--                     transitions to 'sending'.  Used by
--                     recover_stale_sending_drafts() to measure how long a
--                     draft has been in-flight, so a draft created long before
--                     it was claimed is never falsely flagged as stale while
--                     SMTP is still running.
-- status            = pipeline state for this draft

CREATE TABLE IF NOT EXISTS draft_responses (
    id                  SERIAL PRIMARY KEY,
    email_id            INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    draft_body          TEXT,
    edited_body         TEXT,
    confidence          FLOAT DEFAULT 0.0,
    generated_at        TIMESTAMPTZ DEFAULT NOW(),
    approved_at         TIMESTAMPTZ,
    rejected_at         TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,
    scheduled_send_at   TIMESTAMPTZ DEFAULT NULL,
    sending_started_at  TIMESTAMPTZ DEFAULT NULL,
    status              VARCHAR(32) DEFAULT 'pending'
                        CHECK (status IN (
                            'pending',
                            'approved',
                            'rejected',
                            'sent',
                            'send_failed',
                            'sending'
                        )),
    admin_note          TEXT
);

COMMENT ON COLUMN draft_responses.status IS
    'pending      = awaiting admin review
     approved     = admin approved; will be sent (immediately or at scheduled_send_at)
     sending      = transient claim state — dispatcher has picked this row
     sent         = email delivered successfully
     send_failed  = SMTP delivery failed; visible in Pending Approvals for retry
     rejected     = admin rejected; will not be sent';

COMMENT ON COLUMN draft_responses.scheduled_send_at IS
    'UTC timestamp when the draft should be sent. NULL = send immediately.';

COMMENT ON COLUMN draft_responses.sending_started_at IS
    'Stamped when status transitions to ''sending''. Used by stale-send recovery '
    'to measure elapsed in-flight time rather than time since draft creation.';


-- ── 1c. templates ─────────────────────────────────────────────────────────────
-- Per-category response templates.
-- Placeholders: {{ai_response}}, {{customer_name}}
-- send_delay_seconds: seconds to wait after approval before sending;
--                     NULL = send immediately

CREATE TABLE IF NOT EXISTS templates (
    id                  SERIAL PRIMARY KEY,
    name                VARCHAR(128) UNIQUE NOT NULL,
    query_type          VARCHAR(128),
    subject             VARCHAR(256),
    body                TEXT,
    send_delay_seconds  INTEGER DEFAULT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON COLUMN templates.send_delay_seconds IS
    'Seconds to wait after approval before sending. NULL = immediate.';


-- ── 1d. activity_logs ─────────────────────────────────────────────────────────
-- Full audit trail of every pipeline action.

CREATE TABLE IF NOT EXISTS activity_logs (
    id         SERIAL PRIMARY KEY,
    email_id   INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    action     VARCHAR(128),
    detail     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);


-- ── 1e. app_settings ──────────────────────────────────────────────────────────
-- Key-value store for runtime-editable configuration.
-- The backend reads these live so changes take effect without a restart.
-- Sensitive values (passwords, API keys) are stored encrypted at-rest
-- by Supabase; the app reads them via the service-role client only.

CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR(128) PRIMARY KEY,
    value      TEXT,
    is_secret  BOOLEAN DEFAULT FALSE,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);


-- ── 1f. custom_query_types ────────────────────────────────────────────────────
-- User-defined email query categories with optional keyword hints
-- for the fallback classifier.
-- keywords: comma-separated plain words/phrases; each is converted to a
--           \bword\b regex pattern by the classifier.

CREATE TABLE IF NOT EXISTS custom_query_types (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(128) UNIQUE NOT NULL,
    keywords   TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE  custom_query_types          IS
    'User-defined email query categories with optional keyword hints for the fallback classifier.';
COMMENT ON COLUMN custom_query_types.keywords IS
    'Comma-separated plain words/phrases. Each is converted to a \bword\b regex pattern by the classifier.';


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 2 — INDEXES
-- ─────────────────────────────────────────────────────────────────────────────

-- emails
CREATE INDEX IF NOT EXISTS idx_emails_sender      ON emails(sender);
CREATE INDEX IF NOT EXISTS idx_emails_status      ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at DESC);

-- draft_responses
CREATE INDEX IF NOT EXISTS idx_drafts_email_id ON draft_responses(email_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status   ON draft_responses(status);

-- Speeds up the scheduled-dispatch query (approved drafts with a future send time)
CREATE INDEX IF NOT EXISTS idx_drafts_scheduled
    ON draft_responses (status, scheduled_send_at)
    WHERE status = 'approved' AND scheduled_send_at IS NOT NULL;

-- Speeds up recover_stale_sending_drafts() which scans all 'sending' rows
-- and checks sending_started_at to determine staleness.
CREATE INDEX IF NOT EXISTS idx_drafts_sending_started_at
    ON draft_responses (sending_started_at)
    WHERE status = 'sending';

-- activity_logs
CREATE INDEX IF NOT EXISTS idx_logs_created_at ON activity_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_email_id   ON activity_logs(email_id);

-- custom_query_types
CREATE INDEX IF NOT EXISTS idx_custom_query_types_name ON custom_query_types(name);

-- Enforce one template per query_type (dedup before creating the index).
-- Keeps the oldest row (lowest id) when duplicates exist.
WITH duplicates AS (
    SELECT id
    FROM (
        SELECT
            id,
            query_type,
            ROW_NUMBER() OVER (
                PARTITION BY query_type
                ORDER BY id ASC          -- keep the oldest row (lowest id)
            ) AS rn
        FROM templates
        WHERE query_type IS NOT NULL     -- NULL query_type rows are ignored
    ) ranked
    WHERE rn > 1                         -- surplus rows: all but the first
)
DELETE FROM templates
WHERE id IN (SELECT id FROM duplicates);

CREATE UNIQUE INDEX IF NOT EXISTS idx_templates_query_type_unique
    ON templates (query_type)
    WHERE query_type IS NOT NULL;


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 3 — SEED DATA
-- ─────────────────────────────────────────────────────────────────────────────

-- ── 3a. Default response templates ───────────────────────────────────────────

INSERT INTO templates (name, query_type, subject, body) VALUES
(
    'billing_inquiry', 'billing', 'Re: Your Billing Inquiry',
    'Dear {{customer_name}},

Thank you for reaching out regarding your billing inquiry.

{{ai_response}}

If you have further questions, please don''t hesitate to contact us.

Best regards,
Support Team'
),
(
    'technical_support', 'technical', 'Re: Your Technical Support Request',
    'Dear {{customer_name}},

Thank you for contacting our technical support team.

{{ai_response}}

Please let us know if this resolves your issue.

Best regards,
Technical Support Team'
),
(
    'general_inquiry', 'general', 'Re: Your Inquiry',
    'Dear {{customer_name}},

Thank you for getting in touch with us.

{{ai_response}}

Best regards,
Support Team'
),
(
    'complaint', 'complaint', 'Re: Your Feedback',
    'Dear {{customer_name}},

We sincerely apologize for the inconvenience you have experienced.

{{ai_response}}

We value your feedback and are committed to resolving this promptly.

Best regards,
Support Team'
),
(
    'refund_request', 'refund', 'Re: Your Refund Request',
    'Dear {{customer_name}},

Thank you for contacting us about your refund request.

{{ai_response}}

Best regards,
Billing Team'
)
ON CONFLICT (name) DO NOTHING;


-- ── 3b. Default app settings ──────────────────────────────────────────────────
-- Seeded with empty values; the app falls back to .env if these are not set.
-- Update these via the Settings UI after first run.

INSERT INTO app_settings (key, value, is_secret) VALUES
    ('EMAIL_ADDRESS',         '',    FALSE),
    ('EMAIL_PASSWORD',        '',    TRUE),
    ('IMAP_HOST',             '',    FALSE),
    ('IMAP_PORT',             '993', FALSE),
    ('SMTP_HOST',             '',    FALSE),
    ('SMTP_PORT',             '465', FALSE),
    ('SMTP_MODE',             'ssl', FALSE),
    ('IMAP_SENT_FOLDER',      '',    FALSE),
    ('POLL_INTERVAL_SECONDS', '60',  FALSE),
    ('MAX_REPEAT_COUNT',      '3',   FALSE),
    ('GEMINI_MODEL',          '',    FALSE)
ON CONFLICT (key) DO NOTHING;


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 4 — VIEWS
-- ─────────────────────────────────────────────────────────────────────────────

-- ── client_email_log ──────────────────────────────────────────────────────────
-- Joins the three core tables for easy querying of the full email lifecycle:
--   client_email_id    = who sent it
--   client_query       = what they asked
--   response_generated = what was (or will be) sent back

CREATE OR REPLACE VIEW client_email_log AS
SELECT
    e.id                                        AS email_id,
    e.sender                                    AS client_email_id,
    e.subject,
    e.body                                      AS client_query,
    e.query_type,
    e.received_at,
    e.status                                    AS email_status,
    COALESCE(d.edited_body, d.draft_body)       AS response_generated,
    d.confidence,
    d.status                                    AS response_status,
    d.sent_at
FROM emails e
LEFT JOIN draft_responses d ON d.email_id = e.id
ORDER BY e.received_at DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- SECTION 5 — ROW LEVEL SECURITY
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Policy: admin_only
-- Only users whose Supabase JWT carries app_metadata.role = 'admin' can
-- read or write any table via the REST API (anon key + user JWT).
--
-- The FastAPI backend uses the service-role key which bypasses RLS
-- completely — the pipeline is unaffected by these policies.
--
-- coalesce(..., false) ensures the expression is never NULL, which would
-- cause the policy to silently deny all access including legitimate admins.

-- ── Enable RLS on all tables ──────────────────────────────────────────────────

ALTER TABLE emails             ENABLE ROW LEVEL SECURITY;
ALTER TABLE draft_responses    ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates          ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_logs      ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_settings       ENABLE ROW LEVEL SECURITY;
ALTER TABLE custom_query_types ENABLE ROW LEVEL SECURITY;


-- ── Drop any pre-existing policies before creating the correct ones ───────────
-- Safe to run on a fresh database (IF EXISTS prevents errors).
-- Also safe to re-run on an existing database to upgrade from the old
-- permissive auth_users_all policies to the admin_only policies.

DROP POLICY IF EXISTS auth_users_all ON public.emails;
DROP POLICY IF EXISTS auth_users_all ON public.draft_responses;
DROP POLICY IF EXISTS auth_users_all ON public.templates;
DROP POLICY IF EXISTS auth_users_all ON public.activity_logs;
DROP POLICY IF EXISTS auth_users_all ON public.app_settings;
DROP POLICY IF EXISTS auth_users_all ON public.custom_query_types;
DROP POLICY IF EXISTS admin_only     ON public.emails;
DROP POLICY IF EXISTS admin_only     ON public.draft_responses;
DROP POLICY IF EXISTS admin_only     ON public.templates;
DROP POLICY IF EXISTS admin_only     ON public.activity_logs;
DROP POLICY IF EXISTS admin_only     ON public.app_settings;
DROP POLICY IF EXISTS admin_only     ON public.custom_query_types;


-- ── Create admin_only policies ────────────────────────────────────────────────

CREATE POLICY admin_only ON public.emails
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));

CREATE POLICY admin_only ON public.draft_responses
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));

CREATE POLICY admin_only ON public.templates
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));

CREATE POLICY admin_only ON public.activity_logs
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));

CREATE POLICY admin_only ON public.app_settings
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));

CREATE POLICY admin_only ON public.custom_query_types
    FOR ALL TO authenticated
    USING      (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false))
    WITH CHECK (coalesce((auth.jwt() -> 'app_metadata' ->> 'role') = 'admin', false));


-- ═══════════════════════════════════════════════════════════════════════════════
-- Done.
--
-- You should now have:
--   6 tables  : emails, draft_responses, templates, activity_logs,
--               app_settings, custom_query_types
--   1 view    : client_email_log
--   5 default templates seeded
--   11 app_settings keys seeded
--   6 admin_only RLS policies (one per table)
--
-- Verify policies:
--   SELECT tablename, policyname, cmd
--   FROM pg_policies
--   WHERE schemaname = 'public'
--   ORDER BY tablename;
-- ═══════════════════════════════════════════════════════════════════════════════
