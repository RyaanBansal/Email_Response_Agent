"""
app/db/models.py  –  Supabase table helpers

Fixes applied (this revision)
──────────────────────────────
P1 (Stale-send recovery could mark an actively-sending draft as failed):
  recover_stale_sending_drafts() previously used generated_at to decide
  whether a 'sending' draft was stale.  Any draft created more than
  SENDING_TIMEOUT_MINUTES ago was immediately eligible for recovery the
  instant it entered 'sending', even if SMTP was still running.  Scheduled
  sends were especially exposed: get_and_claim_scheduled_drafts() set only
  status='sending' with no claim timestamp, so a concurrent pipeline run
  could flip a legitimately in-flight send to 'send_failed'.

  Fix: both claim paths (approve_and_send and get_and_claim_scheduled_drafts)
  now stamp sending_started_at=utcnow() at the moment status transitions to
  'sending'.  recover_stale_sending_drafts() filters on sending_started_at
  instead of generated_at, so a draft is only eligible for recovery after
  SENDING_TIMEOUT_MINUTES have elapsed since the claim — not since it was
  first created.  Rows with a NULL sending_started_at (created by old code)
  fall back to generated_at, preserving the previous conservative behaviour
  for any pre-existing stuck rows.

P2 (update_draft_if_status hides DB errors):
  Returns the module-level _DB_ERROR sentinel on exception instead of None,
  so callers can distinguish "another worker claimed it first" (None → benign
  skip) from "the DB call itself failed" (_DB_ERROR → hard failure that must
  be surfaced to the admin).

P2 (nondeterministic template selection):
  get_template_by_query_type() now orders by id ASC so the oldest
  (first-created) template wins when duplicates exist, giving deterministic
  behaviour at runtime.  A UNIQUE index on templates(query_type) is added via
  migration (see supabase_migration_unique_query_type.sql) to prevent new
  duplicates at the DB level.

Earlier fixes (preserved)
──────────────────────────
P1 (approve_and_send race condition):
  update_draft_if_status() — atomic conditional UPDATE.

P1 (send_failed invisible):
  get_pending_drafts() returns 'pending' and 'send_failed'.

P1 (stranded emails):
  get_pending_emails_without_draft().

P2 (double-send):
  get_and_claim_scheduled_drafts() atomic update-then-select.
"""
from datetime import datetime, timezone, timedelta
from loguru import logger
from app.db.database import get_supabase_admin_client


def _db():
    return get_supabase_admin_client()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Sentinel returned by update_draft_if_status() on a real DB error.
# Distinct from None, which means "status mismatch / row not found".
# Using a dedicated class (not a string or bool) makes isinstance checks
# unambiguous and avoids accidental equality with normal return values.
# ---------------------------------------------------------------------------
class _DbErrorType:
    """Singleton sentinel — one instance, _DB_ERROR, used as a return value."""
    _instance = None
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    def __repr__(self):
        return "_DB_ERROR"

_DB_ERROR = _DbErrorType()

# How long a draft may remain in 'sending' before being considered stale.
# Must be comfortably longer than any realistic SMTP timeout (20 s in sender.py)
# but short enough to surface crashes promptly.  10 minutes is conservative.
SENDING_TIMEOUT_MINUTES = 10


# ══════════════════════════════════════════════════════════════════════════════
# emails table
# ══════════════════════════════════════════════════════════════════════════════

def get_email_by_uid(uid: str) -> dict | None:
    res = _db().table("emails").select("*").eq("uid", uid).limit(1).execute()
    return res.data[0] if res.data else None


def count_emails_by_sender(sender: str) -> int:
    res = _db().table("emails").select("id", count="exact").eq("sender", sender).execute()
    return res.count or 0


def count_emails_by_sender_and_query(sender: str, query_type: str,
                                     exclude_email_id: int | None = None) -> int:
    q = (
        _db().table("emails")
        .select("id", count="exact")
        .eq("sender", sender)
        .eq("query_type", query_type)
    )
    if exclude_email_id is not None:
        q = q.neq("id", exclude_email_id)
    res = q.execute()
    return res.count or 0


def insert_email(uid: str, sender: str, subject: str, body: str,
                 is_repeat: bool, sender_count: int) -> dict | None:
    res = _db().table("emails").insert({
        "uid":          uid,
        "sender":       sender,
        "subject":      subject,
        "body":         body,
        "received_at":  utcnow(),
        "is_repeat":    is_repeat,
        "sender_count": sender_count,
        "status":       "pending",
    }).execute()
    return res.data[0] if res.data else None


