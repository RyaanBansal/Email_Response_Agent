"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Fixes applied (this revision)
──────────────────────────────
P1 (approve_and_send race condition):
  Two concurrent HTTP requests could both read status='pending', both pass the
  idempotency guard, and both trigger a send.  Fixed with an atomic conditional
  update: update_draft_if_status() issues
      UPDATE draft_responses SET status='sending', approved_at=...
      WHERE id=? AND status IN ('pending','send_failed') RETURNING *
  Only one caller wins the row; the other gets back an empty result and returns
  True immediately (already claimed by the winner).

P1 (scheduled_send_at not cleared on immediate retry):
  When a send_failed draft was retried with force_immediate=True (or with no
  timer), approve_and_send() set status='approved' but left the old
  scheduled_send_at in place.  On the next scheduler tick the draft would
  appear due and dispatch_scheduled_drafts() would claim it again, causing a
  double-send.  Fixed: the immediate path now always passes
  scheduled_send_at=None to update_draft so any stale timestamp is cleared.

Earlier fixes (preserved)
──────────────────────────
P1 (Failed sends invisible):
  _do_send() sets draft to 'send_failed' / email back to 'approved' on SMTP
  failure.

P1 (Dry-run / missing credentials):
  send_email() returns False when credentials absent (see sender.py).

P2 (Double-send under multiple workers):
  dispatch_scheduled_drafts() uses atomic get_and_claim_scheduled_drafts().

P2 (MAX_REPEAT_COUNT ignored):
  run_pipeline() reads the live setting and routes to manual only after
  >= MAX_REPEAT_COUNT prior emails of the same query type.
"""
import os
from datetime import datetime, timezone, timedelta
from loguru import logger

from app.db.models import (
    get_email_by_id, update_email_status, update_email_query_type,
    get_template_by_query_type, insert_draft, insert_log,
    get_draft_by_id, update_draft, update_draft_if_status,
    get_and_claim_scheduled_drafts,
    count_emails_by_sender_and_query,
)
from app.email.poller import poll_inbox
from app.ai.generator import classify_email, generate_draft


# ── Config helpers ─────────────────────────────────────────────────────────────

def _get_max_repeat_count() -> int:
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

            sender  = record["sender"]
            subject = record.get("subject") or ""
            body    = record.get("body") or ""

            classification = classify_email(subject, body)
            query_type = classification["query_type"]
            update_email_query_type(record["id"], query_type)

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

    Precondition: the draft's status has already been atomically set to
    'sending' by the caller so no other worker can claim the same draft.

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

    FIX P1 (atomic claim — replaces optimistic status check):
    Instead of read-then-check-then-write (which has a TOCTOU race window),
    this function issues a single conditional UPDATE:
        UPDATE draft_responses
        SET status='sending', approved_at=NOW()
        WHERE id=? AND status IN ('pending','send_failed')
        RETURNING *
    Only one concurrent caller wins the row.  The loser gets back an empty
    result and returns True immediately (the winner already handled it).
    This eliminates the double-send window entirely within a single DB node.

    FIX P1 (scheduled_send_at cleared on immediate retry):
    The immediate send path now explicitly passes scheduled_send_at=None so
    that any stale timestamp from a previous scheduled approval is cleared,
    preventing dispatch_scheduled_drafts() from picking up the same draft.

    Returns True on success (sent or scheduled), False on hard failure.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Atomic claim ──────────────────────────────────────────────────────────
    # Flip status to 'sending' only if the current status is one we own.
    # update_draft_if_status() returns the updated row or None if the condition
    # was not met (another worker already claimed or the draft is in a terminal
    # state like 'sent' / 'rejected').
    claimed = update_draft_if_status(
        draft_id,
        from_statuses=("pending", "send_failed"),
        status="sending",
        approved_at=now,
    )
    if claimed is None:
        # Either the draft does not exist or it's already been handled.
        # Fetch the current record to decide how to respond to the caller.
        draft = get_draft_by_id(draft_id)
        if not draft:
            logger.error(f"approve_and_send: draft {draft_id} not found in DB")
            return False
        current = draft.get("status")
        logger.warning(
            f"approve_and_send: draft {draft_id} has status '{current}' "
            f"— skipping (already claimed or terminal state)."
        )
        # Return True so the HTTP caller doesn't surface a spurious error for
        # a draft that was already approved/sent by a concurrent request.
        return True

    # We own the draft — fetch the full row (claimed contains the updated fields
    # but may be a partial dict depending on the Supabase client version).
    draft = get_draft_by_id(draft_id)
    if not draft:
        logger.error(f"approve_and_send: draft {draft_id} vanished after claim")
        return False

    record = get_email_by_id(draft["email_id"])
    if not record:
        logger.error(f"approve_and_send: email {draft['email_id']} not found in DB")
        update_draft(draft_id, status="send_failed")
        return False

    query_type = record.get("query_type") or "general"
    tmpl       = get_template_by_query_type(query_type)
    delay_secs = tmpl.get("send_delay_seconds") if tmpl else None

    # ── Scheduled path ────────────────────────────────────────────────────────
    if not force_immediate and delay_secs and int(delay_secs) > 0:
        send_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(delay_secs))
        ).isoformat()
        # Move from 'sending' → 'approved' with a scheduled timestamp.
        update_draft(draft_id, status="approved", scheduled_send_at=send_at)
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

    # FIX P1: clear any stale scheduled_send_at so the scheduler cannot
    # double-claim this draft after an immediate send.  Status is already
    # 'sending' from the atomic claim above — do not re-set it here to avoid
    # an unnecessary DB round-trip that could mask the claim state.
    update_draft(draft_id, scheduled_send_at=None)
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
