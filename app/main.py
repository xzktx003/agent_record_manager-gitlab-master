"""FastAPI application entry point."""
import os
import logging
import faulthandler
import signal
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware

from app.config import get_config
from app.scheduler import start_scheduler, stop_scheduler, sync_manager
from app.enrichment import enrichment_manager
from app.work_memory import get_today_dashboard, recover_interrupted_agent_state
from app.api import auth, sessions, search, export, mcp, sync_api, work, model_providers
from app.auth import auth_repository, initialize_auth
from app.auth_middleware import authentication_middleware
from app.reliability import router as reliability_router, request_metrics_middleware
from app.tenant import LEGACY_DATA_SCOPE, tenant_scope
from app.tenant_data import ensure_tenant_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing Agent Memory Manager...")
    stack_signal_installed = False
    if hasattr(signal, "SIGUSR1"):
        try:
            faulthandler.register(signal.SIGUSR1, all_threads=True)
            stack_signal_installed = True
        except (OSError, RuntimeError, ValueError):
            logger.warning("Unable to register SIGUSR1 stack dump", exc_info=True)
    config = get_config()
    initialize_auth()
    scopes = (
        auth_repository.active_data_scopes()
        if config.auth.enabled
        else [LEGACY_DATA_SCOPE]
    )
    if config.auth.enabled and not scopes:
        logger.error(
            "Authentication is enabled but no active account exists; "
            "create the first administrator with scripts/auth_admin.py"
        )
    for scope in scopes:
        ensure_tenant_data(scope)

    # Seed the server-side last-good dashboard before startup sync begins.
    # Direct browsers can arrive while that first write pass is active; an
    # existing snapshot lets Today single-flight serve stale data to the burst
    # instead of making every request wait behind the initial SQLite writer.
    if scopes:
        try:
            with tenant_scope(scopes[0]):
                get_today_dashboard(compact=True)
        except Exception:
            logger.warning("Unable to warm Today dashboard cache", exc_info=True)

    # Kick off the initial local sync in a background thread so the HTTP
    # server can start serving immediately. The previous implementation
    # awaited sync_local() here, which could block uvicorn for minutes
    # while Claude/Codex/VSCode JSONLs were parsed.
    initial = sync_manager.submit_all_tenants("local", force=False)
    logger.info(f"Initial local sync dispatched: {initial}")

    # The intelligence worker is independent from raw synchronization.  It
    # consumes durable SQLite jobs and can be paused/restarted without losing
    # work or making conversation search unavailable.
    for scope in scopes:
        with tenant_scope(scope):
            recover_interrupted_agent_state()
    enrichment_manager.start_all(scopes)
    if config.agent.enabled and config.agent.backfill_on_start:
        for scope in scopes:
            with tenant_scope(scope):
                queued = enrichment_manager.enqueue_history(
                    days=config.agent.recent_backfill_days
                )
            logger.info(
                "Recent work-memory backfill queued for %s: %s", scope, queued
            )

    # Start scheduler if configured
    if config.sync.auto_start:
        start_scheduler()

    yield

    stop_scheduler()
    enrichment_manager.stop_all()
    if stack_signal_installed:
        try:
            faulthandler.unregister(signal.SIGUSR1)
        except (OSError, RuntimeError, ValueError):
            pass
    logger.info("Agent Memory Manager stopped.")

app = FastAPI(
    title="Agent Memory Manager",
    description="本地优先的 Agent 会话、任务、项目与工作记忆平台",
    version="2.0.0",
    lifespan=lifespan,
)

allowed_origins = list(get_config().auth.allowed_origins)
if allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
    )
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)
app.middleware("http")(request_metrics_middleware)
app.middleware("http")(authentication_middleware)

# Register API routers
app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(search.router)
app.include_router(export.router)
app.include_router(mcp.router)
app.include_router(sync_api.router)
app.include_router(work.router)
app.include_router(model_providers.router)
app.include_router(reliability_router)

# Serve static frontend
STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")


@app.get("/login", response_class=HTMLResponse)
def login_page():
    login_path = os.path.join(STATIC_DIR, "login.html")
    if os.path.exists(login_path):
        return FileResponse(login_path, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>登录页面尚未安装</h1>", status_code=503)

@app.get("/", response_class=HTMLResponse)
def root():
    index_path = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_path):
        return FileResponse(
            index_path,
            headers={"Cache-Control": "no-store"},
        )
    return HTMLResponse("<h1>Agent Memory Manager</h1><p>Frontend not found. API available at /docs</p>")

# Mount static files LAST - only serves actual files, won't catch-all
if os.path.exists(STATIC_DIR):
    app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
