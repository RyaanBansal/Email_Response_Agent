-- ═══════════════════════════════════════════════════════════════════════════════
-- Agentic Email Response System — Supabase Table Setup
-- Run this entire script in: Supabase Dashboard → SQL Editor → New Query
-- ═══════════════════════════════════════════════════════════════════════════════


-- ── 1. emails ─────────────────────────────────────────────────────────────────
-- Stores every incoming client email.
-- client_email_id = sender column
-- client_query    = body column

CREATE TABLE IF NOT EXISTS emails (
    id           SERIAL PRIMARY KEY,
    uid          VARCHAR(128) UNIQUE NOT NULL,   -- IMAP message UID (dedup key)
    sender       VARCHAR(256) NOT NULL,           -- client email address
    subject      VARCHAR(512),
    body         TEXT,                            -- full client query / message
    received_at  TIMESTAMPTZ DEFAULT NOW(),
    is_repeat    BOOLEAN DEFAULT FALSE,
    sender_count INTEGER DEFAULT 1,
    status       VARCHAR(32) DEFAULT 'pending'    -- pending | manual | approved | rejected | sent
                 CHECK (status IN ('pending','manual','approved','rejected','sent')),
    query_type   VARCHAR(128)                     -- billing | technical | general | complaint | refund | onboarding
);

-- Indexes for fast lookups used by the admin console
CREATE INDEX IF NOT EXISTS idx_emails_sender      ON emails(sender);
CREATE INDEX IF NOT EXISTS idx_emails_status      ON emails(status);
CREATE INDEX IF NOT EXISTS idx_emails_received_at ON emails(received_at DESC);


-- ── 2. draft_responses ────────────────────────────────────────────────────────
-- Stores AI-generated drafts.
-- response_generated = draft_body (original AI output) or edited_body (after admin edit)

CREATE TABLE IF NOT EXISTS draft_responses (
    id           SERIAL PRIMARY KEY,
    email_id     INTEGER NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    draft_body   TEXT,              -- original AI-generated response
    edited_body  TEXT,              -- admin-edited version (used if not null)
    confidence   FLOAT DEFAULT 0.0, -- AI classification confidence (0.0–1.0)
    generated_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at  TIMESTAMPTZ,
    rejected_at  TIMESTAMPTZ,
    sent_at      TIMESTAMPTZ,
    status       VARCHAR(32) DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','rejected','sent')),
    admin_note   TEXT
);

CREATE INDEX IF NOT EXISTS idx_drafts_email_id ON draft_responses(email_id);
CREATE INDEX IF NOT EXISTS idx_drafts_status   ON draft_responses(status);


-- ── 3. templates ──────────────────────────────────────────────────────────────
-- Per-category response templates with {{ai_response}} and {{customer_name}} placeholders.

CREATE TABLE IF NOT EXISTS templates (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(128) UNIQUE NOT NULL,
    query_type  VARCHAR(128),
    subject     VARCHAR(256),
    body        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);


-- ── 4. activity_logs ──────────────────────────────────────────────────────────
-- Full audit trail of every pipeline action.

CREATE TABLE IF NOT EXISTS activity_logs (
    id         SERIAL PRIMARY KEY,
    email_id   INTEGER REFERENCES emails(id) ON DELETE SET NULL,
    action     VARCHAR(128),   -- e.g. email_received, draft_generated, email_sent
    detail     TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_logs_created_at ON activity_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_email_id   ON activity_logs(email_id);


-- ── 5. Seed default templates ─────────────────────────────────────────────────

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


-- ── 6. Helpful view: client_email_log ─────────────────────────────────────────
-- This view joins the three core tables so you can query:
--   client_email_id  (who sent it)
--   client_query     (what they asked)
--   response_generated (what was sent back)

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

-- ═══════════════════════════════════════════════════════════════════════════════
-- Done! You should see 4 tables + 1 view in your Supabase Table Editor.
-- ═══════════════════════════════════════════════════════════════════════════════
