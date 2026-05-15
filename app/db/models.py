"""
app/db/models.py  –  Supabase table helpers

Fixes applied (this revision)
──────────────────────────────
P1 (approve_and_send race condition):
  Added update_draft_if_status() — an atomic conditional UPDATE that flips a
  draft's status only when it currently matches an expected set of values.
  Implemented as UPDATE ... WHERE status IN (...) RETURNING * via the Supabase
  Python client's .update().in_().execute() with return=representation.
  Only the one caller whose UPDATE touches a row gets the row back; every
  other concurrent caller gets an empty result and backs off.

Earlier fixes (preserved)
──────────────────────────
P1 (send_failed invisible):
  get_pending_drafts() returns both 'pending' and 'send_failed' drafts.

P2 (double-send):
  get_and_claim_scheduled_drafts() uses atomic update-then-select pattern.
"""
from datetime import datetime, timezone
from loguru import logger
from app.db.database import get_supabase_admin_client


def _db():
    return get_supabase_admin_client()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    FIX P1: Previously only 'pending' was returned, so 'send_failed' drafts
    disappeared from the UI after an SMTP failure.  Admins can now see and
    retry them from the Pending Approvals page.
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
) -> dict | None:
    """
    Atomic conditional update: set `fields` on draft `draft_id` only when its
    current status is one of `from_statuses`.  Returns the updated row dict if
    the condition matched, or None if the row was not updated (status mismatch,
    or the row does not exist).

    This is the building block for race-free draft claiming.  The Supabase
    Python client issues a single PATCH with Prefer: return=representation,
    which is effectively:

        UPDATE draft_responses
        SET    <fields>
        WHERE  id = draft_id
          AND  status = ANY(from_statuses)
        RETURNING *;

    Only the first concurrent caller whose UPDATE touches the row gets it
    back; every subsequent caller finds the status has already changed and
    receives an empty result.

    NOTE: For true serialisable safety across multiple DB replicas or under
    high concurrency, replace this with a PL/pgSQL function that uses
    SELECT ... FOR UPDATE SKIP LOCKED.  For the typical single-node Supabase
    deployment this implementation is sufficient.
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
        return None


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


# ══════════════════════════════════════════════════════════════════════════════
# templates table
# ══════════════════════════════════════════════════════════════════════════════

def get_all_templates() -> list[dict]:
    res = _db().table("templates").select("*").order("query_type").execute()
    return res.data or []


def get_template_by_query_type(query_type: str) -> dict | None:
    res = (
        _db().table("templates")
        .select("*")
        .eq("query_type", query_type)
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
    passed by flipping their status to 'sending' in the same operation.

    FIX P2 (double-send prevention):
    The update() call sets status='sending' on all qualifying rows before
    returning them.  A second scheduler worker or tick will not find these
    rows because their status is no longer 'approved', so they cannot be
    dispatched twice.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        res = (
            _db().table("draft_responses")
            .update({"status": "sending"})
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
