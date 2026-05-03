"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Changes:
  • approve_and_send now checks template.send_delay_seconds; if set it schedules
    the send rather than dispatching immediately.
  • dispatch_scheduled_drafts() is called by the scheduler to flush queued sends.
  • Settings from app_settings override env vars for IMAP/SMTP at runtime.
"""
from datetime import datetime, timezone, timedelta
from loguru import logger

from app.db.models import (
    get_email_by_id, update_email_status, update_email_query_type,
    get_template_by_query_type, insert_draft, insert_log,
    get_draft_by_id, update_draft, get_scheduled_drafts,
)
from app.email.poller import poll_inbox
from app.ai.generator import process_email


def run_pipeline():
    logger.info("─── Pipeline run started ───")
    new_emails = poll_inbox()

    if not new_emails:
        logger.info("No new emails to process.")
        return

    for email_data in new_emails:
        if email_data["is_repeat"]:
            logger.info(f"Email {email_data['id']} → manual queue (repeat sender).")
            insert_log(email_data["id"], "routed_manual", "Repeat sender — manual handling required.")
            continue

        try:
            record = get_email_by_id(email_data["id"])
            if not record:
                continue

            result = process_email(
                sender  = record["sender"],
                subject = record.get("subject") or "",
                body    = record.get("body") or "",
            )

            update_email_query_type(record["id"], result["query_type"])

            # Apply template wrapper if available
            tmpl = get_template_by_query_type(result["query_type"])
            if tmpl and "{{ai_response}}" in (tmpl.get("body") or ""):
                final_draft = tmpl["body"].replace(
                    "{{customer_name}}", record["sender"].split("@")[0].capitalize()
                ).replace("{{ai_response}}", result["draft"])
            else:
                final_draft = result["draft"]

            insert_draft(record["id"], final_draft, result["confidence"])
            insert_log(record["id"], "draft_generated",
                       f"Type: {result['query_type']} | Confidence: {result['confidence']:.0%}")
            logger.success(f"Draft generated for email {record['id']} ({result['query_type']})")

        except Exception as exc:
            logger.error(f"Pipeline error on email {email_data.get('id')}: {exc}")

    logger.info("─── Pipeline run complete ───")


def _do_send(draft_id: int, draft: dict, record: dict) -> bool:
    """Internal: actually call send_email and update DB records."""
    from app.email.sender import send_email

    body_to_send = draft.get("edited_body") or draft.get("draft_body") or ""
    subject      = f"Re: {record.get('subject') or 'Your Inquiry'}"
    now          = datetime.now(timezone.utc).isoformat()

    logger.info(f"Sending draft {draft_id} to {record['sender']} | Subject: {subject}")
    sent = send_email(to=record["sender"], subject=subject, body=body_to_send)

    if sent:
        update_draft(draft_id, status="sent", sent_at=now, approved_at=now)
        update_email_status(record["id"], "sent")
        insert_log(record["id"], "email_sent", f"Draft {draft_id} approved and sent.")
        logger.success(f"Draft {draft_id} sent successfully.")
        return True

    logger.error(f"_do_send: send_email returned False for draft {draft_id}")
    insert_log(record["id"], "send_failed", f"Draft {draft_id} failed to send — check SMTP config.")
    return False


def approve_and_send(draft_id: int) -> bool:
    """
    Approve a draft.

    • If the matching template has send_delay_seconds set, the draft is marked
      'approved' and scheduled_send_at is stamped. The scheduler will call
      dispatch_scheduled_drafts() to pick it up later.
    • Otherwise sends immediately (original behaviour).

    Returns:
      True  — either sent immediately or scheduled successfully
      False — draft / email record missing, or SMTP failure on immediate send
    """
    draft = get_draft_by_id(draft_id)
    if not draft:
        logger.error(f"approve_and_send: draft {draft_id} not found in DB")
        return False

    record = get_email_by_id(draft["email_id"])
    if not record:
        logger.error(f"approve_and_send: email {draft['email_id']} not found in DB")
        return False

    # Check if the template for this email type has a send delay
    query_type = record.get("query_type") or "general"
    tmpl = get_template_by_query_type(query_type)
    delay_seconds = tmpl.get("send_delay_seconds") if tmpl else None

    if delay_seconds and int(delay_seconds) > 0:
        # Schedule the send
        send_at = (datetime.now(timezone.utc) + timedelta(seconds=int(delay_seconds))).isoformat()
        now     = datetime.now(timezone.utc).isoformat()
        update_draft(draft_id, status="approved", approved_at=now, scheduled_send_at=send_at)
        update_email_status(record["id"], "approved")
        insert_log(
            record["id"], "email_scheduled",
            f"Draft {draft_id} scheduled for {send_at} ({delay_seconds}s delay)."
        )
        logger.info(f"Draft {draft_id} scheduled to send at {send_at}")
        return True  # "success" — just deferred

    # No delay → send immediately
    now = datetime.now(timezone.utc).isoformat()
    update_draft(draft_id, approved_at=now)
    return _do_send(draft_id, draft, record)


def dispatch_scheduled_drafts():
    """
    Called periodically by the scheduler.
    Finds approved drafts whose scheduled_send_at has passed and sends them.
    """
    due = get_scheduled_drafts()
    if not due:
        return

    logger.info(f"dispatch_scheduled_drafts: {len(due)} draft(s) due.")
    for draft in due:
        record = get_email_by_id(draft["email_id"])
        if not record:
            logger.warning(f"Scheduled draft {draft['id']}: email record missing, skipping.")
            continue
        _do_send(draft["id"], draft, record)


def reject_draft(draft_id: int, note: str = "") -> bool:
    draft = get_draft_by_id(draft_id)
    if not draft:
        return False
    now = datetime.now(timezone.utc).isoformat()
    update_draft(draft_id, status="rejected", rejected_at=now, admin_note=note)
    update_email_status(draft["email_id"], "rejected")
    insert_log(draft["email_id"], "draft_rejected", note or "No reason given.")
    return True


def save_edited_draft(draft_id: int, edited_body: str) -> bool:
    try:
        update_draft(draft_id, edited_body=edited_body)
        return True
    except Exception as exc:
        logger.error(f"Save edit error: {exc}")
        return False
