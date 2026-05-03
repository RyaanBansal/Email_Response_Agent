"""
app/email/sender.py  –  SMTP outbound email sender

Connection strategy (auto-detected from SMTP_PORT, overridable via SMTP_MODE):
  Port 465            → SMTP_SSL  (SSL from the start)
  Port 587 or 25      → SMTP + STARTTLS
  SMTP_MODE=ssl       → force SMTP_SSL  regardless of port
  SMTP_MODE=starttls  → force SMTP + STARTTLS regardless of port

Settings are resolved at call-time from app_settings DB table (editable in the
Settings UI) with fallback to .env — so no restart is needed after a change.
"""
import os
import ssl
import smtplib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from dotenv import load_dotenv
from imapclient import IMAPClient
from loguru import logger

load_dotenv()


def _cfg(key: str, default: str = "") -> str:
    """Read a config value from app_settings (DB) or fall back to env / default."""
    try:
        from app.db.models import get_setting
        val = get_setting(key)
        if val:
            return val
    except Exception:
        pass
    return os.getenv(key, default)


def _use_ssl(smtp_port: int, smtp_mode: str) -> bool:
    if smtp_mode == "ssl":
        return True
    if smtp_mode == "starttls":
        return False
    return smtp_port == 465


def _build_message(from_addr: str, to: str, subject: str, body: str) -> MIMEText:
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"]       = from_addr
    msg["To"]         = to
    msg["Subject"]    = subject
    msg["Date"]       = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid()
    return msg


def _detect_sent_folder(client: IMAPClient, override: str) -> str:
    if override:
        return override
    try:
        folders = client.list_folders()
        for flags, delimiter, name in folders:
            if b"\\Sent" in flags or "\\Sent" in str(flags):
                return name
        common = ["sent", "sent items", "sent mail", "gesendete elemente"]
        for flags, delimiter, name in folders:
            if name.lower().strip() in common:
                return name
    except Exception as exc:
        logger.warning(f"Could not detect Sent folder: {exc}")
    return "Sent"


def _append_to_sent(raw_message: bytes, imap_host: str, imap_port: int,
                    email_addr: str, email_pass: str, sent_override: str) -> None:
    try:
        with IMAPClient(imap_host, port=imap_port, ssl=True) as client:
            client.login(email_addr, email_pass)
            sent_folder = _detect_sent_folder(client, sent_override)
            now = datetime.now(timezone.utc)
            client.append(sent_folder, raw_message, flags=[b"\\Seen"], msg_time=now)
            logger.info(f"Message appended to IMAP folder: {sent_folder!r}")
    except Exception as exc:
        logger.warning(f"Could not append to Sent folder (email was still sent): {exc}")


def send_email(to: str, subject: str, body: str) -> bool:
    """
    Send a plain-text email via SMTP and append it to the IMAP Sent folder.
    Credentials and server settings are resolved live from app_settings / .env.
    Returns True on success, False on failure.
    """
    smtp_host    = _cfg("SMTP_HOST", "smtp.gmail.com")
    smtp_port    = int(_cfg("SMTP_PORT", "587"))
    smtp_mode    = _cfg("SMTP_MODE", "").lower()
    imap_host    = _cfg("IMAP_HOST", "imap.gmail.com")
    imap_port    = int(_cfg("IMAP_PORT", "993"))
    email_addr   = _cfg("EMAIL_ADDRESS")
    email_pass   = _cfg("EMAIL_PASSWORD")
    sent_override = _cfg("IMAP_SENT_FOLDER", "")

    if not email_addr or not email_pass:
        logger.warning("SMTP credentials not set. Email NOT sent (dry-run).")
        logger.info(f"[DRY-RUN] To: {to} | Subject: {subject}")
        return True

    msg       = _build_message(email_addr, to, subject, body)
    raw_bytes = msg.as_bytes()
    use_ssl   = _use_ssl(smtp_port, smtp_mode)
    mode_label = "SSL" if use_ssl else "STARTTLS"
    logger.info(f"Connecting to {smtp_host}:{smtp_port} via {mode_label}")

    try:
        ctx = ssl.create_default_context()

        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ctx, timeout=20) as server:
                server.ehlo()
                server.login(email_addr, email_pass)
                server.sendmail(email_addr, [to], raw_bytes)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as server:
                server.ehlo()
                server.starttls(context=ctx)
                server.ehlo()
                server.login(email_addr, email_pass)
                server.sendmail(email_addr, [to], raw_bytes)

        logger.success(f"Email sent to {to} | Subject: {subject}")
        _append_to_sent(raw_bytes, imap_host, imap_port, email_addr, email_pass, sent_override)
        return True

    except smtplib.SMTPAuthenticationError as exc:
        logger.error(f"SMTP auth failed for {email_addr}: {exc}")
        return False
    except smtplib.SMTPRecipientsRefused as exc:
        logger.error(f"Recipient refused: {exc.recipients}")
        return False
    except smtplib.SMTPException as exc:
        logger.error(f"SMTP protocol error sending to {to}: {exc}")
        return False
    except (TimeoutError, OSError) as exc:
        logger.error(f"Connection to {smtp_host}:{smtp_port} failed ({mode_label}): {exc}")
        return False
    except Exception as exc:
        logger.error(f"Unexpected error sending to {to}: {type(exc).__name__}: {exc}")
        return False
