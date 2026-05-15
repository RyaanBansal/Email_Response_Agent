"""
main.py  –  FastAPI entry point for the Agentic Email System

Fixes applied (this revision)
──────────────────────────────
P2 (CORS wildcard + credentials):
  allow_origins=["*"] combined with allow_credentials=True is rejected by all
  modern browsers (CORS spec forbids it) and, even if it were accepted, would
  allow any origin to make credentialed cross-site requests.  Fixed:

  • ALLOWED_ORIGINS env var controls the list (comma-separated).
  • Default is the empty string; the app then falls back to a localhost-only
    list so development still works out of the box.
  • In production, set ALLOWED_ORIGINS to your exact frontend URL(s):
      ALLOWED_ORIGINS=https://your-app.onrender.com

  allow_credentials remains True because the session cookie must be sent with
  API requests from the frontend, but it is now paired with an explicit origin
  list rather than a wildcard.

Earlier fixes (preserved)
──────────────────────────
P6 (Render health check on authenticated endpoint):
  Added GET /healthz (unauthenticated).

P2 (Double-send under multiple processes):
  SCHEDULER_ENABLED guards the background scheduler.
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

load_dotenv()

# ── CORS origins ──────────────────────────────────────────────────────────────
# Set ALLOWED_ORIGINS to a comma-separated list of your frontend URL(s).
# Example (Render): ALLOWED_ORIGINS=https://email-agent-admin.onrender.com
# Example (local):  ALLOWED_ORIGINS=http://localhost:3000,http://localhost:5173
#
# If unset, the app defaults to localhost variants for local development only.
# The wildcard ("*") is intentionally never used because it cannot be combined
# with allow_credentials=True and would defeat cookie-based auth.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
if _raw_origins:
    _ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
else:
    # Safe local-dev default — replace with the real URL before deploying.
    _ALLOWED_ORIGINS = [
        "http://localhost:8000",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:3000",
    ]
    logger.warning(
        "ALLOWED_ORIGINS is not set.  CORS is restricted to localhost only. "
        "Set ALLOWED_ORIGINS=https://your-app-url before deploying to production."
    )

# ── Scheduler guard ───────────────────────────────────────────────────────────
_SCHEDULER_ENABLED = os.getenv("SCHEDULER_ENABLED", "true").lower() not in ("false", "0", "no")

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler

    if _SCHEDULER_ENABLED:
        from apscheduler.schedulers.background import BackgroundScheduler
        from app.orchestrator import run_pipeline, dispatch_scheduled_drafts
        from app.db.models import get_setting

        def _poll_interval() -> int:
            try:
                val = get_setting("POLL_INTERVAL_SECONDS")
                if val:
                    return int(val)
            except Exception:
                pass
            return int(os.getenv("POLL_INTERVAL_SECONDS", 60))

        interval = _poll_interval()
        _scheduler = BackgroundScheduler()
        _scheduler.add_job(run_pipeline, "interval", seconds=interval,
                           id="poll_inbox", replace_existing=True)
        _scheduler.add_job(dispatch_scheduled_drafts, "interval", seconds=60,
                           id="dispatch_scheduled", replace_existing=True)
        _scheduler.start()
        logger.info(f"Scheduler started — poll every {interval}s, dispatch every 60s.")
    else:
        logger.info("Scheduler disabled (SCHEDULER_ENABLED=false).")

    yield

    if _scheduler:
        _scheduler.shutdown(wait=False)
        logger.info("Scheduler stopped.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Email Agent Admin",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # FIX P2: explicit origin list; wildcard removed.
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Health check (unauthenticated) ────────────────────────────────────────────
@app.get("/healthz", include_in_schema=False)
def healthz():
    return JSONResponse({"status": "ok"})


# ── Routers ───────────────────────────────────────────────────────────────────

from routers.auth         import router as auth_router
from routers.dashboard    import router as dashboard_router
from routers.approvals    import router as approvals_router
from routers.manual_queue import router as manual_router
from routers.sent         import router as sent_router
from routers.templates    import router as templates_router
from routers.settings     import router as settings_router

app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(approvals_router)
app.include_router(manual_router)
app.include_router(sent_router)
app.include_router(templates_router)
app.include_router(settings_router)

# ── Static files & SPA fallback ───────────────────────────────────────────────

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str = ""):
    return FileResponse("static/index.html")
