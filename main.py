"""
main.py  –  FastAPI entry point for the Agentic Email System

Fixes applied (this revision)
──────────────────────────────
P2 (Unknown /api/ GET routes return the SPA with 200):
  The catch-all route previously matched every unmatched GET path, including
  paths under /api/.  A typo like GET /api/apporvals would silently return the
  HTML page with status 200, making misrouted API calls impossible to detect
  from the response alone and breaking clients that check for JSON.

  Fix: the catch-all handler now returns a 404 JSON response for any path that
  starts with "api/" (the leading slash is stripped by FastAPI's path
  parameter).  Only genuine frontend navigation paths (no "api/" prefix) are
  served the SPA.  This keeps SPA deep-linking intact (e.g. /dashboard) while
  surfacing real 404s for unknown API routes.

Earlier fixes (preserved)
──────────────────────────
P2 (CORS wildcard + credentials):
  allow_origins=["*"] combined with allow_credentials=True is rejected by all
  modern browsers (CORS spec forbids it).  Fixed: ALLOWED_ORIGINS env var
  controls the list; defaults to localhost-only for development.

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
_raw_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
if _raw_origins:
    _ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
else:
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
            # Safe parsing: a bad stored value (e.g. "60x") must not raise
            # ValueError here and crash the lifespan context at startup.
            # The same fix was applied in poller.py, sender.py, and
            # orchestrator.py; this completes the set.
            try:
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
def serve_root():
    return FileResponse("static/index.html")


@app.get("/{full_path:path}", include_in_schema=False)
def serve_spa(full_path: str = ""):
    # FIX P2a: Do NOT serve the SPA for unmatched /api/ paths.
    #
    # FastAPI strips the leading slash before populating full_path, so a
    # request for /api/apporvals arrives here as full_path="api/apporvals".
    # Returning a 200 HTML page for such paths silently swallowed typos and
    # made missing API routes impossible to detect from the response.
    #
    # Return a proper 404 JSON response instead so clients (and developers)
    # get an unambiguous signal that the endpoint does not exist.
    if full_path.startswith("api/") or full_path == "api":
        return JSONResponse(
            status_code=404,
            content={"detail": f"API route not found: /{full_path}"},
        )

    # All other paths are legitimate SPA deep-links (e.g. /dashboard,
    # /settings).  Serve the shell and let the frontend router handle them.
    return FileResponse("static/index.html")
