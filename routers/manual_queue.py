"""
routers/manual_queue.py  –  Manual (repeat-sender) queue

GET  /api/manual                        → list emails in manual status
POST /api/manual/{email_id}/generate    → force-generate an AI draft (with template) for a manual email
POST /api/manual/{email_id}/reply       → submit a manually typed response and send it

Reliability fixes applied
─────────────────────────
P1 (Manual endpoints can resend or mutate already-handled emails):
  generate_for_manual() and submit_manual_reply() previously fetched the email
  record but never verified its current status before acting.  A direct API
  call (or a stale browser tab) could:
    • generate a new pending draft for an email already marked sent/rejected, or
    • fire a second SMTP send for an email already delivered.

  Fix: both endpoints now assert the email is in 'manual' status before
  proceeding.  Any other status returns 409 Conflict so the caller knows the
  record has already been handled.

P2 (Manual reply SMTP failures disappear from retry queues):
  submit_manual_reply() previously marked the draft 'approved' and the email
  'approved' before calling send_email().  On SMTP failure it logged and
  returned 500, but left the draft in 'approved' status.  Since pending
  approvals only surfaces 'pending' and 'send_failed' drafts, and the manual
  queue only surfaces 'manual' emails, the record became permanently invisible
  to the admin with no way to retry.

  Fix: on SMTP failure, roll back the draft to 'pending' (so it reappears in
  Pending Approvals with the typed body preserved as edited_body) and the
  email back to 'manual' (so it also reappears in the Manual Queue).  The
  admin can then retry the send from either interface.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import (
    get_emails_by_status,
    get_email_by_id,
    update_email_status,
    update_email_query_type,
    insert_draft,
    insert_log,
    get_template_by_query_type,
)
from app.ai.generator import process_email
from routers.auth import get_current_user

router = APIRouter(prefix="/api/manual", tags=["manual_queue"])

# Only emails in 'manual' status may be processed by the manual-queue endpoints.
# Any other status (pending, approved, sent, rejected) returns 409 Conflict.
_MANUAL_STATUSES = {"manual"}


def _assert_manual(email_id: int) -> dict:
    """
    Fetch the email record and raise 404 / 409 if it is not in a state that
    the manual-queue endpoints are allowed to act on.

    Returns the record dict on success.

    FIX P1: Prevents generate_for_manual() and submit_manual_reply() from
    re-processing emails that have already been sent, rejected, or approved
    by a concurrent request or a stale browser tab.
    """
    record = get_email_by_id(email_id)
    if not record:
        raise HTTPException(status_code=404, detail="Email not found")
    status = record.get("status", "")
    if status not in _MANUAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Email {email_id} is in '{status}' state and cannot be processed "
                f"by the manual queue. Only emails in {sorted(_MANUAL_STATUSES)} "
                f"may be acted on here."
            ),
        )
    return record


@router.get("")
def list_manual(_user=Depends(get_current_user)):
    return get_emails_by_status("manual")


@router.post("/{email_id}/generate")
def generate_for_manual(email_id: int, _user=Depends(get_current_user)):
    """
    Force-generate an AI draft for a repeat-sender email and move it to
    pending approvals.

    Applies the same template wrapper used by the main pipeline so that the
    draft stored in the DB — and shown in Pending Approvals — is the complete
    formatted email, not just the bare AI-generated body.

    FIX P1: Asserts the email is in 'manual' status before generating a draft.
    Returns 409 if the email has already been handled.
    """
    # FIX P1: status guard — replaces bare get_email_by_id() + no check
    record = _assert_manual(email_id)

    result = process_email(
        sender  = record["sender"],
        subject = record.get("subject") or "",
        body    = record.get("body") or "",
    )

    # Apply template wrapper (identical logic to run_pipeline in orchestrator.py)
    tmpl = get_template_by_query_type(result["query_type"])
    if tmpl and "{{ai_response}}" in (tmpl.get("body") or ""):
        final_draft = tmpl["body"].replace(
            "{{customer_name}}", record["sender"].split("@")[0].capitalize()
        ).replace("{{ai_response}}", result["draft"])
    else:
        final_draft = result["draft"]

    update_email_status(email_id, "pending")
    update_email_query_type(email_id, result["query_type"])
    insert_draft(email_id, final_draft, result["confidence"])
    insert_log(email_id, "draft_generated_manual",
               f"Manual-queue AI draft generated. Type: {result['query_type']}")
    return {"detail": "Draft generated", "query_type": result["query_type"]}


class ManualReplyBody(BaseModel):
    body: str


@router.post("/{email_id}/reply")
def submit_manual_reply(email_id: int, payload: ManualReplyBody, _user=Depends(get_current_user)):
    """
    Send a fully manual reply typed by the admin.

    Workflow:
      1. Validate the email exists and is in 'manual' status (FIX P1).
      2. Insert a draft record with confidence=1.0 (human-written).
      3. Mark the draft/email 'approved' then attempt SMTP send.
      4a. On success: mark both 'sent'.
      4b. On failure: roll back draft to 'pending' and email to 'manual' so
          the admin can retry from Pending Approvals or the Manual Queue.

    FIX P1: Returns 409 if the email is not in 'manual' status, preventing
    double-sends from stale tabs or direct API calls.

    FIX P2: Previously a failed send left the draft in 'approved' state which
    is not surfaced by any UI queue, making it impossible to retry without
    direct DB access.
    """
    # FIX P1: status guard — replaces bare get_email_by_id() + no check
    record = _assert_manual(email_id)

    if not payload.body or not payload.body.strip():
        raise HTTPException(status_code=400, detail="Reply body cannot be empty.")

    from app.db.models import update_draft
    from app.email.sender import send_email
    from datetime import datetime, timezone

    # Insert a draft record so we have an audit trail.
    # Store the body as edited_body so it survives rollback and is shown
    # pre-filled if the admin retries from Pending Approvals.
    draft = insert_draft(email_id, payload.body.strip(), confidence=1.0)
    if not draft:
        raise HTTPException(status_code=500, detail="Failed to create draft record.")

    draft_id = draft["id"]
    subject  = f"Re: {record.get('subject') or 'Your Inquiry'}"
    now      = datetime.now(timezone.utc).isoformat()

    # Claim the draft as 'sending' (not 'approved') so that:
    #   1. The scheduled dispatcher cannot pick it up.
    #   2. recover_stale_sending_drafts() can rescue it if the process crashes
    #      between this point and the success/failure status update below.
    # sending_started_at is stamped for the same reason it is in approve_and_send
    # and get_and_claim_scheduled_drafts — so stale-send recovery measures from
    # the real claim time, not from generated_at.
    update_draft(draft_id, status="sending", approved_at=now, sending_started_at=now)
    update_email_status(email_id, "approved")

    sent = send_email(to=record["sender"], subject=subject, body=payload.body.strip())

    if sent:
        update_draft(draft_id, status="sent", sent_at=now)
        update_email_status(email_id, "sent")
        insert_log(email_id, "manual_reply_sent",
                   f"Manual reply sent by admin. Draft id: {draft_id}")
        return {"detail": "Reply sent successfully."}

    # FIX P2: SMTP failed — roll back so the admin can retry.
    #
    # Draft  → 'pending'  : reappears in Pending Approvals with body preserved.
    # Email  → 'manual'   : reappears in Manual Queue.
    #
    # We also store the typed body as edited_body so it's pre-filled when the
    # admin opens the draft in Pending Approvals, avoiding the need to retype.
    update_draft(draft_id, status="pending", edited_body=payload.body.strip())
    update_email_status(email_id, "manual")
    insert_log(email_id, "manual_reply_failed",
               f"Manual reply SMTP failure. Draft id: {draft_id} rolled back to "
               f"pending for retry. Check SMTP config.")

    raise HTTPException(
        status_code=500,
        detail=(
            "SMTP send failed — your reply has been saved as a draft in "
            "Pending Approvals so you can retry it. Check Settings → SMTP config."
        ),
    )
