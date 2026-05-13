"""
main.py  –  FastAPI entry point for the Agentic Email System

Fixes applied
─────────────
P2 (Render health check on authenticated endpoint):
  Added GET /healthz that returns 200 without requiring auth.
  render.yaml healthCheckPath is updated to /healthz.

P2 (Double-send under multiple processes):
  The scheduler is now guarded by an environment variable
  SCHEDULER_ENABLED (default "true").  When deploying with multiple
  uvicorn workers (--workers N) set SCHEDULER_ENABLED=false on all but
  one process, or run the scheduler as a separate Render service.
  The atomic claim in get_and_claim_scheduled_drafts() provides a
  second layer of protection within a single-process deployment.

Start with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
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

# ── Scheduler guard ───────────────────────────────────────────────────────────
# Set SCHEDULER_ENABLED=false in any replica that should NOT run background jobs
# to prevent duplicate scheduled sends when multiple uvicorn workers are used.
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

    yield  # app runs here

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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health check (unauthenticated) ────────────────────────────────────────────
# FIX P6: Render's healthCheckPath must not require auth cookies.
# /api/auth/me returned 401 to the health checker, causing false restarts.

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
