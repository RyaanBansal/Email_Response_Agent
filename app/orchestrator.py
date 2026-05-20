"""
app/orchestrator.py  –  Main pipeline orchestrator (Supabase version)

Fixes applied (this revision)
──────────────────────────────
P1 (Stale-send recovery could mark an actively-sending draft as failed):
  approve_and_send() now stamps sending_started_at=now in its atomic claim.
  This pairs with the same stamp in get_and_claim_scheduled_drafts() so
  recover_stale_sending_drafts() measures the timeout from the real claim
  time rather than generated_at, preventing a freshly-claimed draft from
  being falsely recovered while SMTP is still running.

P1 (int() on live settings can raise inside critical sections):
  _get_max_repeat_count() and approve_and_send() used bare int() on values
  read from app_settings.  A bad stored value (e.g. "3x") would raise
  ValueError mid-flight.  In approve_and_send() that could happen after the
  draft was already claimed as 'sending', leaving it permanently hidden.
  Both callers now use _safe_int() which falls back to a sensible default and
  logs a warning instead of raising.

P2 (update_draft_if_status DB errors silently treated as success):
  The function now returns the _DB_ERROR sentinel on exception.
  approve_and_send() checks for it explicitly and returns False so the API
  endpoint raises HTTP 500 and the admin sees the failure rather than a
  spurious "Approved" toast.

Earlier fixes (preserved)
──────────────────────────
P1 (stranded emails): run_pipeline() recovers pending emails with no draft.
P2 (template subject): _do_send() uses the template subject when available.
P1 (race condition / double-send): atomic claim via update_draft_if_status().
P1 (scheduled_send_at cleared on immediate retry).
P1 (failed sends visible as send_failed).
P2 (double-send under multiple workers).
P2 (MAX_REPEAT_COUNT respected).
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
    get_pending_emails_without_draft,
    recover_stale_sending_drafts,
    _DB_ERROR,
    _DbErrorType,
)
from app.email.poller import poll_inbox
from app.ai.generator import classify_email, generate_draft


# ── Internal helpers ───────────────────────────────────────────────────────────

def _safe_int(value: str, default: int, label: str) -> int:
    """
    Parse value as int, returning default on failure.

    FIX P1: Replaces bare int() calls on live-settings values so that a bad
    stored value (e.g. "3x") never raises ValueError inside a critical section
    that has already mutated state (e.g. after claiming a draft as 'sending').
    """
    try:
        return int(value)
    except (ValueError, TypeError):
        logger.warning(
            f"orchestrator: {label}={value!r} is not a valid integer; "
            f"using default {default}."
        )
        return default


def _get_max_repeat_count() -> int:
    try:
        from app.db.models import get_setting
        val = get_setting("MAX_REPEAT_COUNT")
        if val:
            # FIX P1: was bare int(val) — raises ValueError on bad stored value.
            return _safe_int(val, 3, "MAX_REPEAT_COUNT")
    except Exception:
        pass
    return _safe_int(os.getenv("MAX_REPEAT_COUNT", "3"), 3, "MAX_REPEAT_COUNT")


# ── Pipeline ───────────────────────────────────────────────────────────────────

def _process_email_record(record: dict, max_repeats: int) -> None:
    """
    Classify, route, draft, and insert a log entry for a single email record.
    Extracted so that both the fresh-poll path and stranded-email recovery
    share identical logic.
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
            f"(threshold: {max_repeats}).",
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


def run_pipeline() -> None:
    """
    Poll for new emails, recover stale/stranded records, then process
    everything through classification → draft → log.

    FIX P1 (stale 'sending' drafts):
    Calls recover_stale_sending_drafts() first so any draft left in 'sending'
    by a previous crashed run is promoted to 'send_failed' before the rest of
    the pipeline runs.

    FIX P1 (stranded emails — earlier fix, preserved):
    Also queries for 'pending' emails with no draft and merges them into the
    current batch for automatic retry.
    """
    logger.info("─── Pipeline run started ───")

    # FIX P1: recover drafts stuck in 'sending' from a previous crashed run.
    recover_stale_sending_drafts()

    # Poll IMAP for new unseen messages.
    new_emails = poll_inbox()
    new_ids: set[int] = {e["id"] for e in new_emails}

    # FIX P1b (earlier): recover 'pending' emails with no draft.
    stranded = get_pending_emails_without_draft()
    recovered_emails = [e for e in stranded if e["id"] not in new_ids]
    if recovered_emails:
        logger.info(
            f"Recovering {len(recovered_emails)} stranded pending email(s) "
            f"with no draft: {[e['id'] for e in recovered_emails]}"
        )

    # Merge: process fresh emails first, then stranded ones.
    to_process: list[dict] = []
    for e in new_emails:
        record = get_email_by_id(e["id"])
        if record:
            to_process.append(record)
    to_process.extend(recovered_emails)

    if not to_process:
        logger.info("No new or stranded emails to process.")
        return

    max_repeats = _get_max_repeat_count()

    for record in to_process:
        try:
            _process_email_record(record, max_repeats)
        except Exception as exc:
            logger.error(f"Pipeline error on email {record.get('id')}: {exc}")
            # Leave the email in 'pending'; next run retries via
            # get_pending_emails_without_draft().

    logger.info("─── Pipeline run complete ───")


