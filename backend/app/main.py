"""FastAPI application factory."""

import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.deployer.byoc  # noqa: F401 — registers the byoc (bring your own code) method
import app.deployer.harness_local  # noqa: F401 — registers the self-built harness method (方式B)
import app.deployer.zip_runtime  # noqa: F401 — registers zip_runtime + studio methods
from app.assistant.evaluation_assets import resume_operations as resume_evaluation_assets
from app.assistant.service import clear_stale_turn_claims
from app.core.config import get_settings
from app.core.db import SessionLocal, init_db
from app.core.display_time import DisplayTimeJSONResponse
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.route_policy import enforce_route_policy
from app.deployer import build_dirs
from app.deployer.pipeline import install_shutdown_hook, resume_pending_jobs
from app.evaluation.engine import set_engine
from app.evaluation.executor import set_job_runner, set_runtime_executor
from app.evaluation.online_routers import router as online_eval_router
from app.evaluation.online_scheduler import start_online_scheduler
from app.evaluation.pipeline_routers import router as eval_pipelines_router
from app.evaluation.pipeline_routers import (
    sweep_interrupted_from_sessions_jobs,
    sweep_interrupted_pipelines,
)
from app.evaluation.routers import router as evaluation_router
from app.evaluation.runtime_executor import RuntimeExecutor, inprocess_job_runner
from app.evaluation.service import resume_interrupted_runs, sweep_token_files
from app.evaluation.strands_engine import StrandsEngine
from app.optimization.canary_routers import router as runtime_canaries_router
from app.optimization.canary_service import (
    clear_stale_running_actions as clear_stale_canary_actions,
)
from app.optimization.legacy_experiments import count_legacy_experiment_rows
from app.routers.agent_skills import router as agent_skills_router
from app.routers.agents import router as agents_router
from app.routers.announcements import router as announcements_router
from app.routers.apikeys import router as apikeys_router
from app.routers.assistant import AssistantBodyCap
from app.routers.assistant import router as assistant_router
from app.routers.auth import OPEN_CONSOLE_REMEDY, auth_middleware
from app.routers.auth import enabled as auth_enabled
from app.routers.auth import router as auth_router
from app.routers.capabilities import router as capabilities_router
from app.routers.chat import router as chat_router
from app.routers.codegen import router as codegen_router
from app.routers.conversations import router as conversations_router
from app.routers.execution import router as execution_router
from app.routers.governance import router as governance_router
from app.routers.identity import router as identity_router
from app.routers.knowledge import router as knowledge_router
from app.routers.memory import router as memory_router
from app.routers.memory_resources import router as memory_resources_router
from app.routers.model_providers import router as model_providers_router
from app.routers.model_roles import router as model_roles_router
from app.routers.observability import router as observability_router
from app.routers.observability import traces_router as chat_traces_router
from app.routers.overview import router as overview_router
from app.routers.policies import router as policies_router
from app.routers.policy_engines import router as policy_engines_router
from app.routers.public_api import router as public_router
from app.routers.registry import router as registry_router
from app.routers.system_agents import router as system_agents_router
from app.routers.tools import router as tools_router
from app.routers.users import router as users_router
from app.routers.videos import router as videos_router
from app.routers.workspaces import router as workspaces_router
from app.services import (
    byoc_uploads,
    chat_turns,
    embedding_guard,
    knowledge_backend,
    local_exec,
    memory_backend,
    model_registry,
    model_roles,
    policy_audit,
    policy_backend,
    registry_backend,
)
from app.services.attachment_body import AttachmentBodyCap
from app.services.build_info import build_info
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from app.services.model_probe import ModelProbeHooks
from app.services.policy_cedar import CedarEngine
from app.services.registry_local import LocalRegistryBackend
from app.services.snapshots import backfill_baselines as backfill_snapshot_baselines
from app.skill_lab import task_assets
from app.skill_lab.jobs import sweep_interrupted_jobs as sweep_skill_lab_jobs
from app.skill_lab.routers import router as skill_lab_router
from app.system_agents import auto_repair as system_agents_auto_repair
from app.system_agents import service as system_agents_service

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


