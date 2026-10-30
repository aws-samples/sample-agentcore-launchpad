"""Which console modules this deployment exposes (drives both console navigations)."""

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.deployer.pipeline import method_capabilities, registered_methods
from app.routers.auth import require_identity
from app.routers.tools import browser_signing_supported
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas import model_catalog
from app.schemas.agent import (
    configured_providers,
    model_catalog_entries,
    principal_of_identity,
    providers,
)
from app.services import policy_backend, policy_store

router = APIRouter(prefix="/api", tags=["capabilities"])


def policy_engine_registered() -> bool:
    return policy_backend.backend_or_none() is not None


@router.get("/capabilities")
def capabilities(
    request: Request,
    db: Session = Depends(get_db), ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    settings = get_settings()
    # N17-obs203 §6: the catalog a user is OFFERED = the workspace-scope providers' models +
    # their own personal providers' models (administrators: everything) — every picker reads
    # this list, so another member's personal provider never shows up anywhere
    principal = principal_of_identity(require_identity(request))
    # N-R6-obs1: whether each provider's API key is configured in THIS workspace (the
    # bootstrap's `resources.model_credentials` record, else the hub env) — the rows stay
    # listed so the console can explain why one is not selectable.
    configured = configured_providers(ws.context.resources)
    # D21: the model registry snapshot every row below is a view over (plan §3). Existing
    # keys keep their semantics; the registry dimension rides along additively.
    snapshot = model_catalog.current()
    embedding = snapshot.embedding_model()
    # D20 P1-d (plan §2.6): `policy` is a capability only while the Cedar engine is
    # registered — with the feature on but no engine the routers answer 503, so both
    # navigations hide the Policy tabs instead of showing a dead module.
    policy_on = "policy" in settings.features and policy_engine_registered()
    features = [f for f in settings.features if f != "policy" or policy_on]
    body: dict[str, Any] = {
        "features": features,
        # N17-obs463: the zone the API's times are in and the console formats in
        "display_timezone": settings.display_timezone,
        # CN additive (N12-obs41): the evaluator forms (V2 + classic) hide the Lambda
        # "Code" definition while the workspace has not opted in — the same flag
        # `POST/PUT /api/eval/evaluators` enforces (403 evaluation.code_evaluators_disabled).
        "evaluation": {"code_evaluators_enabled": bool(settings.eval_code_evaluators_enabled)},
        # CN additive (N17-obs258): Web Bot Auth browser signing is partition-scoped —
        # false in aws-cn, where the Browser demo hides the toggle and `POST
        # /api/demos/browser {web_bot_auth: true}` is refused (409
        # browser.signing_unsupported_in_partition).
        "tools": {"browser_signing": browser_signing_supported(ws.context)},
        # D21 additive: the journal id the snapshot was built from (0 = built-in table)
        "catalog_version": snapshot.version,
        # CN additive (N-R6): the model catalog with its provider dimension — the console
        # pickers merge these rows into their static Bedrock list and show the provider
        # badge; `tools: false` rows are judge-only. D21: chat rows only, a row an
        # administrator disabled (model / provider / this workspace's flag) is omitted —
        # like a row the server "no longer lists", which the frontend already tolerates.
        "models": [
            {
                "model_id": e["model_id"],
                "label": e["label"],
                "provider": e["provider"],
                "provider_kind": e["provider_kind"],
                "provider_label": e["provider_label"],
                "api_model_name": e["api_model_name"],
                "tools": e["tools"],
                "streaming": e["streaming"],
                "vision": e["vision"],
                "default": e["default"],
                # CN additive (N-R6-obs1): = the provider is configured; an unavailable
                # row is rendered disabled with the SSM parameter to create
                "available": e["available"],
                # D21 additive: registry dimension
                "kind": e["kind"],
                "source": e["source"],
                "enabled": e["enabled"],
            }
            for e in model_catalog_entries(ws.context.resources, workspace_id=ws.id,
                                           principal=principal)
        ],
        "model_providers": {
            name: {"kind": row["kind"], "label": row["label"],
                   "credential_provider": row["credential_provider"],
                   # CN additive (N-R6-obs1)
                   "configured": configured[name],
                   "ssm_parameter": row["ssm_parameter"],
                   "api_key_env": row["api_key_env"],
                   # D21 additive: registry row id, enable flag, last probe state
                   "id": prow.id if prow is not None else None,
                   "enabled": prow.enabled if prow is not None else True,
                   "probe_state": prow.probe_state if prow is not None else "never"}
            for name, row in providers().items()
            for prow in (snapshot.providers.get(name),)
            # N17-obs203 §6: another member's personal provider is not listed either
            if prow is None or principal is None or principal.sees_provider(prow)
        },
        # D21 additive (plan §3): the active embedding row for the KB / Memory info line
        # (embedding rows are never in `models[]` — pickers are chat-only)
        "embedding": (
            {"model_id": embedding.model_id, "dimension": embedding.embedding_dimension,
             "provider": embedding.provider}
            if embedding is not None else None
        ),
    }
    # round 16 additive (N15-obs16; plans/CAPABILITY-MATRIX-DESIGN.md §1.2): per deploy
    # method, the creation-time option domain its generate stage renders — declared once at
    # `register_method` (`MethodCapabilities`), enforced by `POST /api/agents` (422
    # `agent.method_unsupported`) and read by the wizard to disable what a method cannot take.
    body["methods"] = {name: method_capabilities(name).as_dict() for name in registered_methods()}
    if policy_on:
        # Global model: the workspace's policy engines and how many gateways are attached
        # (`policy_engines.capability_summary` → `{engines, attached_gateways}`).
        body["policy"] = policy_store.capability_summary(db, ws.id)
    return body
