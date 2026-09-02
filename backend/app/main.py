"""FastAPI application factory."""

import logging

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.deployer.container  # noqa: F401 — registers the container (Claude SDK) method
import app.deployer.harness  # noqa: F401 — registers the harness deploy method
import app.deployer.zip_runtime  # noqa: F401 — registers zip_runtime + studio methods
from app.core.config import get_settings
from app.core.db import init_db
from app.core.errors import register_error_handlers
from app.core.route_policy import enforce_route_policy
from app.deployer.pipeline import resume_pending_jobs
from app.evaluation.online_routers import router as online_eval_router
from app.evaluation.routers import router as evaluation_router
from app.evaluation.service import resume_interrupted_runs
from app.optimization.canary_routers import router as runtime_canaries_router
from app.optimization.canary_service import (
    clear_stale_running_actions as clear_stale_canary_actions,
)
from app.optimization.routers import router as experiments_router
from app.optimization.service import clear_stale_running_actions
from app.routers.agent_skills import router as agent_skills_router
from app.routers.agents import router as agents_router
from app.routers.apikeys import router as apikeys_router
from app.routers.auth import OPEN_CONSOLE_REMEDY, auth_middleware
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import router as auth_router
from app.routers.chat import router as chat_router
from app.routers.codegen import router as codegen_router
from app.routers.conversations import router as conversations_router
from app.routers.execution import router as execution_router
from app.routers.governance import router as governance_router
from app.routers.knowledge import router as knowledge_router
from app.routers.memory import router as memory_router
from app.routers.memory_resources import router as memory_resources_router
from app.routers.observability import router as observability_router
from app.routers.overview import router as overview_router
from app.routers.public_api import router as public_router
from app.routers.registry import router as registry_router
from app.routers.tools import router as tools_router
from app.routers.users import router as users_router
from app.routers.workspaces import router as workspaces_router
from app.services import local_exec
from app.services.governance import reconcile_policy_changes
from app.services.model_prices import start_auto_refresh
from app.skill_lab import task_assets
from app.skill_lab.jobs import sweep_interrupted_jobs as sweep_skill_lab_jobs
from app.skill_lab.routers import router as skill_lab_router

API_DESCRIPTION = """AgentCore Launchpad — enterprise agent platform.

The `/v1` endpoints are the **public integration surface** (X-Api-Key auth,
sync + SSE streaming invoke). `/api/*` endpoints back the console UI and share
the same invoke chain, so behavior is identical across both entrances.
"""


async def hsts(request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
    )
    return response


async def health() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": settings.version,
        "region": settings.region,
    }


def _assert_production_is_authenticated(settings) -> None:
    """Refuse to build an unauthenticated app in production mode.

    The per-request guard in `auth_middleware` is the real control (it is the only
    place the caller's address is known). This assertion exists so a misconfigured
    production launch fails at boot with one clear message instead of serving a
    console that 403s every request.
    """
    if settings.run_mode != "prod" or settings.allow_open_console:
        return
    if not auth_enabled(settings):
        raise RuntimeError(
            "Refusing to start in production mode without console authentication. "
            + OPEN_CONSOLE_REMEDY
        )


def create_app(resume_jobs: bool = False) -> FastAPI:
    settings = get_settings()
    _assert_production_is_authenticated(settings)
    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.version,
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        # Console authorization for every /api route lives in one auditable,
        # default-deny table instead of per-route Depends(require_admin).
        dependencies=[Depends(enforce_route_policy)],
    )

    if settings.run_mode == "prod":
        # Only in production: an HSTS header served over a dev HTTP origin pins
        # localhost to HTTPS in the developer's browser, and that cache is sticky
        # and awkward to clear.
        app.middleware("http")(hsts)

    # Register before auth so Starlette's reverse middleware stack keeps auth
    # outermost while this exact-route gate still runs before multipart parsing.
    app.middleware("http")(task_assets.task_asset_body_limit_middleware)
    app.middleware("http")(auth_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    init_db()
    app.include_router(auth_router)
    app.include_router(overview_router)
    app.include_router(agents_router)
    app.include_router(agent_skills_router)  # attach-without-registering skill sources
    app.include_router(tools_router)
    app.include_router(registry_router)
    app.include_router(knowledge_router)  # managed knowledge bases + retrieval playground
    app.include_router(chat_router)
    app.include_router(memory_router)  # read-only short-/long-term memory console
    app.include_router(memory_resources_router)  # memory resource lifecycle (create/delete)
    app.include_router(execution_router)  # studio local-debug: run un-deployed code
    app.include_router(conversations_router)  # studio local-debug: multi-turn chat
    app.include_router(codegen_router)  # studio local-debug: AI fix (diagnose + repair)
    app.include_router(governance_router)
    app.include_router(observability_router)
    app.include_router(evaluation_router)
    app.include_router(online_eval_router)
    app.include_router(skill_lab_router)
    app.include_router(experiments_router)
    app.include_router(runtime_canaries_router)
    app.include_router(users_router)  # admin-only console account management
    app.include_router(workspaces_router)  # environments + the request-boundary grants
    app.include_router(apikeys_router)
    app.include_router(public_router)
    if resume_jobs:
        resumed = resume_pending_jobs()
        if resumed:
            logging.getLogger("launchpad").info(
                "resumed %d interrupted deploy/bootstrap job(s)", len(resumed)
            )
        resumed_evals = resume_interrupted_runs()
        if resumed_evals:
            logging.getLogger("launchpad").info(
                "reconciling %d interrupted eval run(s): %s",
                len(resumed_evals), ", ".join(resumed_evals),
            )
        stale_actions = clear_stale_running_actions()
        if stale_actions:
            logging.getLogger("launchpad").info(
                "cleared stale experiment action(s) on: %s",
                ", ".join(stale_actions),
            )
        # skill-lab jobs are child subprocesses — a restart killed them; fail
        # the rows honestly (resume_pending_jobs cannot see this table)
        sweep_skill_lab_jobs()
        stale_canaries = clear_stale_canary_actions()
        if stale_canaries:
            logging.getLogger("launchpad").info(
                "cleared stale Runtime Canary action(s) on: %s",
                ", ".join(stale_canaries),
            )
        reconciled_policy_changes = reconcile_policy_changes()
        if reconciled_policy_changes:
            logging.getLogger("launchpad").info(
                "reconciled %d interrupted Policy operation(s)",
                len(reconciled_policy_changes),
            )
        reaped = local_exec.reap_orphan_containers()
        if reaped:
            logging.getLogger("launchpad").info(
                "reaped %d orphaned studio exec container(s)", reaped
            )
        start_auto_refresh()  # periodic model-price refresh (real server only)

    app.add_api_route("/api/health", health, methods=["GET"])

    return app


app = create_app(resume_jobs=True)
