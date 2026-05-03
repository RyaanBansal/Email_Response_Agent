"""
routers/dashboard.py  –  Dashboard stats endpoint

GET /api/dashboard  → metrics shown on the Dashboard page
"""
from datetime import datetime
from fastapi import APIRouter, Depends

from app.db.models import (
    count_emails_received_today,
    count_drafts_by_status,
    get_emails_by_status,
    get_sent_drafts_with_times,
    get_recent_logs,
)
from routers.auth import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
def dashboard_stats(_user=Depends(get_current_user)):
    received_today = count_emails_received_today()
    pending        = count_drafts_by_status("pending")
    approved       = count_drafts_by_status("approved")
    sent           = len(get_emails_by_status("sent"))
    manual_q       = len(get_emails_by_status("manual"))

    sent_drafts = get_sent_drafts_with_times()
    avg_min = 0.0
    if sent_drafts:
        deltas = []
        for d in sent_drafts:
            try:
                gen    = datetime.fromisoformat(d["generated_at"])
                sent_t = datetime.fromisoformat(d["sent_at"])
                deltas.append((sent_t - gen).total_seconds() / 60)
            except Exception:
                pass
        avg_min = round(sum(deltas) / len(deltas), 1) if deltas else 0.0

    logs = get_recent_logs(20)
    recent_activity = [
        {
            "time":   l["created_at"][11:19] if l.get("created_at") else "",
            "action": l.get("action", ""),
            "detail": l.get("detail", ""),
        }
        for l in logs
    ]

    return {
        "received_today":  received_today,
        "pending":         pending,
        "approved":        approved,
        "sent":            sent,
        "manual_queue":    manual_q,
        "avg_response_min": avg_min,
        "recent_activity": recent_activity,
    }
