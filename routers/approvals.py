"""
routers/approvals.py  –  Pending-draft approval workflow

GET  /api/approvals              → list pending drafts with parent email + timer info
POST /api/approvals/{id}/approve → approve (& send or schedule)
POST /api/approvals/{id}/reject  → reject with optional note
POST /api/approvals/{id}/save    → save body edits without approving

Fix applied (this revision)
────────────────────────────
P2 (no draft state validation before edits/rejects):
  The /save and /reject endpoints previously applied their mutations to any
  draft_id regardless of its current status.  Direct API calls (or a browser
  tab left open after another tab had already approved the draft) could
  overwrite the edited_body of a 'sent' draft, or flip a 'sent' draft back to
  'rejected', silently corrupting the sent-email history.

  Fix: both endpoints now fetch the draft first and enforce a status allowlist
  before proceeding:
    • /save   — only allowed on 'pending' or 'send_failed' drafts.
    • /reject — only allowed on 'pending' or 'send_failed' drafts.
  Any other status returns 409 Conflict with a descriptive message.

  /approve already delegates its status guard to approve_and_send() (which
  uses the atomic update_draft_if_status() claim added in orchestrator.py),
  so no additional check is needed here for that endpoint.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import (
    get_pending_drafts, get_email_by_id, get_template_by_query_type,
    get_draft_by_id,
)
from app.orchestrator import approve_and_send, reject_draft, save_edited_draft
from routers.auth import get_current_user

router = APIRouter(prefix="/api/approvals", tags=["approvals"])

# Statuses on which edits and rejections are permitted.
_MUTABLE_STATUSES = {"pending", "send_failed"}


def _delay_badge(delay: int | None) -> str:
    if not delay or int(delay) <= 0:
        return ""
    d = int(delay)
    if d >= 86400:
        return f"{d // 86400}d timer"
    if d >= 3600:
        return f"{d // 3600}h timer"
    return f"{d // 60}m timer"


def _assert_mutable(draft_id: int) -> dict:
    """
    Fetch the draft and raise 404 / 409 if it cannot be mutated.
    Returns the draft dict on success.
    """
    draft = get_draft_by_id(draft_id)
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found.")
    status = draft.get("status", "")
    if status not in _MUTABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Draft {draft_id} is in '{status}' state and cannot be modified. "
                f"Only drafts in {sorted(_MUTABLE_STATUSES)} may be edited or rejected."
            ),
        )
    return draft


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
    The atomic status claim inside approve_and_send() prevents double-sends
    from concurrent requests; no additional pre-check is needed here.
    If force_immediate=True, bypass the template timer and send right away.
    """
    # If the admin supplied an edited body, pass it to approve_and_send via
    # save_edited_draft first — but only when the draft is still mutable.
    # _assert_mutable raises 409 for terminal states (sent, rejected, approved).
    #
    # Race note: a concurrent approve or the scheduler could claim the draft
    # between _assert_mutable and the approve_and_send() call below.  If that
    # happens, approve_and_send() returns True via its idempotent-skip path.
    # The edit written here is harmless: _do_send() re-fetches the draft just
    # before calling send_email(), so the winning claimer picks up the edit.
    # The window is narrow (two DB calls apart) and the worst case is the edit
    # being redundantly written to a row already in 'sending' — no data loss.
    if body.body:
        _assert_mutable(draft_id)
        save_edited_draft(draft_id, body.body)
    success = approve_and_send(draft_id, force_immediate=body.force_immediate)
    if not success:
        raise HTTPException(status_code=500, detail="Send failed — check SMTP config.")
    return {"detail": "ok"}


@router.post("/{draft_id}/reject")
def reject(draft_id: int, body: RejectBody, _user=Depends(get_current_user)):
    """Reject a draft.  Only pending/send_failed drafts may be rejected."""
    _assert_mutable(draft_id)   # FIX P2: guard against rejecting sent/approved drafts
    reject_draft(draft_id, body.note)
    return {"detail": "rejected"}


@router.post("/{draft_id}/save")
def save_edits(draft_id: int, body: SaveBody, _user=Depends(get_current_user)):
    """Save body edits without approving.  Only mutable drafts may be edited."""
    _assert_mutable(draft_id)   # FIX P2: guard against overwriting sent draft bodies
    save_edited_draft(draft_id, body.body)
    return {"detail": "saved"}
