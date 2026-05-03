"""
routers/settings.py  –  App settings (live DB config)

GET  /api/settings            → return all settings as a flat dict
POST /api/settings            → bulk-save settings dict
POST /api/settings/test-smtp  → test SMTP connection with current values
POST /api/settings/run-pipeline → manually trigger the email pipeline
"""
import os
import smtplib
import ssl as _ssl
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db.models import get_all_settings, set_setting, get_setting
from routers.auth import get_current_user

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTING_KEYS = [
    "EMAIL_ADDRESS", "EMAIL_PASSWORD",
    "IMAP_HOST", "IMAP_PORT", "IMAP_SENT_FOLDER",
    "SMTP_HOST", "SMTP_PORT", "SMTP_MODE",
    "POLL_INTERVAL_SECONDS", "MAX_REPEAT_COUNT", "GEMINI_MODEL",
]


def _sval(key: str, default: str = "") -> str:
    val = get_setting(key)
    if val:
        return val
    return os.getenv(key, default)


@router.get("")
def get_settings(_user=Depends(get_current_user)):
    rows = get_all_settings()
    db_dict = {r["key"]: r["value"] for r in rows}
    # Merge with env defaults for display; mask password
    result = {}
    for key in _SETTING_KEYS:
        result[key] = db_dict.get(key) or os.getenv(key, "")
    result["EMAIL_PASSWORD"] = "••••••••" if result.get("EMAIL_PASSWORD") else ""
    return result


class SettingsPayload(BaseModel):
    settings: dict[str, str]


@router.post("")
def save_settings(payload: SettingsPayload, _user=Depends(get_current_user)):
    for key, val in payload.settings.items():
        if key in _SETTING_KEYS:
            # Don't overwrite the masked password placeholder
            if key == "EMAIL_PASSWORD" and val == "••••••••":
                continue
            set_setting(key, val)
    return {"detail": "Settings saved"}


class SmtpTestPayload(BaseModel):
    host: str = ""
    port: int = 587
    mode: str = ""
    address: str = ""
    password: str = ""


@router.post("/test-smtp")
def test_smtp(body: SmtpTestPayload, _user=Depends(get_current_user)):
    host  = body.host or _sval("SMTP_HOST", "smtp.gmail.com")
    port  = body.port or int(_sval("SMTP_PORT", "587"))
    mode  = (body.mode or _sval("SMTP_MODE", "")).lower()
    addr  = body.address or _sval("EMAIL_ADDRESS")
    pw    = body.password if body.password and body.password != "••••••••" else _sval("EMAIL_PASSWORD")

    use_ssl = mode == "ssl" or (not mode and port == 465)
    ctx = _ssl.create_default_context()
    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=10) as srv:
                srv.ehlo()
                srv.login(addr, pw)
        else:
            with smtplib.SMTP(host, port, timeout=10) as srv:
                srv.ehlo(); srv.starttls(context=ctx); srv.ehlo()
                srv.login(addr, pw)
        return {"detail": f"SMTP connection to {host}:{port} successful!"}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"SMTP test failed: {exc}")


@router.post("/run-pipeline")
def run_pipeline_endpoint(_user=Depends(get_current_user)):
    from app.orchestrator import run_pipeline
    run_pipeline()
    return {"detail": "Pipeline complete"}