def update_email_status(email_id: int, status: str) -> None:
    _db().table("emails").update({"status": status}).eq("id", email_id).execute()


def update_email_query_type(email_id: int, query_type: str) -> None:
    _db().table("emails").update({"query_type": query_type}).eq("id", email_id).execute()


def get_email_by_id(email_id: int) -> dict | None:
    res = _db().table("emails").select("*").eq("id", email_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_emails_by_status(status: str) -> list[dict]:
    res = (
        _db().table("emails")
        .select("*")
        .eq("status", status)
        .order("received_at", desc=True)
        .execute()
    )
    return res.data or []


def count_emails_received_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    res = (
        _db().table("emails")
        .select("id", count="exact")
        .gte("received_at", f"{today}T00:00:00+00:00")
        .execute()
    )
    return res.count or 0


def get_pending_emails_without_draft() -> list[dict]:
    """
    Return 'pending' emails that have no associated draft_responses row.

    FIX P1b (stranded emails — earlier fix, preserved):
    These are emails inserted by poll_inbox() whose downstream processing
    failed before a draft record was created.  run_pipeline() calls this each
    run so they are retried automatically rather than silently abandoned.
    """
    try:
        pending_res = (
            _db().table("emails")
            .select("*")
            .eq("status", "pending")
            .order("received_at", desc=True)
            .execute()
        )
        pending_rows: list[dict] = pending_res.data or []
        if not pending_rows:
            return []

        pending_ids = [r["id"] for r in pending_rows]

        drafted_res = (
            _db().table("draft_responses")
            .select("email_id")
            .in_("email_id", pending_ids)
            .execute()
        )
        drafted_ids: set[int] = {r["email_id"] for r in (drafted_res.data or [])}

        return [r for r in pending_rows if r["id"] not in drafted_ids]

    except Exception as exc:
        logger.error(f"get_pending_emails_without_draft error: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# draft_responses table
# ══════════════════════════════════════════════════════════════════════════════

def insert_draft(email_id: int, draft_body: str, confidence: float) -> dict | None:
    res = _db().table("draft_responses").insert({
        "email_id":     email_id,
        "draft_body":   draft_body,
        "confidence":   round(confidence, 4),
        "generated_at": utcnow(),
        "status":       "pending",
    }).execute()
    return res.data[0] if res.data else None


def get_pending_drafts() -> list[dict]:
    """
    Return drafts that need admin attention: pending approvals AND failed sends.

    FIX P1 (earlier fix, preserved):
    'send_failed' drafts were previously invisible after an SMTP failure.
    """
    res = (
        _db().table("draft_responses")
        .select("*")
        .in_("status", ["pending", "send_failed"])
        .order("generated_at", desc=True)
        .execute()
    )
    return res.data or []


def get_draft_by_id(draft_id: int) -> dict | None:
    res = _db().table("draft_responses").select("*").eq("id", draft_id).limit(1).execute()
    return res.data[0] if res.data else None


def get_draft_for_email(email_id: int, status: str | None = None) -> dict | None:
    q = _db().table("draft_responses").select("*").eq("email_id", email_id)
    if status:
        q = q.eq("status", status)
    res = q.order("generated_at", desc=True).limit(1).execute()
    return res.data[0] if res.data else None


def update_draft(draft_id: int, **fields) -> None:
    _db().table("draft_responses").update(fields).eq("id", draft_id).execute()


def update_draft_if_status(
    draft_id: int,
    from_statuses: tuple[str, ...],
    **fields,
) -> dict | None | _DbErrorType:
    """
    Atomic conditional update: apply `fields` to draft `draft_id` only when
    its current status is one of `from_statuses`.

    Return values
    ─────────────
    dict          — The updated row.  This caller won the atomic claim.
    None          — Status mismatch or row not found.  Another worker already
                    claimed the draft, or it reached a terminal state.  The
                    caller should treat this as a benign idempotent skip.
    _DB_ERROR     — A real DB / network exception occurred.  The draft was NOT
                    updated.  The caller must treat this as a hard failure and
                    surface it to the admin (return False / HTTP 500).

    FIX P2 (this revision):
    Previously returned None for both "status mismatch" and actual exceptions,
    so approve_and_send() silently skipped drafts on transient DB failures
    while showing "Approved" in the UI.  _DB_ERROR lets callers distinguish
    the two cases.
    """
    try:
        res = (
            _db().table("draft_responses")
            .update(fields)
            .eq("id", draft_id)
            .in_("status", list(from_statuses))
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error(f"update_draft_if_status({draft_id}) error: {exc}")
        return _DB_ERROR


def count_drafts_by_status(status: str) -> int:
    res = _db().table("draft_responses").select("id", count="exact").eq("status", status).execute()
    return res.count or 0


def get_sent_drafts_with_times() -> list[dict]:
    res = (
        _db().table("draft_responses")
        .select("generated_at, sent_at")
        .eq("status", "sent")
        .not_.is_("sent_at", "null")
        .execute()
    )
    return res.data or []


def recover_stale_sending_drafts() -> int:
    """
    Reset drafts stuck in 'sending' for longer than SENDING_TIMEOUT_MINUTES
    back to 'send_failed', and roll the parent email back to 'approved'.

    FIX P1 (stale 'sending' drafts permanently hidden):
    'sending' is a transient claim set just before SMTP delivery.  If the
    process crashes, is OOM-killed, or raises an unhandled exception after the
    claim but before the status is updated to 'sent' or 'send_failed', the
    draft stays in 'sending' forever.  Pending Approvals only surfaces
    'pending' and 'send_failed', so stale 'sending' drafts are permanently
    invisible.

    Timestamp choice — sending_started_at:
    Both claim paths (approve_and_send and get_and_claim_scheduled_drafts) now
    stamp sending_started_at=utcnow() at the moment they flip the draft to
    'sending'.  Staleness is measured from that instant, so a draft that was
    generated hours ago but only just claimed is never incorrectly recovered
    while SMTP is still in flight.

    Fallback for rows without sending_started_at (NULL):
    Rows that entered 'sending' before this column existed have no claim time.
    For those we fall back to generated_at, which preserves the conservative
    behaviour of the previous implementation and ensures they are eventually
    recovered rather than hidden forever.

    Called at the top of every run_pipeline() pass so the recovery window is
    bounded by poll_interval + SENDING_TIMEOUT_MINUTES.

    Returns the number of drafts successfully reset.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=SENDING_TIMEOUT_MINUTES)
    ).isoformat()

    try:
        # Fetch all drafts currently in 'sending'.  We select both timestamps
        # so we can apply the correct one per row in Python.
        res = (
            _db().table("draft_responses")
            .select("id, email_id, generated_at, sending_started_at")
            .eq("status", "sending")
            .execute()
        )
        candidates: list[dict] = res.data or []
    except Exception as exc:
        logger.error(f"recover_stale_sending_drafts: query failed: {exc}")
        return 0

    # A draft is stale when its claim timestamp is older than the cutoff.
    # Use sending_started_at if present; fall back to generated_at for rows
    # that pre-date the column (NULL means they were claimed by old code).
    stale = [
        d for d in candidates
        if (d.get("sending_started_at") or d.get("generated_at") or "") <= cutoff
    ]

    if not stale:
        return 0

    logger.warning(
        f"recover_stale_sending_drafts: {len(stale)} stale draft(s) in 'sending' "
        f"(older than {SENDING_TIMEOUT_MINUTES} min): {[d['id'] for d in stale]}"
    )

    recovered = 0
    for draft in stale:
        claim_ts = draft.get("sending_started_at") or draft.get("generated_at", "unknown")
        try:
            update_draft(draft["id"], status="send_failed")
            update_email_status(draft["email_id"], "approved")
            insert_log(
                draft["email_id"],
                "send_stale_recovered",
                f"Draft {draft['id']} was stuck in 'sending' since "
                f"{claim_ts}; reset to 'send_failed' for admin retry.",
            )
            recovered += 1
            logger.info(
                f"recover_stale_sending_drafts: reset draft {draft['id']} "
                f"(email {draft['email_id']}) to 'send_failed'."
            )
        except Exception as exc:
            logger.error(
                f"recover_stale_sending_drafts: failed to reset draft "
                f"{draft['id']}: {exc}"
            )

    logger.info(
        f"recover_stale_sending_drafts: reset {recovered}/{len(stale)} draft(s)."
    )
    return recovered


# ══════════════════════════════════════════════════════════════════════════════
# templates table
# ══════════════════════════════════════════════════════════════════════════════

def get_all_templates() -> list[dict]:
    res = _db().table("templates").select("*").order("query_type").execute()
    return res.data or []


def get_template_by_query_type(query_type: str) -> dict | None:
    """
    Return the template for query_type.

    FIX P2 (nondeterministic selection):
    Previously used limit(1) with no ordering, so when multiple templates
    share the same query_type the returned row was arbitrary.  Now orders by
    id ASC so the oldest (first-created) template always wins, giving
    deterministic behaviour until the UNIQUE constraint migration is applied.
    """
    res = (
        _db().table("templates")
        .select("*")
        .eq("query_type", query_type)
        .order("id", desc=False)    # oldest row wins; deterministic with duplicates
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def update_template(template_id: int, subject: str, body: str) -> None:
    _db().table("templates").update({
        "subject":    subject,
        "body":       body,
        "updated_at": utcnow(),
    }).eq("id", template_id).execute()


def insert_template(name: str, query_type: str, subject: str, body: str) -> None:
    _db().table("templates").insert({
        "name":       name,
        "query_type": query_type,
        "subject":    subject,
        "body":       body,
    }).execute()


# ══════════════════════════════════════════════════════════════════════════════
# activity_logs table
# ══════════════════════════════════════════════════════════════════════════════

def insert_log(email_id: int | None, action: str, detail: str) -> None:
    try:
        _db().table("activity_logs").insert({
            "email_id":   email_id,
            "action":     action,
            "detail":     detail,
            "created_at": utcnow(),
        }).execute()
    except Exception as exc:
        logger.warning(f"Log write failed: {exc}")


def get_recent_logs(limit: int = 20) -> list[dict]:
    res = (
        _db().table("activity_logs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    return res.data or []


# ══════════════════════════════════════════════════════════════════════════════
# app_settings table
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str) -> str | None:
    try:
        res = _db().table("app_settings").select("value").eq("key", key).limit(1).execute()
        if res.data and res.data[0].get("value"):
            return res.data[0]["value"]
    except Exception as exc:
        logger.warning(f"get_setting({key!r}) error: {exc}")
    return None


def get_all_settings() -> list[dict]:
    try:
        res = _db().table("app_settings").select("*").order("key").execute()
        return res.data or []
    except Exception as exc:
        logger.warning(f"get_all_settings error: {exc}")
        return []


def set_setting(key: str, value: str) -> None:
    try:
        _db().table("app_settings").upsert({
            "key":        key,
            "value":      value,
            "updated_at": utcnow(),
        }).execute()
    except Exception as exc:
        logger.error(f"set_setting({key!r}) error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Template timer helpers
# ══════════════════════════════════════════════════════════════════════════════

def update_template_timer(template_id: int, send_delay_seconds: int | None) -> None:
    _db().table("templates").update({
        "send_delay_seconds": send_delay_seconds,
        "updated_at":         utcnow(),
    }).eq("id", template_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# Scheduled-send helpers
# ══════════════════════════════════════════════════════════════════════════════

def get_and_claim_scheduled_drafts() -> list[dict]:
    """
    Atomically claim and return approved drafts whose scheduled_send_at has
    passed by flipping their status to 'sending' in the same DB operation.

    FIX P2 (double-send prevention — earlier fix, preserved):
    A second scheduler tick or worker finds status='sending' and skips the
    row, preventing duplicate delivery.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        res = (
            _db().table("draft_responses")
            # FIX P1 (stale-send recovery): stamp sending_started_at so
            # recover_stale_sending_drafts() measures staleness from the actual
            # claim time, not from generated_at.  Without this, a draft that was
            # created more than SENDING_TIMEOUT_MINUTES ago is flagged as stale
            # the instant it enters 'sending', even if SMTP is still running.
            .update({"status": "sending", "sending_started_at": now})
            .eq("status", "approved")
            .not_.is_("scheduled_send_at", "null")
            .lte("scheduled_send_at", now)
            .execute()
        )
        return res.data or []
    except Exception as exc:
        logger.error(f"get_and_claim_scheduled_drafts error: {exc}")
        return []


# ══════════════════════════════════════════════════════════════════════════════
# custom_query_types table
# ══════════════════════════════════════════════════════════════════════════════

def get_custom_query_types() -> list[dict]:
    try:
        res = _db().table("custom_query_types").select("*").order("name").execute()
        return res.data or []
    except Exception as exc:
        logger.warning(f"get_custom_query_types error: {exc}")
        return []


def upsert_custom_query_type(name: str, keywords: str) -> dict | None:
    name = name.strip().lower()
    try:
        res = _db().table("custom_query_types").upsert({
            "name":       name,
            "keywords":   keywords.strip(),
            "created_at": utcnow(),
        }, on_conflict="name").execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        logger.error(f"upsert_custom_query_type({name!r}) error: {exc}")
        return None


def delete_custom_query_type(name: str) -> bool:
    name = name.strip().lower()
    try:
        _db().table("custom_query_types").delete().eq("name", name).execute()
        return True
    except Exception as exc:
        logger.error(f"delete_custom_query_type({name!r}) error: {exc}")
        return False
