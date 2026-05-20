"""
app/scheduler.py  –  APScheduler background jobs

Jobs:
  • run_pipeline          — polls IMAP inbox on POLL_INTERVAL_SECONDS cadence
  • dispatch_scheduled    — checks for timer-delayed drafts every 60 seconds
"""
import os
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from loguru import logger

load_dotenv()


def _get_poll_interval() -> int:
    """Read poll interval from app_settings (live) or fall back to env.
    Uses safe int parsing so a bad stored value never raises ValueError."""
    try:
        from app.db.models import get_setting
        val = get_setting("POLL_INTERVAL_SECONDS")
        if val:
            try:
                return int(val)
            except (ValueError, TypeError):
                logger.warning(
                    f"POLL_INTERVAL_SECONDS={val!r} in app_settings is not "
                    f"a valid integer; falling back to env/default."
                )
    except Exception:
        pass
    raw = os.getenv("POLL_INTERVAL_SECONDS", "60")
    try:
        return int(raw)
    except (ValueError, TypeError):
        logger.warning(
            f"POLL_INTERVAL_SECONDS={raw!r} env var is not a valid integer; "
            f"defaulting to 60s."
        )
        return 60


def start_scheduler():
    from app.orchestrator import run_pipeline, dispatch_scheduled_drafts

    interval = _get_poll_interval()
    scheduler = BackgroundScheduler()

    scheduler.add_job(
        run_pipeline,
        trigger="interval",
        seconds=interval,
        id="poll_inbox",
        replace_existing=True,
    )

    scheduler.add_job(
        dispatch_scheduled_drafts,
        trigger="interval",
        seconds=60,
        id="dispatch_scheduled",
        replace_existing=True,
    )

    scheduler.start()
    logger.info(f"Scheduler started — polling every {interval}s, dispatch check every 60s.")
