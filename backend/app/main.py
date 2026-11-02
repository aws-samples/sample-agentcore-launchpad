"""FastAPI application factory."""

import logging
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.deployer.byoc  # noqa: F401 — registers the byoc (bring your own code) method
import app.deployer.container  # noqa: F401 — registers the container (Claude SDK) method
import app.deployer.harness  # noqa: F401 — registers the harness deploy method
import app.deployer.zip_runtime  # noqa: F401 — registers zip_runtime + studio methods
from app.assistant.evaluation_assets import resume_operations as resume_evaluation_assets
from app.assistant.service import clear_stale_turn_claims
from app.coding import watcher as coding_watcher
from app.coding.install import sweep_interrupted_jobs as sweep_coding_runtime_jobs
from app.coding.routers import router as coding_router
from app.core.billing_policy import enforce_budget
from app.core.config import get_settings
from app.core.db import init_db
from app.core.errors import register_error_handlers
from app.core.route_policy import bind_request_project, enforce_route_policy
from app.deployer.pipeline import resume_pending_jobs
from app.evaluation.online_routers import router as online_eval_router
from app.evaluation.pipeline_routers import router as eval_pipelines_router
from app.evaluation.routers import router as evaluation_router
from app.evaluation.service import resume_interrupted_runs
from app.optimization.canary_routers import router as runtime_canaries_router
from app.optimization.canary_service import (
    clear_stale_running_actions as clear_stale_canary_actions,
)
from app.optimization.routers import router as experiments_router
from app.optimization.service import clear_stale_running_actions
from app.routers.agent_project_settings import router as agent_project_settings_router
from app.routers.agent_skills import router as agent_skills_router
from app.routers.agent_story import router as agent_story_router
from app.routers.agent_telemetry import router as agent_telemetry_router
from app.routers.agents import router as agents_router
from app.routers.alerts import router as alerts_router
from app.routers.announcements import router as announcements_router
from app.routers.answer_rules import router as answer_rules_router
from app.routers.apikeys import router as apikeys_router
from app.routers.assistant import AssistantBodyCap
from app.routers.assistant import router as assistant_router
from app.routers.auth import OPEN_CONSOLE_REMEDY, auth_middleware
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import router as auth_router
from app.routers.budgets import router as budgets_router
from app.routers.channels import console_router as channel_links_router
from app.routers.channels import router as channels_router
from app.routers.chat import router as chat_router
from app.routers.codegen import router as codegen_router
from app.routers.context_layer import router as context_layer_router
from app.routers.conversations import router as conversations_router
from app.routers.cost_tags import router as cost_tags_router
from app.routers.custom_tools import router as custom_tools_router
from app.routers.departments import router as departments_router
from app.routers.dlc import router as dlc_router
from app.routers.environments import router as environments_router
from app.routers.execution import router as execution_router
from app.routers.feedback import router as feedback_router
from app.routers.fleet import router as fleet_router
from app.routers.gateway_lambda import router as gateway_lambda_router
from app.routers.governance import router as governance_router
from app.routers.identity import router as identity_router
from app.routers.invoke_access import router as invoke_access_router
from app.routers.issues import router as issues_router
from app.routers.knowledge import router as knowledge_router
from app.routers.mcp_reviews import router as mcp_reviews_router
from app.routers.memory import router as memory_router
from app.routers.memory_resources import router as memory_resources_router
from app.routers.model_defaults import router as model_defaults_router
from app.routers.model_defaults import workspace_router as model_catalog_router
from app.routers.nav_settings import router as nav_settings_router
from app.routers.network_profiles import router as network_profiles_router
from app.routers.observability import router as observability_router
from app.routers.overview import router as overview_router
from app.routers.project_audit import router as project_audit_router
from app.routers.project_isolation import router as project_isolation_router
from app.routers.project_quotas import router as project_quotas_router
from app.routers.projects import me_router as my_access_router
from app.routers.projects import router as projects_router
from app.routers.projects import templates_router as role_templates_router
from app.routers.promotions import router as promotions_router
from app.routers.public_api import router as public_router
from app.routers.registry import router as registry_router
from app.routers.release_export import router as release_export_router
from app.routers.resource_mappings import router as resource_mappings_router
from app.routers.resource_shares import router as resource_shares_router
from app.routers.review import console_router as review_links_router
from app.routers.review import router as review_router
from app.routers.scim import router as scim_router
from app.routers.share import console_router as share_links_router
from app.routers.share import router as share_router
from app.routers.share_annotate import router as share_annotate_router
from app.routers.share_notices import router as share_notices_router
from app.routers.sso import router as sso_router
from app.routers.storage_mounts import router as storage_mounts_router
from app.routers.system_agents import router as system_agents_router
from app.routers.system_update import router as system_update_router
from app.routers.tools import router as tools_router
from app.routers.users import router as users_router
from app.routers.videos import router as videos_router
from app.routers.workspaces import router as workspaces_router
from app.services import byoc_uploads, local_exec
from app.services.attachment_body import AttachmentBodyCap
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


