"""
app/email/poller.py  –  IMAP inbox poller

Settings (IMAP_HOST, IMAP_PORT, EMAIL_ADDRESS, EMAIL_PASSWORD, MAX_REPEAT_COUNT)
are resolved live from app_settings DB table with .env fallback so they can be
updated from the Settings UI without restarting the app.

Fix applied (this revision)
────────────────────────────
P1 (int() on live settings can raise in the poll path):
  IMAP_PORT and MAX_REPEAT_COUNT were parsed with bare int().  A non-numeric
  value in app_settings (e.g. "993x") raises ValueError and aborts poll_inbox()
  before any emails are processed.  _safe_int() is used instead: it falls back
  to the supplied default and logs a warning so the misconfiguration is
  immediately visible without crashing the pipeline.
"""
import email
import os
import re
from datetime import datetime, timezone
from email.header import decode_header

from dotenv import load_dotenv
from imapclient import IMAPClient
from loguru import logger

from app.db.models import get_email_by_uid, count_emails_by_sender, insert_email, insert_log

load_dotenv()


def _cfg(key: str, default: str = "") -> str:
    try:
        from app.db.models import get_setting
        val = get_setting(key)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key, default)


def _safe_int(value: str, default: int, label: str) -> int:
    """
    Parse value as int, returning default on failure.

    FIX P1: Replaces bare int() on live-settings strings so a bad stored
    value never raises ValueError inside poll_inbox().
    """
    try:
        return int(value)
    except (ValueError, TypeError):
        logger.warning(
            f"poll_inbox: {label}={value!r} is not a valid integer; "
            f"using default {default}."
        )
        return default


def _decode_str(value, charset="utf-8") -> str:
    if isinstance(value, bytes):
        return value.decode(charset or "utf-8", errors="replace")
    return value or ""


def _extract_plain_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            cd = str(part.get("Content-Disposition", ""))
            if ct == "text/plain" and "attachment" not in cd:
                return _decode_str(part.get_payload(decode=True), part.get_content_charset())
    else:
        return _decode_str(msg.get_payload(decode=True), msg.get_content_charset())
    return ""


def _clean_body(raw: str) -> str:
    raw = re.sub(r"\n>.*", "", raw)
    raw = re.sub(r"On .* wrote:.*", "", raw, flags=re.DOTALL)
    raw = re.sub(r"\s{3,}", "\n\n", raw)
    return raw.strip()


def _parse_sender(raw_from: str) -> str:
    match = re.search(r"<(.+?)>", raw_from)
    return match.group(1).lower() if match else raw_from.strip().lower()


def poll_inbox() -> list[dict]:
    imap_host  = _cfg("IMAP_HOST", "imap.gmail.com")
    imap_port  = _safe_int(_cfg("IMAP_PORT", "993"), 993, "IMAP_PORT")    # FIX P1
    email_addr = _cfg("EMAIL_ADDRESS")
    email_pass = _cfg("EMAIL_PASSWORD")
    max_repeat = _safe_int(_cfg("MAX_REPEAT_COUNT", "3"), 3, "MAX_REPEAT_COUNT")  # FIX P1

    if not email_addr or not email_pass:
        logger.warning("IMAP credentials not set. Skipping poll.")
        return []

    new_emails = []

    try:
        with IMAPClient(imap_host, port=imap_port, ssl=True) as client:
            client.login(email_addr, email_pass)
            client.select_folder("INBOX")
            uids = client.search(["UNSEEN"])
            logger.info(f"Found {len(uids)} unseen email(s).")

            if not uids:
                return []

            messages = client.fetch(uids, ["RFC822"])

            for uid, data in messages.items():
                uid_str = str(uid)
                if get_email_by_uid(uid_str):
                    continue

                msg     = email.message_from_bytes(data[b"RFC822"])
                decoded = decode_header(msg.get("Subject", "(No Subject)"))
                subject = "".join(_decode_str(t, enc) for t, enc in decoded)
                sender  = _parse_sender(msg.get("From", ""))
                body    = _clean_body(_extract_plain_text(msg))

                sender_count = count_emails_by_sender(sender) + 1
                is_repeat    = sender_count > max_repeat

                record = insert_email(
                    uid          = uid_str,
                    sender       = sender,
                    subject      = subject,
                    body         = body,
                    is_repeat    = is_repeat,
                    sender_count = sender_count,
                )

                if record:
                    insert_log(record["id"], "email_received",
                               f"From: {sender} | Subject: {subject} | Repeat: {is_repeat}")
                    new_emails.append({
                        "id":        record["id"],
                        "sender":    sender,
                        "subject":   subject,
                        "body":      body,
                        "is_repeat": is_repeat,
                        "status":    record["status"],
                    })
                    logger.success(f"Saved email {uid_str} from {sender}")

    except Exception as exc:
        logger.error(f"IMAP poll error: {exc}")

    return new_emails
