"""
main.py  –  FastAPI entry point for the Agentic Email System

Replaces admin_app.py (Streamlit).

Start with:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload
"""
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

load_dotenv()

# ── Lifespan: start APScheduler on boot, shut it down cleanly ────────────────

_scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _scheduler
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

    yield  # app runs here

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
    # All non-API paths serve the single-page app shell
    return FileResponse("static/index.html")