_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _warn_local_return_url(settings) -> bool:
    """Warn (once per boot) when a prod console still derives the as_user 3LO
    return URL from the dev default ``http://localhost:5173``.

    AgentCore Identity redirects the user's BROWSER there after IdP consent,
    and the deployer allow-lists it on every as_user agent's workload identity
    — on a real deployment that page does not exist for the user. Not fatal:
    a console without as_user tools never uses it. Returns whether it warned.
    """
    if settings.run_mode != "prod":
        return False
    return_url = settings.resolved_oauth_return_url()
    if (urlsplit(return_url).hostname or "").lower() not in _LOCAL_HOSTS:
        return False
    logging.getLogger("launchpad").warning(
        "run_mode=prod but the as_user (3LO) OAuth return URL is %s — set "
        "LAUNCHPAD_PUBLIC_BASE_URL to the console's public origin (e.g. "
        "https://console.example.com) or LAUNCHPAD_OAUTH_RETURN_URL, then redeploy "
        "agents with as_user tools; see docs/agent-runbook-prod.md",
        return_url,
    )
    return True


def _apply_model_overrides() -> None:
    """Console model choices (admin Models page) win over env — load them onto the
    cached Settings before any route reads a model id."""
    from app.core.db import SessionLocal
    from app.services import agent_project_settings, model_settings

    db = SessionLocal()
    try:
        model_settings.apply_overrides(db)
        # the agent-project switch, same console > env precedence (no query per flush)
        agent_project_settings.apply_override(db)
    finally:
        db.close()


