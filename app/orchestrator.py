"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Reliability fixes applied (original)
──────────────────────────────────────
P1 (Failed sends invisible):
  _do_send() sets draft status to 'send_failed' and email status back to
  'approved' when SMTP fails, instead of leaving records silently in limbo.

P1 (Dry-run / missing credentials treated as success):
  send_email() returns False when credentials are absent (see sender.py).

P2 (Double-send under multiple workers):
  dispatch_scheduled_drafts() uses an atomic claim via
  get_and_claim_scheduled_drafts() in models.py.

Additional fixes applied
────────────────────────
P1 (approve_and_send not idempotent):
  approve_and_send() now checks the draft's current status before proceeding.
  Only drafts in 'pending' or 'send_failed' state are approved; any other
  status (already sent, already approved/scheduled, rejected) causes an early
  return so that double-clicks, network retries, and repeated API calls cannot
  trigger a second email send.

P2 (MAX_REPEAT_COUNT ignored):
  run_pipeline() previously routed to manual on the very first repeat of the
  same query type from a sender (threshold was effectively 1).
  MAX_REPEAT_COUNT is now read live from the DB / env and used as the actual
  threshold: a sender must have >= MAX_REPEAT_COUNT prior emails of the same
  query type before that email is held for manual review. This matches the
  documented intent of the setting.
"""
import os
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


# ── Config helpers ─────────────────────────────────────────────────────────────

def _get_max_repeat_count() -> int:
    """
    Read MAX_REPEAT_COUNT from app_settings (live DB) with .env / default fallback.
    Returns the integer threshold — a sender must have this many prior emails of
    the same query type before the current email is routed to the manual queue.
    """
    try:
        from app.db.models import get_setting
        val = get_setting("MAX_REPEAT_COUNT")
        if val:
            return int(val)
    except Exception:
        pass
    return int(os.getenv("MAX_REPEAT_COUNT", "3"))


# ── Pipeline ───────────────────────────────────────────────────────────────────

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

            # Note: no trailing comma — these are plain strings, not tuples.
            sender  = record["sender"]
            subject = record.get("subject") or ""
            body    = record.get("body") or ""

            classification = classify_email(subject, body)
            query_type = classification["query_type"]
            update_email_query_type(record["id"], query_type)

            # FIX P2: Apply MAX_REPEAT_COUNT as the actual routing threshold.
            # Previously `prior_same_query > 0` meant the very first repeat
            # triggered manual routing regardless of the setting value.
            # Now the sender must have >= max_repeats prior emails of the same
            # query type before this one is held for manual review.
            prior_same_query = count_emails_by_sender_and_query(
                sender, query_type, exclude_email_id=record["id"]
            )
            max_repeats = _get_max_repeat_count()

            if prior_same_query >= max_repeats:
                update_email_status(record["id"], "manual")
                insert_log(
                    record["id"], "routed_manual",
                    f"Repeat query ({query_type}) from {record['sender']} "
                    f"— {prior_same_query} prior email(s) with same query type "
                    f"(threshold: {max_repeats})."
                )
                logger.info(
                    f"Email {record['id']} → manual queue "
                    f"(prior_count={prior_same_query} >= max={max_repeats}, "
                    f"query_type='{query_type}', sender={record['sender']})."
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


# ── Send helpers ───────────────────────────────────────────────────────────────

def _do_send(draft_id: int, draft: dict, record: dict) -> bool:
    """
    Internal: call send_email and update DB records on success or failure.

    Precondition: the draft's status has already been set to 'approved' (or
    'sending' for scheduled) by the caller so it no longer appears in the
    pending list regardless of send outcome.

    On SMTP failure the draft is moved to 'send_failed' and the email back to
    'approved', making both visible for admin retry.
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

    # Mark as 'send_failed' so admins can see and retry from Pending Approvals.
    logger.error(f"_do_send: send_email returned False for draft {draft_id}")
    update_draft(draft_id, status="send_failed")
    update_email_status(record["id"], "approved")
    insert_log(record["id"], "send_failed",
               f"Draft {draft_id} failed to send — check SMTP config. "
               f"Draft is in 'send_failed' state and can be retried from Pending Approvals.")
    return False


def approve_and_send(draft_id: int, force_immediate: bool = False) -> bool:
    """
    Approve a draft and send or schedule it.

    FIX P1 (idempotency):
    The draft's current status is checked before any action is taken.
    Only 'pending' and 'send_failed' drafts can be approved — all other
    statuses (sent, approved/scheduled, rejected, sending) cause an early
    return so that double-clicks, network retries, or repeated API calls
    cannot trigger a second email send.

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

    # FIX P1: Idempotency guard — only act on drafts that need to be approved.
    # 'pending'     → normal approval flow.
    # 'send_failed' → admin is retrying a previously failed send; allow it.
    # Any other status (sent, approved, sending, rejected) → already handled;
    # return True so the caller doesn't surface a spurious error.
    current_status = draft.get("status")
    if current_status not in ("pending", "send_failed"):
        logger.warning(
            f"approve_and_send: draft {draft_id} has status '{current_status}' "
            f"— skipping to prevent duplicate action."
        )
        return True

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

    Uses get_and_claim_scheduled_drafts() which atomically sets status to
    'sending' in the same DB query that selects due drafts, preventing
    double-send under concurrent workers.
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
            # Return to approved so it can be investigated / retried.
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
