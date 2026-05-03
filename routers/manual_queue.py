"""
routers/manual_queue.py  –  Manual (repeat-sender) queue

GET  /api/manual                  → list emails in manual status
POST /api/manual/{email_id}/generate → force-generate a draft for a manual email
"""
from fastapi import APIRouter, Depends, HTTPException

from app.db.models import (
    get_emails_by_status,
    update_email_status,
    update_email_query_type,
    insert_draft,
)
from app.ai.generator import process_email
from routers.auth import get_current_user

router = APIRouter(prefix="/api/manual", tags=["manual_queue"])


@router.get("")
def list_manual(_user=Depends(get_current_user)):
    return get_emails_by_status("manual")


@router.post("/{email_id}/generate")
def generate_for_manual(email_id: int, _user=Depends(get_current_user)):
    from app.db.models import get_email_by_id
    record = get_email_by_id(email_id)
    if not record:
        raise HTTPException(status_code=404, detail="Email not found")

    result = process_email(
        sender  = record["sender"],
        subject = record.get("subject") or "",
        body    = record.get("body") or "",
    )
    update_email_status(email_id, "pending")
    update_email_query_type(email_id, result["query_type"])
    insert_draft(email_id, result["draft"], result["confidence"])
    return {"detail": "Draft generated", "query_type": result["query_type"]}