def create_app(resume_jobs: bool = False) -> FastAPI:
    settings = get_settings()
    _assert_production_is_authenticated(settings)
    _warn_local_return_url(settings)
    app = FastAPI(
        title=f"{settings.app_name} API",
        version=settings.version,
        description=API_DESCRIPTION,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        # Console authorization for every /api route lives in one auditable,
        # default-deny table instead of per-route Depends(require_admin). A billable
        # route then meets its project's monthly budget (core/billing_policy.py).
        dependencies=[Depends(enforce_route_policy), Depends(bind_request_project),
                      Depends(enforce_budget)],
    )

    if settings.run_mode == "prod":
        # Only in production: an HSTS header served over a dev HTTP origin pins
        # localhost to HTTPS in the developer's browser, and that cache is sticky
        # and awkward to clear.
        app.middleware("http")(hsts)

    # Register before auth so Starlette's reverse middleware stack keeps auth
    # outermost while these exact-route gates still run before multipart parsing.
    app.middleware("http")(task_assets.task_asset_body_limit_middleware)
    app.middleware("http")(byoc_uploads.upload_body_limit_middleware)
    app.middleware("http")(auth_middleware)
    app.add_middleware(AssistantBodyCap)  # ingress byte cap for assistant writes
    app.add_middleware(AttachmentBodyCap)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    init_db()
    _apply_model_overrides()
    app.include_router(auth_router)
    app.include_router(sso_router)  # console SSO: IdP buttons, OIDC leg, admin config
    app.include_router(scim_router)  # SCIM 2.0 provisioning (sso §2.4), its own bearer token
    app.include_router(overview_router)
    app.include_router(announcements_router)
    app.include_router(model_defaults_router)  # platform model settings (Models page)
    app.include_router(model_catalog_router)  # region model catalog + probe
    app.include_router(videos_router)
    app.include_router(system_update_router)  # online update (OU-1), hub-global
    app.include_router(agents_router)
    app.include_router(agent_telemetry_router)  # detail-page telemetry status (§17.3-4)
    app.include_router(agent_story_router)  # storyline facts + my next steps (SG-1)
    app.include_router(invoke_access_router)  # ARNs + IAM policy for your backend (IA-1)
    app.include_router(agent_skills_router)  # attach-without-registering skill sources
    app.include_router(custom_tools_router)  # custom @tool checks, approvals, sandbox (SB-3)
    app.include_router(tools_router)
    app.include_router(identity_router)  # Connections + Connection-bound gateway targets
    app.include_router(registry_router)
    app.include_router(system_agents_router)
    app.include_router(assistant_router)  # architect assistant (SE-039)
    app.include_router(knowledge_router)  # managed knowledge bases + retrieval playground
    app.include_router(context_layer_router)  # the connected COA deployment + member consent
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
    app.include_router(eval_pipelines_router)  # V2 数据处理: sessions → datasets
    app.include_router(skill_lab_router)
    app.include_router(coding_router)  # coding agent: runtime, repos, turns, files
    app.include_router(experiments_router)
    app.include_router(runtime_canaries_router)
    app.include_router(users_router)  # admin-only console account management
    app.include_router(workspaces_router)  # environments + the request-boundary grants
    app.include_router(projects_router)  # projects, members, roles, bindings (projects §7.10)
    app.include_router(project_isolation_router)  # runtime isolation (projects §10.13)

    app.include_router(project_audit_router)  # a project's audit view + export (§11.6)
    app.include_router(project_quotas_router)  # a project's quotas and budgets (§11.3)
    app.include_router(budgets_router)  # the platform / my budget dashboards (§11.3)
    app.include_router(cost_tags_router)  # cost allocation tags + billing view (CT-2)
    app.include_router(resource_shares_router)  # cross-project sharing grants (§11.2)
    app.include_router(share_notices_router)  # the caller's ended-share notices (SHARE-2)
    app.include_router(role_templates_router)
    app.include_router(my_access_router)  # GET /api/me/access: the caller's projects + keys
    app.include_router(nav_settings_router)  # the rail modules an administrator hid
    app.include_router(agent_project_settings_router)  # may new agents use `default`?
    app.include_router(departments_router)  # departments (projects §2.1, §7.10)
    app.include_router(network_profiles_router)  # VPC network profiles (projects §12)
    app.include_router(storage_mounts_router)  # the storage catalog (storage-mounts §4)
    app.include_router(mcp_reviews_router)  # MCP server tool reviews (projects §15.6)
    app.include_router(gateway_lambda_router)  # Lambda / adopted tool targets (§15.12)
    app.include_router(fleet_router)  # fleet overview, governance health, marketplace (T37-T39)
    app.include_router(alerts_router)  # spend attribution + threshold alerts (T28/T29)
    app.include_router(promotions_router)  # release bundles, promotions, inbox (T20-T22)
    app.include_router(resource_mappings_router)  # logical resource mapping (T23)
    app.include_router(release_export_router)  # GitOps bundle export (T31)
    app.include_router(environments_router)  # environment compare + drift (T32)
    app.include_router(apikeys_router)
    app.include_router(public_router)
    app.include_router(feedback_router)  # thumbs feedback (T15)
    app.include_router(share_links_router)  # share-link CRUD (T14)
    app.include_router(channel_links_router)  # channel-link creation (T30)
    app.include_router(channels_router)  # IM webhooks; before /share/{token} routes (T30)
    app.include_router(share_router)  # account-free /share surface (T13)
    app.include_router(review_router)  # account-free SME review (T34) — before /share/{token}
    app.include_router(review_links_router)  # review-link CRUD (T34)
    app.include_router(answer_rules_router)  # curated answers (T35)
    app.include_router(issues_router)  # intent view + issue box (T33/T36)
    app.include_router(dlc_router)  # Agent-DLC: criteria, gates, calibration, watch
    # account-free labelling for calibration — before /share/{token}, like review
    app.include_router(share_annotate_router)
    if resume_jobs:
        clear_stale_turn_claims()  # only a live request of THIS process can hold one
        resumed = resume_pending_jobs()
        if resumed:
            logging.getLogger("launchpad").info(
                "resumed %d interrupted deploy/bootstrap job(s)", len(resumed)
            )
        from app.system_agents.timeout_migration import migrate_preset_timeouts

        migrated = migrate_preset_timeouts()  # assistant-context C5: 900 s → 1500 s
        if migrated:
            logging.getLogger("launchpad").info(
                "re-publishing %d system preset(s) onto the new turn budget", len(migrated)
            )
        resumed_assets = resume_evaluation_assets()
        if resumed_assets:
            logging.getLogger("launchpad").info(
                "resumed %d interrupted evaluation-asset operation(s)", len(resumed_assets)
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
        # coding-runtime install/uninstall jobs run on a thread a restart killed;
        # fail them honestly (each step is idempotent: the admin re-runs it)
        sweep_coding_runtime_jobs()
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
        # online update (OU-1): the updater's apply.result, interrupted prepare jobs
        from app.services.system_update import resume_system_updates

        resume_system_updates()
        # Agent-DLC: the one background tick — due watch re-evaluations, alert rules,
        # calibration and waiver expiry (docs/agent-dlc-design.md §10)
        from app.dlc.scheduler import start as start_dlc_scheduler

        start_dlc_scheduler()
        # coding turns run in their microVMs whether or not a browser streams them:
        # the watcher settles them (usage, snapshot) and snapshots saved files
        coding_watcher.start()

    app.add_api_route("/api/health", health, methods=["GET"])

    return app


app = create_app(resume_jobs=True)