# ── Send helpers ───────────────────────────────────────────────────────────────

def _do_send(
    draft_id: int,
    draft: dict,
    record: dict,
    override_subject: str | None = None,
) -> bool:
    """
    Call send_email() and update DB records on success or failure.

    Precondition: the caller has already atomically set status='sending' so no
    other worker can claim the same draft.

    FIX P2b (template subject — earlier fix, preserved):
    Uses override_subject when provided so admin-configured subject lines are
    actually delivered rather than always falling back to "Re: <original>".

    On SMTP failure the draft is moved to 'send_failed' and the email back to
    'approved' so both are visible in Pending Approvals for admin retry.
    """
    from app.email.sender import send_email

    body_to_send = draft.get("edited_body") or draft.get("draft_body") or ""
    subject = (
        override_subject.strip()
        if override_subject and override_subject.strip()
        else f"Re: {record.get('subject') or 'Your Inquiry'}"
    )

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
    insert_log(
        record["id"], "send_failed",
        f"Draft {draft_id} failed to send — check SMTP config. "
        f"Draft is in 'send_failed' state and can be retried from Pending Approvals.",
    )
    return False


def approve_and_send(draft_id: int, force_immediate: bool = False) -> bool:
    """
    Approve a draft and send it immediately or schedule it.

    FIX P2 (DB error returns False, not True):
    update_draft_if_status() returns _DB_ERROR on a real exception.  We now
    check for that sentinel explicitly and return False so the API endpoint
    raises HTTP 500 instead of silently returning success.

    FIX P1 (int() on delay_secs can raise inside critical section):
    delay_secs comes from a live DB setting.  If it contains a non-numeric
    value, bare int() would raise after the draft has been claimed as
    'sending', permanently hiding it.  _safe_int() is used instead.

    Earlier fixes (preserved):
    - Template subject passed to _do_send().
    - Atomic claim via update_draft_if_status().
    - scheduled_send_at cleared on immediate path.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── Atomic claim ──────────────────────────────────────────────────────────
    claimed = update_draft_if_status(
        draft_id,
        from_statuses=("pending", "send_failed"),
        status="sending",
        approved_at=now,
        # FIX P1 (stale-send recovery): record the exact moment this draft
        # entered 'sending' so recover_stale_sending_drafts() measures the
        # timeout from here, not from generated_at.  A draft approved minutes
        # after it was generated would otherwise be immediately eligible for
        # false recovery while SMTP is still in flight.
        sending_started_at=now,
    )

    # FIX P2: _DB_ERROR means the DB call itself failed — surface as hard error.
    if isinstance(claimed, _DbErrorType):
        logger.error(
            f"approve_and_send: DB error while claiming draft {draft_id}. "
            f"Draft was NOT updated. Returning failure so the UI shows an error."
        )
        return False

    if claimed is None:
        # Status mismatch: another worker already claimed or the draft is in a
        # terminal state.  This is idempotent — treat as success.
        draft = get_draft_by_id(draft_id)
        if not draft:
            logger.error(f"approve_and_send: draft {draft_id} not found in DB")
            return False
        logger.warning(
            f"approve_and_send: draft {draft_id} has status '{draft.get('status')}' "
            f"— skipping (already claimed or terminal state)."
        )
        return True

    # We own the claim.  Load full records.
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

    # FIX P1: use _safe_int() — delay_secs comes from a live DB value.
    raw_delay  = (tmpl.get("send_delay_seconds") if tmpl else None)
    delay_secs = _safe_int(str(raw_delay), 0, "send_delay_seconds") if raw_delay is not None else 0

    tmpl_subject: str | None = (tmpl.get("subject") or "").strip() if tmpl else None

    # ── Scheduled path ────────────────────────────────────────────────────────
    if not force_immediate and delay_secs > 0:
        send_at = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_secs)
        ).isoformat()
        update_draft(draft_id, status="approved", scheduled_send_at=send_at)
        update_email_status(record["id"], "approved")
        insert_log(
            record["id"], "email_scheduled",
            f"Draft {draft_id} scheduled for {send_at} ({delay_secs}s delay).",
        )
        logger.info(f"Draft {draft_id} scheduled to send at {send_at}")
        return True

    # ── Immediate path ────────────────────────────────────────────────────────
    if force_immediate and delay_secs > 0:
        logger.info(f"Draft {draft_id} — timer overridden, sending immediately.")
        insert_log(
            record["id"], "timer_overridden",
            f"Draft {draft_id} sent immediately despite {delay_secs}s timer.",
        )

    # Clear any stale scheduled_send_at so the scheduler cannot double-claim.
    update_draft(draft_id, scheduled_send_at=None)
    update_email_status(record["id"], "approved")

    return _do_send(draft_id, draft, record, override_subject=tmpl_subject)


def dispatch_scheduled_drafts() -> None:
    """
    Called periodically by the scheduler.  Atomically claims and sends any
    approved drafts whose scheduled_send_at has passed.
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

        query_type   = record.get("query_type") or "general"
        tmpl         = get_template_by_query_type(query_type)
        tmpl_subject = (tmpl.get("subject") or "").strip() if tmpl else None

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
