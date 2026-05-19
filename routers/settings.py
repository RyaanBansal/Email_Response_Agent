"""
routers/settings.py  –  App settings (live DB config)

GET  /api/settings            → return all settings as a flat dict
POST /api/settings            → bulk-save settings dict
POST /api/settings/test-smtp  → test SMTP connection with current values
POST /api/settings/run-pipeline → manually trigger the email pipeline

Fix applied (this revision)
────────────────────────────
P1 (Invalid integer settings stored, crash later in critical sections):
  POST /api/settings previously accepted any string without validation.
  Integer-typed settings (IMAP_PORT, SMTP_PORT, POLL_INTERVAL_SECONDS,
  MAX_REPEAT_COUNT) are consumed with int() in hot paths including send_email(),
  poll_inbox(), and approve_and_send().  A bad value (e.g. "587x", "abc")
  causes ValueError; in the worst case that happens after a draft has been
  claimed as 'sending', permanently hiding it.

  Fix: save_settings() runs a validation pass over all integer-typed keys
  before writing anything.  Invalid values produce a 422 with a clear message
  listing every offending key; no values are persisted until all pass.

  Validation rules match the constraints already documented in render.yaml
  and enforced by the UI number inputs:
    IMAP_PORT              1–65535
    SMTP_PORT              1–65535
    POLL_INTERVAL_SECONDS  10–86400
    MAX_REPEAT_COUNT       ≥ 1

  Empty strings are skipped (treated as "leave unchanged") so partial saves
  that intentionally omit a key continue to work.
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

# Integer-typed settings and their allowed ranges: key → (min, max | None).
# None for max means unbounded above.
_INT_SETTINGS: dict[str, tuple[int, int | None]] = {
    "IMAP_PORT":             (1, 65535),
    "SMTP_PORT":             (1, 65535),
    "POLL_INTERVAL_SECONDS": (10, 86400),
    "MAX_REPEAT_COUNT":      (1, None),
}


def _sval(key: str, default: str = "") -> str:
    val = get_setting(key)
    if val:
        return val
    return os.getenv(key, default)


@router.get("")
def get_settings(_user=Depends(get_current_user)):
    rows = get_all_settings()
    db_dict = {r["key"]: r["value"] for r in rows}
    result = {}
    for key in _SETTING_KEYS:
        result[key] = db_dict.get(key) or os.getenv(key, "")
    result["EMAIL_PASSWORD"] = "••••••••" if result.get("EMAIL_PASSWORD") else ""
    return result


class SettingsPayload(BaseModel):
    settings: dict[str, str]


@router.post("")
def save_settings(payload: SettingsPayload, _user=Depends(get_current_user)):
    """
    Validate then persist settings.

    FIX P1: All integer-typed settings are validated before any DB writes.
    If any value is invalid the entire request is rejected (422) and nothing
    is persisted, so the DB is never left in a partially-updated state.
    """
    # ── Validation pass — no writes until all checks pass ────────────────────
    errors: list[str] = []

    for key, (min_val, max_val) in _INT_SETTINGS.items():
        raw = payload.settings.get(key)
        if raw is None:
            continue            # key absent from payload — not being changed
        raw = raw.strip()
        if raw == "":
            continue            # empty → leave existing value unchanged

        try:
            parsed = int(raw)
        except (ValueError, TypeError):
            errors.append(f"{key}: '{raw}' is not a valid integer.")
            continue

        if parsed < min_val:
            errors.append(
                f"{key}: {parsed} is below the minimum allowed value ({min_val})."
            )
        if max_val is not None and parsed > max_val:
            errors.append(
                f"{key}: {parsed} exceeds the maximum allowed value ({max_val})."
            )

    if errors:
        raise HTTPException(
            status_code=422,
            detail=(
                "Invalid settings — no changes were saved:\n"
                + "\n".join(f"  • {e}" for e in errors)
            ),
        )

    # ── Write pass — all values have passed validation ────────────────────────
    for key, val in payload.settings.items():
        if key not in _SETTING_KEYS:
            continue
        # Never overwrite a real password with the masked placeholder.
        if key == "EMAIL_PASSWORD" and val == "••••••••":
            continue
        # Don't write an empty string for integer fields (skip, not overwrite).
        if key in _INT_SETTINGS and val.strip() == "":
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