async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "status": "ok",
        "version": settings.version,
        "region": settings.region,
        # N13-obs10 (additive): the deployed commit — {sha, short, built_at, source} (+ N17-obs466
        # dist_sha: the commit the served console bundle was built from), every
        # key null when no build stamp exists (services/build_info.py)
        "build": build_info(),
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


def _load_model_role_overrides() -> None:
    db = SessionLocal()
    try:
        model_roles.load_overrides(db)
    finally:
        db.close()


def create_app(resume_jobs: bool = False) -> FastAPI:
    configure_logging()  # level + logger name on every record (r7-i addendum 6)
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
        # default-deny table instead of per-route Depends(require_admin).
        dependencies=[Depends(enforce_route_policy)],
        # N17-obs463: every JSON body's times in the display zone (app/core/display_time.py)
        default_response_class=DisplayTimeJSONResponse,
    )

    if settings.run_mode == "prod" and not settings.console_http_lab:
        # Normal production pins HTTPS. The explicit lab profile serves both protocols
        # with one session; its edge also clears historical HSTS over HTTPS.
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
    # D21: the model registry is the catalog. A fresh ledger is seeded from the built-in
    # tables (today's rows, `source: builtin`) and the snapshot every `schemas.agent` view
    # reads is published; the 30 s stamp check covers a second process (plan §5.1/5.4).
    model_registry.ensure_seeded()
    model_registry.install_refresher()
    # r15-e (upstream #275 subset): the administrator's model-role overrides
    # (`platform_settings`) win over env / yaml — load them into the snapshot
    # `get_settings` re-applies on every rebuild, before any route reads a model id.
    _load_model_role_overrides()
    # D21 (M1-b): the probe + credential slots behind Settings → Model Providers. Real
    # collaborators (httpx, DNS, the `ssm` / Identity funnel clients, the Global bearer
    # client); nothing runs until an admin clicks Probe / Register credential.
    model_registry.set_probe_hooks(ModelProbeHooks())
    if "registry" in settings.features:
        # The local Registry (N4) is ledger-backed: it exists as soon as the ledger does.
        registry_backend.set_backend(LocalRegistryBackend())
    # --- CN memory/KB (N6) ---
    if "memory" in settings.features:
        # Self-hosted Memory (N6): S3 snapshots + OpenSearch k-NN + SQS extraction.
        # Registered whenever the feature is on; a workspace without the substrate
        # answers 503 `memory.not_provisioned` per call (ruling R16) rather than
        # hiding the module.
        memory_backend.set_backend(SelfHostedMemoryBackend())
    if "knowledge_bases" in settings.features:
        # Self-hosted Knowledge Bases (N6): same substrate as Memory, one
        # OpenSearch index per KB. Registered whenever the feature is on; a
        # workspace without the substrate answers 503 `kb.not_provisioned` per
        # call (ruling R16) rather than hiding the module.
        knowledge_backend.set_backend(SelfHostedKnowledgeBackend())
    # D21 (M1-d): the embedding row may change only while every vector index is empty
    # (409 embedding.reindex_required otherwise), and the Settings page warns when an
    # installed preset's snapshot pins an older catalog than the ledger's.
    embedding_guard.install()
    model_registry.add_warning_producer(system_agents_service.catalog_snapshot_warnings)
    # N17-obs206: the installed-preset auto-repair sweep — one scheduler per process (its
    # thread starts lazily) + the registry listener that debounces a sweep after every
    # catalog-shaping write; both triggers honour `system_presets_auto_repair`
    system_agents_auto_repair.install()
    # --- end CN memory/KB (N6) ---
    if "policy" in settings.features:
        # Self-hosted Policy (D20): Cedar evaluated in-process by cedarpy; the ledger +
        # S3 hold the policies. Registered whenever the feature is on; the router below
        # is mounted only with it, and answers 503 `feature.unavailable` if a test
        # clears the slot.
        policy_backend.set_backend(CedarEngine())
        if settings.policy_audit_enabled:
            # P1-d: every decision row is mirrored to CloudWatch /launchpad/policy/decisions
            # by a batched, fail-soft background sink (the ledger stays the record).
            policy_audit.install(policy_audit.CloudWatchDecisionSink.from_settings())
    app.include_router(auth_router)
    app.include_router(overview_router)
    app.include_router(capabilities_router)  # feature set the console navigation renders
    app.include_router(announcements_router)
    app.include_router(videos_router)  # hub-global video library (published) + admin drafts
    app.include_router(agents_router)
    app.include_router(agent_skills_router)  # attach-without-registering skill sources
    app.include_router(tools_router)
    app.include_router(identity_router)  # Connections + Connection-bound gateway targets
    app.include_router(registry_router)
    app.include_router(system_agents_router)
    app.include_router(assistant_router)  # architect assistant (SE-039)
    app.include_router(knowledge_router)  # managed knowledge bases + retrieval playground
    app.include_router(chat_router)
    app.include_router(memory_router)  # read-only short-/long-term memory console
    app.include_router(memory_resources_router)  # memory resource lifecycle (create/delete)
    app.include_router(execution_router)  # studio local-debug: run un-deployed code
    app.include_router(conversations_router)  # studio local-debug: multi-turn chat
    app.include_router(codegen_router)  # studio local-debug: AI fix (diagnose + repair)
    app.include_router(governance_router)
    if "policy" in settings.features:
        app.include_router(policy_engines_router)  # Global model: engines, policies, attachments
        app.include_router(policies_router)  # retired /api/policies → 410 policy.moved
    app.include_router(observability_router)
    app.include_router(chat_traces_router)  # Chat Inspector trace rail (/api/traces/{session_id})
    # --- CN evaluation / optimization (N12) ---
    # Mounted only when the feature is on (N1 capabilities drive both navigations);
    # with the feature off the paths answer 404 like any unknown route.
    if "evaluation" in settings.features:
        # E1: the Strands engine (SCORE NOW, in-process dev), the worker executor (prod)
        # and the worker's own job dispatcher for the in-process executor — one code path
        set_engine(StrandsEngine())
        set_runtime_executor(RuntimeExecutor())
        set_job_runner(inprocess_job_runner)
        app.include_router(evaluation_router)
        app.include_router(online_eval_router)
        app.include_router(eval_pipelines_router)  # V2 data processing: sessions → datasets
    if "optimization" in settings.features:
        # N13-obs2: canaries only — the configuration-experiment routes are retired
        app.include_router(runtime_canaries_router)
    # --- end CN evaluation / optimization (N12) ---
    app.include_router(skill_lab_router)
    app.include_router(users_router)  # admin-only console account management
    app.include_router(workspaces_router)  # environments + the request-boundary grants
    app.include_router(model_providers_router)  # D21: admin-only Settings → Model Providers
    app.include_router(model_roles_router)  # r15-e: model roles (GET for members, admin writes)
    app.include_router(apikeys_router)
    app.include_router(public_router)
    if resume_jobs:
        # N17-obs113: a SIGTERM to the service (systemctl restart → the whole control
        # group) marks this process as stopping, so a build subprocess it kills leaves its
        # stage `interrupted` (resumed at the next start) instead of a terminal failure.
        install_shutdown_hook()
        clear_stale_turn_claims()  # only a live request of THIS process can hold one
        chat_turns.clear_stale_turn_claims()  # N-ARCH-obs15 v2: the Chat playground's turns
        # snapshot #1 ("baseline") for live agents that predate spec snapshots (upstream
        # #277), so the scorecards have a version to read; ledger-only, idempotent
        baselined = backfill_snapshot_baselines()
        if baselined:
            logging.getLogger("launchpad").info(
                "recorded a baseline spec snapshot for %d agent(s)", baselined
            )
        # N17-obs151: the local build-dir retention sweep runs BEFORE the interrupted jobs are
        # resumed, so no package stage is writing under data/builds while it deletes
        build_dirs.startup_sweep(settings)
        resumed = resume_pending_jobs()
        if resumed:
            logging.getLogger("launchpad").info(
                "resumed %d interrupted deploy/bootstrap job(s)", len(resumed)
            )
        # N17-obs206: sweep the installed presets of EVERY workspace once the host has
        # finished booting (`system_presets_auto_repair_start_delay_s`, default 60 s) — the
        # resumed jobs above keep their rows `deploying`, so the sweep skips them
        system_agents_auto_repair.schedule_startup(settings)
        if "policy" in settings.features:
            # per-user policy stream B5: ingest the gateway interceptor's decision lines into
            # policy_decisions — a no-op while Settings.policy_gateway_interceptor is off
            policy_audit.start_gateway_ingest(settings)
        # N12 W-E5: evaluation-assets operations an authorized caller approved and that a
        # restart interrupted (queued/running) resume from their persisted intents
        resumed_assets = resume_evaluation_assets()
        if resumed_assets:
            logging.getLogger("launchpad").info(
                "resumed %d interrupted evaluation-assets operation(s)", len(resumed_assets)
            )
        reaped = local_exec.reap_orphan_containers()
        if reaped:
            logging.getLogger("launchpad").info(
                "reaped %d orphaned studio exec container(s)", reaped
            )
        # --- CN evaluation / optimization (N12) ---
        from app.services import policy_engines, policy_gateway_publish

        if "policy" in settings.features:
            # Global model (2026-10-28): the pre-engine ledger (one implicit set per workspace,
            # the workspace mode / switches) becomes one default engine per workspace, attached
            # to the workspace gateway when the old posture was live — idempotent, journalled
            migrated = policy_engines.migrate_once()
            if migrated:
                logging.getLogger("launchpad").info(
                    "policy: %d workspace(s) migrated to the engine model", len(migrated))
        if policy_gateway_publish.configured():
            # A5a-2: a gateway bundle whose write failed after its install is written now;
            # review F2 (N17-obs491): and any bundle that no longer says what the hub would
            # compose — the global kill switch included, so this runs with the switch OFF too
            db = SessionLocal()
            try:
                rewritten = policy_gateway_publish.reconcile(db)
            except Exception:  # noqa: BLE001 — the next publish retries it
                logging.getLogger("launchpad").exception("gateway bundle reconcile failed")
                rewritten = []
            finally:
                db.close()
            if rewritten:
                logging.getLogger("launchpad").info(
                    "rewrote %d pending gateway bundle(s)", len(rewritten))
            policy_gateway_publish.start_reconciler()  # every 300 s (+ jitter) while on
        if "evaluation" in settings.features:
            # D5 (I9): token files whose job no watcher will see again go first
            swept_tokens = sweep_token_files()
            if swept_tokens:
                logging.getLogger("launchpad").info(
                    "deleted %d orphaned eval token file(s)", len(swept_tokens))
            resumed_evals = resume_interrupted_runs()
            if resumed_evals:
                logging.getLogger("launchpad").info(
                    "reconciling %d interrupted eval run(s): %s",
                    len(resumed_evals), ", ".join(resumed_evals),
                )
            if settings.online_eval_enabled:
                start_online_scheduler(settings)  # W-E2-2 fills the loop (plan §B.3)
        if "optimization" in settings.features:
            legacy_rows = count_legacy_experiment_rows()
            if legacy_rows:
                logging.getLogger("launchpad").info(
                    "legacy configuration-experiment rows: %d; routes retired N13-obs2",
                    legacy_rows,
                )
            stale_canaries = clear_stale_canary_actions()
            if stale_canaries:
                logging.getLogger("launchpad").info(
                    "cleared stale Runtime Canary action(s) on: %s", ", ".join(stale_canaries),
                )
        # --- end CN evaluation / optimization (N12) ---
        # skill-lab jobs are child subprocesses — a restart killed them; fail
        # the rows honestly (resume_pending_jobs cannot see this table)
        sweep_skill_lab_jobs()
        # N-SCN-obs10: a from-sessions dataset job the restart interrupted is failed, not
        # re-run (its dataset write is the last step; see pipeline_routers)
        swept_from_sessions = sweep_interrupted_from_sessions_jobs()
        if swept_from_sessions:
            logging.getLogger("launchpad").info(
                "failed %d interrupted from-sessions job(s)", len(swept_from_sessions)
            )
        # upstream #239 (cn/r15-b): a data pipeline run is a daemon thread; a row the
        # restart left `running` is failed honestly, not re-run (same reason as above)
        swept_pipelines = sweep_interrupted_pipelines()
        if swept_pipelines:
            logging.getLogger("launchpad").info(
                "failed %d interrupted data pipeline run(s)", len(swept_pipelines)
            )

    app.add_api_route("/api/health", health, methods=["GET"])

    return app


app = create_app(resume_jobs=True)
