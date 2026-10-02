import os

# Fail-closed: in team local-only mode, force-disable mem0's PostHog telemetry
# BEFORE any (transitive) mem0 import — mem0.memory.telemetry reads MEM0_TELEMETRY
# at module load time, so this must run before the app.* imports below.
if (os.environ.get("MEM0_LOCAL_ONLY") or "").strip().lower() in ("1", "true", "yes", "on"):
    os.environ["MEM0_TELEMETRY"] = "false"
    # Same reasoning for the optional Hugging Face stack (rerank, card 80110071):
    # huggingface_hub/transformers read these at import time; without them loading
    # a CrossEncoder pings huggingface.co on every startup and may download weights.
    # app.utils.reranking enforces them again in its loader (scripts, bench).
    for _hf_var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
        os.environ.setdefault(_hf_var, "1")

import datetime
import logging
from uuid import uuid4

from app.config import DEFAULT_APP_ID, USER_ID
from app.database import Base, SessionLocal, engine
from app.mcp_server import setup_mcp_server
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.team_auth import TeamAuthMiddleware
from app.models import App, User
from app.routers import (
    admin_router,
    admin_write_queue_router,
    agent_tokens_router,
    apps_router,
    auth_router,
    backup_router,
    compat_v3_router,
    config_router,
    discovery_router,
    governance_router,
    governance_project_merge_router,
    governance_schedule_router,
    groups_router,
    user_analytics_router,
    health_router,
    memories_router,
    metrics_router,
    mcp_oauth_compat_router,
    ops_metrics_router,
    provision_router,
    specs_router,
    specs_rich_router,
    stats_router,
    store_router,
)
from app.workers.spec_task_timeout_worker import spec_task_timeout_worker
from app.workers.spec_workspace_archive_worker import spec_workspace_archive_worker
from app.workers.write_worker import embedded_worker_enabled, write_worker
from app.utils.logging_context import install_structured_logging
from app.utils.tracing import configure_tracing
from app.utils.deletion_guard import deletion_guard_status, log_deletion_guard_startup
from app.utils.reranking import warmup_on_startup as warmup_reranker_on_startup
from app.utils.write_guard import log_write_guard_startup
from app.utils.write_queue_stall import write_queue_stall_watchdog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi_pagination import add_pagination

install_structured_logging()

app = FastAPI(title="ShareMem API")

# Tracing distribuído (task_08 / ADR-004): no-op se OTel ausente/desativado.
configure_tracing(service_name="openmemory-api", app=app, engine=engine)

app.add_middleware(RequestIdMiddleware)


def _cors_allow_origins() -> list[str]:
    """Explicit origins from CORS_ORIGINS (comma-separated). Never '*' with credentials."""
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if raw:
        origins = [o.strip() for o in raw.split(",") if o.strip() and o.strip() != "*"]
        if origins:
            return origins
    # Safe defaults for local UI + common LAN UI ports.
    defaults = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    ui_url = (os.getenv("NEXTAUTH_URL") or os.getenv("OPENMEMORY_UI_URL") or "").strip()
    if ui_url:
        defaults.append(ui_url.rstrip("/"))
    # Dedupe while preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for o in defaults:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Rate limit por (project, hostname) na borda (task_10 / ADR-006).
app.add_middleware(RateLimitMiddleware)
# Autenticação por equipe (task_11 / ADR-006): off|warn|enforce via AUTH_MODE.
app.add_middleware(TeamAuthMiddleware)

# Create all tables (safe under multi-worker race on concurrent DDL).
try:
    Base.metadata.create_all(bind=engine)
except Exception as exc:  # noqa: BLE001 — bootstrap only; Alembic is SoT
    # Two uvicorn workers can race CREATE TYPE/TABLE; ignore if already exists.
    msg = str(getattr(exc, "orig", exc)).lower()
    if "already exists" not in msg and "duplicate" not in msg:
        raise


