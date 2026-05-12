-- ═══════════════════════════════════════════════════════════════════════════════
-- Migration: Timer support, App Settings, and Auth
-- Run AFTER supabase_setup.sql in: Supabase Dashboard → SQL Editor
-- ═══════════════════════════════════════════════════════════════════════════════


-- ── 1. Add send_delay_seconds to templates ────────────────────────────────────
-- After approval, the agent waits this many seconds before sending the email.
-- NULL = send immediately (default behaviour — backwards compatible).

ALTER TABLE templates
    ADD COLUMN IF NOT EXISTS send_delay_seconds INTEGER DEFAULT NULL;

COMMENT ON COLUMN templates.send_delay_seconds IS
    'Seconds to wait after approval before sending. NULL = immediate.';


-- ── 2. Add scheduled_send_at to draft_responses ───────────────────────────────
-- Populated by the orchestrator when a delay is configured.

ALTER TABLE draft_responses
    ADD COLUMN IF NOT EXISTS scheduled_send_at TIMESTAMPTZ DEFAULT NULL;

COMMENT ON COLUMN draft_responses.scheduled_send_at IS
    'UTC timestamp when the draft should be sent. NULL = send immediately.';


-- ── 3. app_settings table ─────────────────────────────────────────────────────
-- Key-value store for runtime-editable configuration.
-- Sensitive values (passwords, API keys) are stored encrypted at-rest
-- by Supabase; the app reads them via the service-role client only.

CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR(128) PRIMARY KEY,
    value      TEXT,
    is_secret  BOOLEAN DEFAULT FALSE,   -- TRUE = mask in UI
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Seed with defaults that mirror the current .env so the UI shows real values
-- on first run. These are safe to leave empty; the app falls back to .env.
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

CREATE TABLE IF NOT EXISTS custom_query_types (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(128) UNIQUE NOT NULL,  -- lowercased, e.g. "warranty"
    keywords   TEXT DEFAULT '',               -- comma-separated hint words
    created_at TIMESTAMPTZ DEFAULT NOW()
);

COMMENT ON TABLE  custom_query_types          IS 'User-defined email query categories with optional keyword hints for the fallback classifier.';
COMMENT ON COLUMN custom_query_types.keywords IS 'Comma-separated plain words/phrases. Each is converted to a \\bword\\b regex pattern by the classifier.';

CREATE INDEX IF NOT EXISTS idx_custom_query_types_name ON custom_query_types(name);


-- ── 4. Row-Level Security ─────────────────────────────────────────────────────
-- Enable RLS on all tables so that only authenticated admin users
-- (service-role key bypasses RLS — used by the backend pipeline) can read/write.

ALTER TABLE emails          ENABLE ROW LEVEL SECURITY;
ALTER TABLE draft_responses ENABLE ROW LEVEL SECURITY;
ALTER TABLE templates       ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_logs   ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_settings    ENABLE ROW LEVEL SECURITY;

-- Allow authenticated users full access (Streamlit frontend authenticates via Supabase Auth)
-- emails
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'emails'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.emails
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;

-- draft_responses
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'draft_responses'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.draft_responses
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;

-- templates
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'templates'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.templates
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;

-- activity_logs
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'activity_logs'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.activity_logs
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;

-- app_settings
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'app_settings'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.app_settings
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;

ALTER TABLE custom_query_types ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'custom_query_types'
      AND policyname = 'auth_users_all'
  ) THEN
    CREATE POLICY auth_users_all
    ON public.custom_query_types
    FOR ALL
    TO authenticated
    USING (true)
    WITH CHECK (true);
  END IF;
END $$;
