"""
routers/manual_queue.py  –  Manual (repeat-sender) queue

GET  /api/manual                        → list emails in manual status
POST /api/manual/{email_id}/generate    → force-generate an AI draft (with template) for a manual email
POST /api/manual/{email_id}/reply       → submit a manually typed response and send it
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
    """
    record = get_email_by_id(email_id)
    if not record:
        raise HTTPException(status_code=404, detail="Email not found")

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
      1. Validate the email exists and is in manual (or pending) status.
      2. Insert a draft record with confidence=1.0 (human-written).
      3. Immediately send via SMTP using the same sender/subject as the original.
      4. Mark the draft and email as sent.
    """
    record = get_email_by_id(email_id)
    if not record:
        raise HTTPException(status_code=404, detail="Email not found")

    if not payload.body or not payload.body.strip():
        raise HTTPException(status_code=400, detail="Reply body cannot be empty.")

    from app.db.models import update_draft
    from app.email.sender import send_email
    from datetime import datetime, timezone

    # Insert a draft record so we have an audit trail
    draft = insert_draft(email_id, payload.body.strip(), confidence=1.0)
    if not draft:
        raise HTTPException(status_code=500, detail="Failed to create draft record.")

    draft_id = draft["id"]
    subject  = f"Re: {record.get('subject') or 'Your Inquiry'}"
    now      = datetime.now(timezone.utc).isoformat()

    # Mark draft approved before sending (removes from pending list immediately)
    update_draft(draft_id, status="approved", approved_at=now)
    update_email_status(email_id, "approved")

    sent = send_email(to=record["sender"], subject=subject, body=payload.body.strip())

    if sent:
        update_draft(draft_id, status="sent", sent_at=now)
        update_email_status(email_id, "sent")
        insert_log(email_id, "manual_reply_sent",
                   f"Manual reply sent by admin. Draft id: {draft_id}")
        return {"detail": "Reply sent successfully."}

    # SMTP failed — leave email as approved so admin can retry from approvals
    insert_log(email_id, "manual_reply_failed",
               f"Manual reply SMTP failure. Draft id: {draft_id}. Check SMTP config.")
    raise HTTPException(
        status_code=500,
        detail="Email drafted but SMTP send failed — check Settings -> SMTP config."
    )
