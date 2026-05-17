"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Fixes applied (this revision)
──────────────────────────────
P1 (Stranded emails after ingestion errors):
  run_pipeline() previously only processed emails returned by the current
  poll_inbox() call.  If draft generation, template lookup, or DB work failed
  for an email, it was silently abandoned: later pipeline runs would never
  see it again because it remained in 'pending' status with no draft but was
  not re-fetched.

  Fix: run_pipeline() now also queries for any 'pending' emails that have no
  associated draft (i.e. draft generation was never completed).  These are
  merged with the freshly polled batch so every run acts as its own recovery
  pass.  A new helper get_pending_emails_without_draft() is added to models.py.

P2 (Template subject lines ignored when sending):
  _do_send() always composed the outbound subject as "Re: <original subject>",
  ignoring the subject field that admins set on response templates.  Changes
  saved in the Templates UI had no effect on delivered email subjects.

  Fix: _do_send() now accepts an optional override_subject parameter.
  approve_and_send() looks up the template for the email's query_type and
  passes its subject (when non-empty) to _do_send().  The "Re: …" fallback is
  kept when no template subject is set so existing behaviour is preserved.

Earlier fixes (preserved)
──────────────────────────
P1 (approve_and_send race condition):
  Atomic conditional UPDATE via update_draft_if_status() prevents double-send.

P1 (scheduled_send_at not cleared on immediate retry):
  Immediate path always passes scheduled_send_at=None.

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
    get_pending_emails_without_draft,   # FIX P1b: new helper
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

def _process_email_record(record: dict, max_repeats: int) -> None:
    """
    Classify, route, draft, and insert a log entry for a single email record.

    Extracted from run_pipeline() so that both the fresh-poll path and the
    stranded-email recovery path share identical logic.
    """
    email_id = record["id"]
    sender   = record["sender"]
    subject  = record.get("subject") or ""
    body     = record.get("body") or ""

    classification = classify_email(subject, body)
    query_type = classification["query_type"]
    update_email_query_type(email_id, query_type)

    prior_same_query = count_emails_by_sender_and_query(
        sender, query_type, exclude_email_id=email_id
    )

    if prior_same_query >= max_repeats:
        update_email_status(email_id, "manual")
        insert_log(
            email_id, "routed_manual",
            f"Repeat query ({query_type}) from {sender} "
            f"— {prior_same_query} prior email(s) with same query type "
            f"(threshold: {max_repeats})."
        )
        logger.info(
            f"Email {email_id} → manual queue "
            f"(prior_count={prior_same_query} >= max={max_repeats}, "
            f"query_type='{query_type}', sender={sender})."
        )
        return

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
            "{{customer_name}}", sender.split("@")[0].capitalize()
        ).replace("{{ai_response}}", draft_body)
    else:
        final_draft = draft_body

    insert_draft(email_id, final_draft, classification["confidence"])
    insert_log(email_id, "draft_generated",
               f"Type: {query_type} | Confidence: {classification['confidence']:.0%}")
    logger.success(f"Draft generated for email {email_id} ({query_type})")


def run_pipeline():
    """
    Poll for new emails AND recover any previously stranded pending emails,
    then process all of them through classification → draft → log.

    FIX P1b (stranded emails):
    After collecting fresh emails from the IMAP inbox, we also query the DB for
    any 'pending' emails that have no draft record yet.  This covers emails
    whose draft-generation step failed in a previous run (exception, API
    timeout, DB error, etc.).  Both sets are merged (deduped by ID) and
    processed together, so every pipeline run doubles as a recovery pass.
    """
    logger.info("─── Pipeline run started ───")

    # 1. Poll IMAP for new unseen messages.
    new_emails = poll_inbox()
    new_ids: set[int] = {e["id"] for e in new_emails}

    # 2. FIX P1b: also recover emails that are stuck in 'pending' with no draft.
    stranded = get_pending_emails_without_draft()
    recovered = [e for e in stranded if e["id"] not in new_ids]
    if recovered:
        logger.info(
            f"Recovering {len(recovered)} stranded pending email(s) "
            f"with no draft: {[e['id'] for e in recovered]}"
        )

    # Merge: process fresh emails first, then stranded ones.
    to_process: list[dict] = []
    for e in new_emails:
        record = get_email_by_id(e["id"])
        if record:
            to_process.append(record)
    to_process.extend(recovered)

    if not to_process:
        logger.info("No new or stranded emails to process.")
        return

    max_repeats = _get_max_repeat_count()

    for record in to_process:
        try:
            _process_email_record(record, max_repeats)
        except Exception as exc:
            logger.error(f"Pipeline error on email {record.get('id')}: {exc}")
            # Leave the email in 'pending' so the next run can retry it.
            # We intentionally do NOT change its status here; the email will
            # be picked up again by get_pending_emails_without_draft().

    logger.info("─── Pipeline run complete ───")


