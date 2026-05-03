"""
app/db/models.py  –  Supabase table helpers
No SQLAlchemy ORM — uses the Supabase Python client directly.
Each function maps to a table operation.
"""
from datetime import datetime, timezone
from loguru import logger
from app.db.database import get_supabase_admin_client


def _db():
    """Shorthand — always returns the admin client for pipeline use."""
    return get_supabase_admin_client()


def utcnow() -> str:
    """ISO timestamp string for Supabase (timestamptz columns)."""
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


def insert_email(uid: str, sender: str, subject: str, body: str,
                 is_repeat: bool, sender_count: int) -> dict | None:
    status = "manual" if is_repeat else "pending"
    res = _db().table("emails").insert({
        "uid":          uid,
        "sender":       sender,
        "subject":      subject,
        "body":         body,
        "received_at":  utcnow(),
        "is_repeat":    is_repeat,
        "sender_count": sender_count,
        "status":       status,
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
    res = (
        _db().table("draft_responses")
        .select("*")
        .eq("status", "pending")
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


def count_drafts_by_status(status: str) -> int:
    res = _db().table("draft_responses").select("id", count="exact").eq("status", status).execute()
    return res.count or 0


def get_sent_drafts_with_times() -> list[dict]:
    """Used for average response time calculation."""
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
# app_settings table  (added by supabase_migration.sql)
# ══════════════════════════════════════════════════════════════════════════════

def get_setting(key: str) -> str | None:
    """Return the value for a settings key, or None if not found / empty."""
    try:
        res = _db().table("app_settings").select("value").eq("key", key).limit(1).execute()
        if res.data and res.data[0].get("value"):
            return res.data[0]["value"]
    except Exception as exc:
        logger.warning(f"get_setting({key!r}) error: {exc}")
    return None


def get_all_settings() -> list[dict]:
    """Return all settings rows ordered by key."""
    try:
        res = _db().table("app_settings").select("*").order("key").execute()
        return res.data or []
    except Exception as exc:
        logger.warning(f"get_all_settings error: {exc}")
        return []


def set_setting(key: str, value: str) -> None:
    """Upsert a settings value."""
    try:
        _db().table("app_settings").upsert({
            "key":        key,
            "value":      value,
            "updated_at": utcnow(),
        }).execute()
    except Exception as exc:
        logger.error(f"set_setting({key!r}) error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Template timer helpers  (send_delay_seconds added by supabase_migration.sql)
# ══════════════════════════════════════════════════════════════════════════════

def update_template_timer(template_id: int, send_delay_seconds: int | None) -> None:
    """Set or clear the send delay for a template."""
    _db().table("templates").update({
        "send_delay_seconds": send_delay_seconds,
        "updated_at":         utcnow(),
    }).eq("id", template_id).execute()


# ══════════════════════════════════════════════════════════════════════════════
# Scheduled-send helpers  (scheduled_send_at added by supabase_migration.sql)
# ══════════════════════════════════════════════════════════════════════════════

def get_scheduled_drafts() -> list[dict]:
    """Return approved drafts whose scheduled_send_at has arrived."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    res = (
        _db().table("draft_responses")
        .select("*")
        .eq("status", "approved")
        .not_.is_("scheduled_send_at", "null")
        .lte("scheduled_send_at", now)
        .execute()
    )
    return res.data or []
