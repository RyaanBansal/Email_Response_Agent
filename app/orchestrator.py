"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Reliability fixes applied
──────────────────────────
P1 (Failed sends invisible):
  _do_send() now sets draft status to 'send_failed' and email status back to
  'approved' when SMTP fails, instead of leaving records silently in limbo.
  The Pending Approvals page queries for both 'pending' AND 'send_failed'
  drafts (see models.get_pending_drafts), so admins can see and retry them.

P1 (Dry-run / missing credentials treated as success):
  send_email() now returns False when credentials are absent (see sender.py).
  _do_send() therefore correctly handles that case as a failure.

P2 (Double-send under multiple workers):
  dispatch_scheduled_drafts() now uses an atomic claim — it updates the draft
  status to 'sending' before dispatching, so a second worker picking up the
  same row will skip it (status != 'approved').  See get_and_claim_scheduled_drafts
  in models.py.
"""
from datetime import datetime, timezone, timedelta
from loguru import logger

from app.db.models import (
    get_email_by_id, update_email_status, update_email_query_type,
    get_template_by_query_type, insert_draft, insert_log,
    get_draft_by_id, update_draft, get_and_claim_scheduled_drafts,
    count_emails_by_sender_and_query,
)
from app.email.poller import poll_inbox
from app.ai.generator import classify_email, generate_draft


def run_pipeline():
    logger.info("─── Pipeline run started ───")
    new_emails = poll_inbox()

    if not new_emails:
        logger.info("No new emails to process.")
        return

    for email_data in new_emails:
        try:
            record = get_email_by_id(email_data["id"])
            if not record:
                continue

            
            sender  = record["sender"],
            subject = record.get("subject") or "",
            body    = record.get("body") or "",
            
            classification = classify_email(subject, body)
            query_type = classification["query_type"]
            update_email_query_type(record["id"], query_type)

            prior_same_query = count_emails_by_sender_and_query(
                sender, query_type, exclude_email_id=record["id"]
            )

            if prior_same_query > 0:
                update_email_status(record["id"], "manual")
                insert_log(
                    record["id"], "routed_manual",
                    f"Repeat query ({query_type}) from {record['sender']} "
                    f"— {prior_same_query} prior email(s) with same query type."
                )
                logger.info(
                    f"Email {record['id']} → manual queue "
                    f"(repeat query_type='{query_type}' from {record['sender']})."
                )
                continue

            tmpl = get_template_by_query_type(query_type)
            template_body = tmpl.get("body") if tmpl else None
 
            draft_body = generate_draft(
                sender        = sender,
                subject       = subject,
                body          = body,
                query_type    = query_type,
                template_body = template_body,
            )
            
            if tmpl and "{{ai_response}}" in (tmpl.get("body") or ""):
                final_draft = tmpl["body"].replace(
                    "{{customer_name}}", record["sender"].split("@")[0].capitalize()
                ).replace("{{ai_response}}", draft_body)
            else:
                final_draft = draft_body

            insert_draft(record["id"], final_draft, classification["confidence"])
            insert_log(record["id"], "draft_generated",
                       f"Type: {query_type} | Confidence: {classification['confidence']:.0%}")
            logger.success(f"Draft generated for email {record['id']} ({query_type})")

        except Exception as exc:
            logger.error(f"Pipeline error on email {email_data.get('id')}: {exc}")

    logger.info("─── Pipeline run complete ───")


def _do_send(draft_id: int, draft: dict, record: dict) -> bool:
    """
    Internal: call send_email and update DB records on success or failure.

    Precondition: the draft's status has already been set to 'approved' (or
    'sending' for scheduled) by the caller so it no longer appears in the
    pending list regardless of send outcome.

    On SMTP failure the draft is moved to 'send_failed' and the email back to
    'approved', making both visible for admin retry.  Previously failures were
    only logged and the records silently disappeared from the UI.
    """
    from app.email.sender import send_email

    body_to_send = draft.get("edited_body") or draft.get("draft_body") or ""
    subject      = f"Re: {record.get('subject') or 'Your Inquiry'}"
    now          = datetime.now(timezone.utc).isoformat()

    logger.info(f"Sending draft {draft_id} to {record['sender']} | Subject: {subject}")
    sent = send_email(to=record["sender"], subject=subject, body=body_to_send)

    if sent:
        update_draft(draft_id, status="sent", sent_at=now)
        update_email_status(record["id"], "sent")
        insert_log(record["id"], "email_sent", f"Draft {draft_id} approved and sent.")
        logger.success(f"Draft {draft_id} sent successfully.")
        return True

    # ── FIX P1: mark as 'send_failed' so admins can see and retry ────────────
    logger.error(f"_do_send: send_email returned False for draft {draft_id}")
    update_draft(draft_id, status="send_failed")
    update_email_status(record["id"], "approved")   # return to retryable state
    insert_log(record["id"], "send_failed",
               f"Draft {draft_id} failed to send — check SMTP config. "
               f"Draft is in 'send_failed' state and can be retried from Pending Approvals.")
    return False


def approve_and_send(draft_id: int, force_immediate: bool = False) -> bool:
    """
    Approve a draft.

    • If force_immediate=True, bypasses any template timer and sends right away.
    • If the matching template has send_delay_seconds set (and force_immediate
      is False), the draft is scheduled.
    • Otherwise sends immediately.

    Returns True on success (sent or scheduled), False on hard failure.
    """
    draft = get_draft_by_id(draft_id)
    if not draft:
        logger.error(f"approve_and_send: draft {draft_id} not found in DB")
        return False

    record = get_email_by_id(draft["email_id"])
    if not record:
        logger.error(f"approve_and_send: email {draft['email_id']} not found in DB")
        return False

    now        = datetime.now(timezone.utc).isoformat()
    query_type = record.get("query_type") or "general"
    tmpl       = get_template_by_query_type(query_type)
    delay_secs = tmpl.get("send_delay_seconds") if tmpl else None

    # ── Scheduled path ────────────────────────────────────────────────────────
    if not force_immediate and delay_secs and int(delay_secs) > 0:
        send_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(delay_secs))
        ).isoformat()
        update_draft(draft_id, status="approved", approved_at=now,
                     scheduled_send_at=send_at)
        update_email_status(record["id"], "approved")
        insert_log(
            record["id"], "email_scheduled",
            f"Draft {draft_id} scheduled for {send_at} ({delay_secs}s delay)."
        )
        logger.info(f"Draft {draft_id} scheduled to send at {send_at}")
        return True

    # ── Immediate path ────────────────────────────────────────────────────────
    if force_immediate and delay_secs and int(delay_secs) > 0:
        logger.info(f"Draft {draft_id} — timer overridden, sending immediately.")
        insert_log(record["id"], "timer_overridden",
                   f"Draft {draft_id} sent immediately despite {delay_secs}s timer.")

    update_draft(draft_id, status="approved", approved_at=now)
    update_email_status(record["id"], "approved")

    return _do_send(draft_id, draft, record)


def dispatch_scheduled_drafts():
    """
    Called periodically by the scheduler.

    FIX P2 (double-send):
    Uses get_and_claim_scheduled_drafts() which atomically sets status to
    'sending' in the same DB query that selects due drafts.  A second worker
    or a second scheduler tick will not find those rows because their status
    is no longer 'approved', eliminating the race condition.
    """
    due = get_and_claim_scheduled_drafts()
    if not due:
        return

    logger.info(f"dispatch_scheduled_drafts: {len(due)} draft(s) due.")
    for draft in due:
        record = get_email_by_id(draft["email_id"])
        if not record:
            logger.warning(
                f"Scheduled draft {draft['id']}: email record missing, skipping."
            )
            # Return to approved so it can be investigated / retried
            update_draft(draft["id"], status="approved")
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