# ── Send helpers ───────────────────────────────────────────────────────────────

def _do_send(draft_id: int, draft: dict, record: dict,
             override_subject: str | None = None) -> bool:
    """
    Internal: call send_email and update DB records on success or failure.

    Precondition: the draft's status has already been atomically set to
    'sending' by the caller so no other worker can claim the same draft.

    FIX P2b (template subject used when sending):
    Accepts an optional override_subject.  When provided (and non-empty) it is
    used as the outbound email subject instead of the "Re: <original>" fallback.
    approve_and_send() passes the template's subject field here so admin edits
    in the Templates UI are actually reflected in delivered emails.

    On SMTP failure the draft is moved to 'send_failed' and the email back to
    'approved', making both visible for admin retry.
    """
    from app.email.sender import send_email

    body_to_send = draft.get("edited_body") or draft.get("draft_body") or ""

    # FIX P2b: prefer template subject over the "Re: …" fallback.
    if override_subject and override_subject.strip():
        subject = override_subject.strip()
    else:
        subject = f"Re: {record.get('subject') or 'Your Inquiry'}"

    now = datetime.now(timezone.utc).isoformat()

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

    FIX P2b (template subject):
    Looks up the template for the email's query_type and passes its subject
    field to _do_send() so the delivered email uses the admin-configured
    subject rather than always defaulting to "Re: <original subject>".

    FIX P1 (atomic claim — replaces optimistic status check):
    Uses update_draft_if_status() to atomically flip status to 'sending'.
    Only one concurrent caller wins the row; the loser returns True immediately.

    FIX P1 (scheduled_send_at cleared on immediate retry):
    The immediate send path passes scheduled_send_at=None to clear any stale
    timestamp from a previous scheduled approval.

    Returns True on success (sent or scheduled), False on hard failure.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Atomic claim ──────────────────────────────────────────────────────────
    claimed = update_draft_if_status(
        draft_id,
        from_statuses=("pending", "send_failed"),
        status="sending",
        approved_at=now,
    )
    if claimed is None:
        draft = get_draft_by_id(draft_id)
        if not draft:
            logger.error(f"approve_and_send: draft {draft_id} not found in DB")
            return False
        current = draft.get("status")
        logger.warning(
            f"approve_and_send: draft {draft_id} has status '{current}' "
            f"— skipping (already claimed or terminal state)."
        )
        return True

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

    # FIX P2b: resolve the subject to use for delivery.
    # Template subject takes priority; fall back to "Re: <original>" otherwise.
    tmpl_subject: str | None = (tmpl.get("subject") or "").strip() if tmpl else None

    # ── Scheduled path ────────────────────────────────────────────────────────
    if not force_immediate and delay_secs and int(delay_secs) > 0:
        send_at = (
            datetime.now(timezone.utc) + timedelta(seconds=int(delay_secs))
        ).isoformat()
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
    # double-claim this draft after an immediate send.
    update_draft(draft_id, scheduled_send_at=None)
    update_email_status(record["id"], "approved")

    # FIX P2b: pass template subject to _do_send().
    return _do_send(draft_id, draft, record, override_subject=tmpl_subject)


def dispatch_scheduled_drafts():
    """
    Called periodically by the scheduler.

    Uses get_and_claim_scheduled_drafts() which atomically sets status to
    'sending' in the same DB query that selects due drafts, preventing
    double-send under concurrent workers.

    FIX P2b: passes the template subject to _do_send() for each due draft.
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

        # FIX P2b: resolve template subject for scheduled sends too.
        query_type = record.get("query_type") or "general"
        tmpl = get_template_by_query_type(query_type)
        tmpl_subject: str | None = (tmpl.get("subject") or "").strip() if tmpl else None

        _do_send(draft["id"], draft, record, override_subject=tmpl_subject)


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
