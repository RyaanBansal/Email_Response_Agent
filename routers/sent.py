"""
routers/sent.py  –  Sent emails history

GET /api/sent  → list sent emails with their sent draft bodies
"""
from fastapi import APIRouter, Depends

from app.db.models import get_emails_by_status, get_draft_for_email
from routers.auth import get_current_user

router = APIRouter(prefix="/api/sent", tags=["sent"])


@router.get("")
def list_sent(_user=Depends(get_current_user)):
    sent_emails = get_emails_by_status("sent")
    result = []
    for em in sent_emails:
        draft = get_draft_for_email(em["id"], status="sent")
        result.append({
            "email": em,
            "sent_body": (
                (draft.get("edited_body") or draft.get("draft_body") or "")
                if draft else ""
            ),
            "sent_at": draft.get("sent_at") if draft else None,
        })
    return result
