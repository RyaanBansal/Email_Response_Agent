"""
routers/approvals.py  –  Pending-draft approval workflow

GET  /api/approvals              → list pending drafts with parent email + timer info
POST /api/approvals/{id}/approve → approve (& send or schedule)
POST /api/approvals/{id}/reject  → reject with optional note
POST /api/approvals/{id}/save    → save body edits without approving
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import get_pending_drafts, get_email_by_id, get_template_by_query_type
from app.orchestrator import approve_and_send, reject_draft, save_edited_draft
from routers.auth import get_current_user

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


def _delay_badge(delay: int | None) -> str:
    if not delay or int(delay) <= 0:
        return ""
    d = int(delay)
    if d >= 86400:
        return f"{d // 86400}d timer"
    if d >= 3600:
        return f"{d // 3600}h timer"
    return f"{d // 60}m timer"


@router.get("")
def list_pending(_user=Depends(get_current_user)):
    drafts = get_pending_drafts()
    result = []
    for draft in drafts:
        email_rec = get_email_by_id(draft["email_id"])
        if not email_rec:
            continue
        qtype  = email_rec.get("query_type") or "unknown"
        tmpl   = get_template_by_query_type(qtype)
        delay  = tmpl.get("send_delay_seconds") if tmpl else None
        result.append({
            "draft":       draft,
            "email":       email_rec,
            "delay_badge": _delay_badge(delay),
        })
    return result


class SaveBody(BaseModel):
    body: str
    force_immediate: bool = False


class RejectBody(BaseModel):
    note: str = ""


@router.post("/{draft_id}/approve")
def approve(draft_id: int, body: SaveBody, _user=Depends(get_current_user)):
    """Save edits (if any) then approve and send/schedule.
    If force_immediate=True, bypass the template timer and send right away.
    """
    if body.body:
        save_edited_draft(draft_id, body.body)
    success = approve_and_send(draft_id, force_immediate=body.force_immediate)
    if not success:
        raise HTTPException(status_code=500, detail="Send failed — check SMTP config.")
    return {"detail": "ok"}


@router.post("/{draft_id}/reject")
def reject(draft_id: int, body: RejectBody, _user=Depends(get_current_user)):
    reject_draft(draft_id, body.note)
    return {"detail": "rejected"}


@router.post("/{draft_id}/save")
def save_edits(draft_id: int, body: SaveBody, _user=Depends(get_current_user)):
    save_edited_draft(draft_id, body.body)
    return {"detail": "saved"}