# Check for USER_ID and create default user if needed
def create_default_user():
    db = SessionLocal()
    try:
        # Check if user exists
        user = db.query(User).filter(User.user_id == USER_ID).first()
        if not user:
            # Create default user
            user = User(
                id=uuid4(),
                user_id=USER_ID,
                name="Default User",
                created_at=datetime.datetime.now(datetime.UTC)
            )
            db.add(user)
            db.commit()
    finally:
        db.close()


def create_default_app():
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.user_id == USER_ID).first()
        if not user:
            return

        # Check if app already exists
        existing_app = db.query(App).filter(
            App.name == DEFAULT_APP_ID,
            App.owner_id == user.id
        ).first()

        if existing_app:
            return

        app = App(
            id=uuid4(),
            name=DEFAULT_APP_ID,
            owner_id=user.id,
            created_at=datetime.datetime.now(datetime.UTC),
            updated_at=datetime.datetime.now(datetime.UTC),
        )
        db.add(app)
        db.commit()
    finally:
        db.close()

# Create default user on startup
create_default_user()
create_default_app()

# Load Kanban column prompts cache on startup (task_06)
db_boot = SessionLocal()
try:
    from app.mcp_server import _load_kanban_prompts_cache
    _load_kanban_prompts_cache(db_boot)
finally:
    db_boot.close()

# Setup MCP server
setup_mcp_server(app)

# Include routers
app.include_router(auth_router)
app.include_router(agent_tokens_router)
app.include_router(admin_router)
app.include_router(admin_write_queue_router)
app.include_router(governance_router)
app.include_router(governance_project_merge_router)
app.include_router(governance_schedule_router)
app.include_router(groups_router)
app.include_router(specs_router)
app.include_router(specs_rich_router)
app.include_router(user_analytics_router)
app.include_router(memories_router)
app.include_router(apps_router)
app.include_router(stats_router)
app.include_router(config_router)
app.include_router(backup_router)
app.include_router(discovery_router)
app.include_router(mcp_oauth_compat_router)
app.include_router(compat_v3_router)
app.include_router(provision_router)
app.include_router(store_router)
app.include_router(health_router)
app.include_router(metrics_router)
app.include_router(ops_metrics_router)

# Add pagination support
add_pagination(app)


# Start/stop the background write worker that consumes the write queue and runs
# the LLM extraction/persistence out of band (task_06 / ADR-004). In scale
# deployments the worker runs as a separate service (RUN_EMBEDDED_WORKER=false).
@app.on_event("startup")
async def _start_write_worker():
    log_deletion_guard_startup()
    log_write_guard_startup()
    if embedded_worker_enabled():
        write_worker.start()
    # Liberação automática de tasks travadas por timeout (task_05 / ADR-007),
    # iniciada no mesmo startup do write_worker (não como serviço Docker próprio).
    spec_task_timeout_worker.start()
    # Auto-arquivamento de workspaces concluídos há mais de N dias (Tarefa
    # kanban-archive-lifecycle) — mesmo padrão de startup do timeout worker.
    spec_workspace_archive_worker.start()
    # Materializa falha na UI quando o write-worker morre/trava (heartbeat).
    write_queue_stall_watchdog.start()
    # Rerank opt-in (card 80110071): sem MEM0_RERANKER_PROVIDER e no-op. Com ele,
    # carrega o cross-encoder numa thread daemon — nunca bloqueia o startup nem
    # o caminho quente da busca (MEM0_RERANKER_WARMUP=0 adia para a 1a busca).
    try:
        warmup_reranker_on_startup()
    except Exception:  # noqa: BLE001 - rerank nunca derruba a API
        logging.getLogger(__name__).exception("rerank warmup failed to start")


@app.on_event("shutdown")
async def _stop_write_worker():
    if embedded_worker_enabled():
        await write_worker.stop()
    await spec_task_timeout_worker.stop()
    await spec_workspace_archive_worker.stop()
    await write_queue_stall_watchdog.stop()
