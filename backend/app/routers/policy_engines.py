"""Policy engines and ``/api/governance/gateways/{gid}/policy-engine``.

The Global AgentCore Policy model over the self-hosted Cedar / Dogwood engine (contract
``docs/cn/interfaces/policy-engines.md``).

Mounted by ``app/main.py`` only when ``"policy"`` is in ``Settings.features``; every handler
first asks for the registered Cedar engine (``policy_backend.get_backend()``) and so answers
503 ``feature.unavailable {"feature": "policy"}`` while none is registered — the Memory /
Registry / Knowledge pattern. Authorization is the ``ROUTE_POLICY`` table (reads MEMBER,
engine / policy writes ``perm:policy.manage``, gateway attachments ADMIN); the one mixed case
the table cannot express lives here: ``POST …/simulate`` with ``record: true`` requires
``perm:policy.manage`` (it writes an audit row).

Model (Global parity): PolicyEngine (many per workspace) ← Policy (belongs to ONE engine,
``enforcement_mode`` active | log_only); Gateway → at most ONE attachment
``{engine_id, mode LOG_ONLY | ENFORCE}``; decisions are made by the Gateway interceptor only.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_db
from app.core.display_time import to_utc_naive
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ApiKey, User, UserWorkspace
from app.models.policy import PolicyEngine
from app.policy import list_templates
from app.routers.auth import require_identity, require_permission
from app.routers.workspaces import WorkspaceScope, require_workspace
from app.schemas.agent import AgentSpec, ToolRef
from app.schemas.policy import (
    EnforcementModeUpdate,
    GatewayPolicyEngineAttach,
    GatewaySimulateRequest,
    PolicyCreate,
    PolicyEngineCreate,
    PolicyEngineUpdate,
    PolicyPublish,
    PolicyRetire,
    PolicySimulationRequest,
    PolicyUpdate,
    SimulateRequest,
    StatementBody,
)
from app.services import (
    policy_audit,
    policy_backend,
    policy_bundle,
    policy_engines,
    policy_store,
)
from app.services import users as users_service
from app.services.agent_access import agent_clause
from app.services.policy_backend import AuthzRequest
from app.services.policy_entities import (
    Principal,
    request_context,
    split_tool_id,
    tool_ref,
)
from app.services.visibility import Viewer, require_viewer
from app.templates.gateway_support import gateway_tool_patterns

router = APIRouter(prefix="/api", tags=["policy-engines"])

ENGINES = "/policy-engines"
GATEWAY_ID = Path(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_CHANNELS = {"chat", "v1", "system", "runtime"}


def _cedar():
    return policy_backend.get_backend()


def _operator(request: Request) -> str:
    return require_identity(request).username


def _engine_or_404(db: Session, ws: WorkspaceScope, engine_id: str) -> PolicyEngine:
    return policy_engines.get_engine(db, ws.id, engine_id)


def _policy_out(db: Session, policy, **extra: Any) -> dict[str, Any]:
    out = policy_store.policy_view(db, policy)
    out.update(extra)
    return out


# ── engines ───────────────────────────────────────────────────────────────────


@router.get(ENGINES)
def list_engines(
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    return {"engines": [policy_engines.engine_view(db, e)
                        for e in policy_engines.list_engines(db, ws.id)]}


@router.post(ENGINES, status_code=201)
def create_engine(
    body: PolicyEngineCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    identity = require_identity(request)
    engine = policy_engines.create_engine(
        db, ws.id, name=body.name, description=body.description or "",
        operator=identity.username, operator_name=identity.username)
    return policy_engines.engine_view(db, engine)


# static sub-resources (declared before `/{engine_id}` so they win the match)


def _declared_tool_patterns(spec: dict[str, Any]) -> list[str]:
    """``gateway_tool_patterns`` over a stored spec; a legacy / foreign spec that no longer
    validates as a whole still contributes every gateway ``ToolRef`` that does."""
    try:
        return gateway_tool_patterns(AgentSpec.model_validate(spec))
    except Exception:
        refs = []
        for raw in spec.get("tools") if isinstance(spec.get("tools"), list) else []:
            try:
                refs.append(ToolRef.model_validate(raw))
            except Exception:
                continue
        return gateway_tool_patterns(AgentSpec.model_construct(tools=refs))


@router.get(ENGINES + "/{engine_id}/entities")
def entity_catalog(
    engine_id: str,
    gateway_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
    viewer: Viewer = Depends(require_viewer),
) -> dict[str, Any]:
    """Pickers for the simulator and prefill data for the editor of one engine: users granted
    on this workspace (+ administrators), the workspace's API keys and live agents, the Gateway
    tools the agents' specs declare (ledger-derived — no AWS call; ``gateway_id`` keeps the
    tools of agents bound to that gateway), the engine's gateways and the baseline templates."""
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    granted = set(
        db.scalars(
            select(UserWorkspace.user_id).where(UserWorkspace.workspace_id == ws.id)
        ).all()
    )
    users = [
        {"id": u.id, "user_id": u.id, "username": u.username, "role": u.role}
        for u in db.scalars(select(User).where(User.status == "active").order_by(User.username))
        if u.role == "admin" or u.id in granted
    ]
    keys = [
        # `created_by` here is the minting USERNAME (what the Cedar attribute carries)
        {"id": k.id, "name": k.name, "label": k.label, "created_by": k.created_by_name,
         "enabled": bool(k.enabled)}
        for k in db.scalars(
            select(ApiKey).where(ApiKey.workspace_id == ws.id).order_by(ApiKey.created_at)
        )
    ]
    agents_rows = list(
        db.scalars(
            select(Agent)
            .where(Agent.workspace_id == ws.id, Agent.status != "deleted", agent_clause(viewer))
            .order_by(Agent.name)
        )
    )
    agents = [
        {"id": a.id, "name": a.name, "method": a.method, "status": a.status,
         "system": bool(a.system_key), "owner": a.created_by_name}
        for a in agents_rows
    ]
    tools: dict[str, dict[str, Any]] = {}
    for a in agents_rows:
        if gateway_id and not _binds_gateway(a.spec or {}, gateway_id):
            continue
        for pattern in _declared_tool_patterns(a.spec or {}):
            target, tool = split_tool_id(pattern)
            entry = tools.setdefault(
                pattern,
                {"id": pattern, "name": pattern, "target": target, "tool": tool,
                 "pattern": "*" in pattern or "?" in pattern or "[" in pattern,
                 "agent_ids": [], "source": "declared"},
            )
            entry["agent_ids"].append(a.id)
    own_id, own_arn = policy_engines.workspace_gateway(db, ws.id)
    gateways = []
    seen: set[str] = set()
    for a in policy_engines.attachments_of(db, engine.id):
        seen.add(a.gateway_id)
        facts = policy_engines.gateway_facts(db, a.gateway_id, workspace_id=ws.id)
        gateways.append({"gateway_id": a.gateway_id, "gateway_arn": facts["gateway_arn"],
                         "attachment": policy_engines.attachment_view(db, a.gateway_id)})
    if own_id and own_id not in seen:
        gateways.append({"gateway_id": own_id, "gateway_arn": own_arn,
                         "attachment": policy_engines.attachment_view(db, own_id)})
    return {
        "engine_id": engine.id,
        "users": users,
        "api_keys": keys,
        "agents": agents,
        "tools": sorted(tools.values(), key=lambda t: t["name"]),
        "roles": ["admin", "member"],
        "system": ["hub"],
        "gateways": gateways,
        "templates": list(list_templates()),
    }


def _binds_gateway(spec: dict[str, Any], gateway_id: str) -> bool:
    """Whether one of the spec's gateway ToolRefs names ``gateway_id``."""
    for tool in spec.get("tools") if isinstance(spec.get("tools"), list) else []:
        if isinstance(tool, dict) and tool.get("type") == "gateway" and \
                str((tool.get("config") or {}).get("gateway_id") or "") == gateway_id:
            return True
    return False


@router.get(ENGINES + "/{engine_id}")
def get_engine(
    engine_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    return policy_engines.engine_view(db, _engine_or_404(db, ws, engine_id))


@router.put(ENGINES + "/{engine_id}")
def update_engine(
    engine_id: str,
    body: PolicyEngineUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = policy_engines.update_engine(db, ws.id, engine_id, description=body.description,
                                         operator=_operator(request))
    return policy_engines.engine_view(db, engine)


@router.delete(ENGINES + "/{engine_id}")
def delete_engine(
    engine_id: str,
    request: Request,
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    policy_engines.delete_engine(db, ws.id, engine_id, operator=_operator(request), force=force)
    return {"deleted": True, "id": engine_id}


@router.get(ENGINES + "/{engine_id}/status")
def engine_status(
    engine_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """The Dogwood engine's shards and THIS engine's install on them. ``available: false``
    (and no engine call) while the platform switch is off; an engine that does not answer is
    ``ready: false`` per shard, never an error."""
    _cedar()
    return policy_engines.engine_status_view(db, _engine_or_404(db, ws, engine_id))


@router.get(ENGINES + "/{engine_id}/changes")
def list_changes(
    engine_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    policy_engines.get_engine(db, ws.id, engine_id, include_deleted=True)
    rows = policy_store.list_changes(db, ws.id, engine_id=engine_id, limit=limit)
    return {"changes": [policy_store.change_view(r) for r in rows]}


@router.get(ENGINES + "/{engine_id}/decisions")
def list_decisions(
    engine_id: str,
    gateway_id: str | None = Query(default=None),
    source: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    principal: str | None = Query(default=None),
    since: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Observability: the decisions the engine's gateways made (``gateway_id`` filters one),
    same row shape as before plus ``gateway_id`` / ``engine_id`` / ``log_only_would``."""
    _cedar()
    policy_engines.get_engine(db, ws.id, engine_id, include_deleted=True)
    since_at: datetime | None = None
    if since:
        try:
            # N17-obs463: the ledger column is naive UTC; a +08:00 value is converted
            since_at = to_utc_naive(datetime.fromisoformat(since.replace("Z", "+00:00")))
        except ValueError as exc:
            raise AppError(
                "policy.invalid_since", "since must be an ISO-8601 timestamp", status_code=422,
            ) from exc
    policy_store.prune_decisions(db)
    rows = policy_store.list_decisions(
        db, ws.id, engine_id=engine_id, gateway_id=gateway_id, source=source,
        decision=decision, agent_id=agent_id, principal=principal, since=since_at, limit=limit,
    )
    return {
        "decisions": [policy_store.decision_view(r, db) for r in rows],
        # the CloudWatch decisions channel (`/launchpad/policy/decisions[-env]`)
        "runtime_channel": policy_audit.runtime_channel(),
    }


# ── statements: validate / format / simulate ──────────────────────────────────


@router.post(ENGINES + "/{engine_id}/validate")
def validate_statement(
    engine_id: str,
    body: StatementBody,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    if body.policy_id:
        policy_store.get_policy(db, ws.id, body.policy_id, engine.id)
    dialect = policy_store.detect_dialect(body.statement)
    if dialect == "dogwood" or get_settings().policy_gateway_interceptor:
        # Both vocabularies now run the install compiler against the Gateway's tools.
        # Preflight shares the engine simulations' per-user budget.
        _engine_simulation_slot(require_identity(request).username or "anonymous")
    return policy_store.validate_statement(
        body.statement, dialect=dialect, db=db, engine=engine,
        policy_id=body.policy_id).as_dict()


@router.post(ENGINES + "/{engine_id}/format")
def format_statement(
    engine_id: str,
    body: StatementBody,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    cedar = _cedar()
    _engine_or_404(db, ws, engine_id)
    try:
        return {"statement": cedar.format(body.statement)}
    except ValueError as exc:
        raise AppError(
            "policy.invalid_statement", f"the statement does not parse: {exc}",
            status_code=422,
        ) from exc


def _resolve_principal(db: Session, ws: WorkspaceScope, spec) -> tuple[Principal, Agent | None]:
    """The simulator's principal from its picker, preferring the ledger row when one
    exists so attributes (role, key name) are the real ones."""
    if spec.kind == "user":
        user = users_service.find_by_username(db, spec.id)
        role = spec.role or (user.role if user is not None else None)
        principal = Principal.from_request("user", spec.id, role=role)
        if user is not None:
            principal = Principal(
                kind="user", id=spec.id, label=user.username, role=user.role,
                attrs={"username": user.username, "role": user.role, "user_id": user.id},
            )
        return principal, None
    if spec.kind == "api_key":
        key = db.get(ApiKey, spec.id)
        if key is None or key.workspace_id != ws.id:
            key = db.scalars(
                select(ApiKey).where(ApiKey.workspace_id == ws.id, ApiKey.name == spec.id)
            ).first()
        return (Principal.from_api_key(key) if key is not None
                else Principal.from_request("api_key", spec.id)), None
    if spec.kind == "agent":
        agent = _agent_or_404(db, ws, spec.id)
        return Principal.from_agent(agent), agent
    return Principal.system(spec.id or "hub"), None


def _agent_or_404(db: Session, ws: WorkspaceScope, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != ws.id or agent.status == "deleted":
        raise NotFoundError("agent.not_found", f"agent {agent_id} was not found")
    return agent


@router.post(ENGINES + "/{engine_id}/simulate")
def simulate(
    engine_id: str,
    body: PolicySimulationRequest,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Dry-run one ``(principal, action, resource, context)`` against the engine's ACTIVE set
    (default), every policy's draft (``include_drafts``) or one pinned version. The gateway
    (``gateway_id``, default the workspace gateway) supplies the mode the answer reports and
    the resource ARN the Dogwood engine evaluates against."""
    if isinstance(body, GatewaySimulateRequest):
        body = body.as_simulate_request()
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    if body.record:
        require_permission(request, "policy.manage")
    principal, principal_agent = _resolve_principal(db, ws, body.principal)
    context = dict(body.context or {})
    channel = str(context.get("channel") or ("v1" if principal.kind == "api_key" else "chat"))
    gateway_id = body.gateway_id or policy_engines.workspace_gateway(db, ws.id)[0] or None
    if body.engine is not None:
        _engine_simulation_slot(require_identity(request).username or "anonymous")
        return _simulate_on_engine(db, ws, engine, body, principal, principal_agent, channel,
                                   gateway_id)
    agent: Agent | None = principal_agent
    resource = tool_ref(body.resource.id)
    tool_ids: tuple[str, ...] = (body.resource.id,)
    agent_id = str(context.get("agent_id") or (agent.id if agent is not None else ""))
    if agent is None and agent_id:
        agent = _agent_or_404(db, ws, agent_id)
    ctx = request_context(body.action, channel=channel, agent_id=agent_id)
    authz = AuthzRequest(principal=principal.ref(), action=body.action, resource=resource,
                         context=ctx)
    pinned = (
        {body.policy_version.policy_id: body.policy_version.version}
        if body.policy_version is not None else None
    )
    if pinned:
        for policy_id, version in pinned.items():
            policy_store.get_version(
                db, policy_store.get_policy(db, ws.id, policy_id, engine.id), version)
    evaluation = policy_bundle.evaluate(
        db, engine, authz, principal=principal, agent=agent, tool_ids=tool_ids,
        gateway_id=gateway_id, include_drafts=body.include_drafts, pinned=pinned,
    )
    out = evaluation.as_dict()
    out["request"] = authz.as_cedar("Launchpad")
    out["gateway_id"] = gateway_id
    if body.record:
        decision = {"ALLOW": "allow", "DENY": "deny", "NO_DECISION": "error"}[evaluation.outcome]
        row = policy_store.record_decision(
            db, ws.id, source="simulate",
            channel=channel if channel in _CHANNELS else "system",
            principal_kind=principal.kind, principal_id=principal.id,
            principal_label=principal.label, action=body.action,
            resource_kind=body.resource.kind, resource_id=body.resource.id,
            agent_id=agent.id if agent is not None else None,
            session_id=context.get("session_id"), decision=decision, mode=evaluation.mode,
            policy_ids=evaluation.policy_ids,
            policy_versions=evaluation.policy_versions,
            reason=f"simulate:{evaluation.source_set}" + (
                f" {evaluation.reason}" if evaluation.reason else ""  # D20-obs1
            ) + (
                f" errors={'; '.join(evaluation.decision.errors)}"
                if evaluation.decision.errors else ""
            ),
            latency_ms=evaluation.decision.latency_ms, gateway_id=gateway_id,
            engine_id=engine.id,
        )
        out["decision_id"] = row.id
    return out


# engine simulations per user (each one runs a compile + replay on the engine); Dogwood
# Gateway publish preflights (`/validate`, either dialect) draw on the same budget
ENGINE_SIMULATIONS_PER_MINUTE = 30
_ENGINE_SIMULATIONS: dict[str, list[float]] = {}
_ENGINE_SIMULATIONS_LOCK = threading.Lock()


def _engine_simulation_slot(user: str) -> None:
    now = time.monotonic()
    with _ENGINE_SIMULATIONS_LOCK:
        recent = [t for t in _ENGINE_SIMULATIONS.get(user, []) if now - t < 60.0]
        if len(recent) >= ENGINE_SIMULATIONS_PER_MINUTE:
            _ENGINE_SIMULATIONS[user] = recent
            raise AppError(
                "policy.simulate_rate_limited",
                f"at most {ENGINE_SIMULATIONS_PER_MINUTE} engine simulations and publish "
                "preflights a minute",
                {"retry_after_s": int(60 - (now - recent[0])) + 1}, status_code=429)
        recent.append(now)
        _ENGINE_SIMULATIONS[user] = recent


def reset_engine_simulation_limits() -> None:
    with _ENGINE_SIMULATIONS_LOCK:
        _ENGINE_SIMULATIONS.clear()


def _epoch_s(at: datetime | None) -> int | None:
    if at is None:
        return None
    return int((at if at.tzinfo else at.replace(tzinfo=UTC)).timestamp())


def _simulate_on_engine(
    db: Session, ws: WorkspaceScope, engine: PolicyEngine, body: SimulateRequest,
    principal: Any, principal_agent: Agent | None, channel: str, gateway_id: str | None,
) -> dict[str, Any]:
    """The simulation on the Dogwood engine (``policy_engine_decide.simulate``) — both stages
    and temporal rules over ``engine.history``. Only while the platform installs on the engine;
    otherwise 409 ``policy.engine_not_routed``."""
    from app.services import policy_engine_decide, policy_engine_sync

    if not policy_engine_sync.enabled():
        raise AppError(
            "policy.engine_not_routed",
            "the platform does not install policies on the policy engine "
            "(Settings.policy_gateway_interceptor)", status_code=409)
    spec = body.engine
    assert spec is not None
    agent_id = spec.agent_id or (principal_agent.id if principal_agent is not None else "")
    if not agent_id:
        raise AppError("policy.invalid_simulation",
                       "a tool simulation names the calling agent (engine.agent_id)",
                       status_code=422)
    agent, tool = _agent_or_404(db, ws, agent_id), spec.tool or body.resource.id
    pinned = (
        {body.policy_version.policy_id: body.policy_version.version}
        if body.policy_version is not None else None
    )
    if pinned:
        for policy_id, version in pinned.items():
            policy_store.get_version(
                db, policy_store.get_policy(db, ws.id, policy_id, engine.id), version)
    out = policy_engine_decide.simulate(
        db, engine, principal=principal, agent=agent, tool=tool, arguments=spec.input,
        at_s=_epoch_s(spec.at), channel=channel, include_drafts=body.include_drafts,
        pinned=pinned, gateway_id=gateway_id,
        history=[{"tool": e.tool, "kind": e.kind, "input": e.input, "output": e.output,
                  "at_s": _epoch_s(e.at)} for e in spec.history],
    )
    out["engine"] = True
    out["gateway_id"] = gateway_id
    if body.action == "list_tool":
        # visibility in tools/list is the Stage-A answer, as the interceptor's list filter
        # decides it (per-user / input rules are decided at call time)
        out["final"], out["list_filter"] = out["stage_a"], True
    if body.record:
        row = policy_store.record_decision(
            db, ws.id, source="simulate",
            channel=channel if channel in _CHANNELS else "system",
            principal_kind=principal.kind, principal_id=principal.id,
            principal_label=principal.label, action=body.action,
            resource_kind=body.resource.kind, resource_id=body.resource.id,
            agent_id=agent.id, session_id=(body.context or {}).get("session_id"),
            decision=out["final"], mode=policy_bundle.gateway_mode(db, gateway_id),
            policy_ids=list(dict.fromkeys(d.split(":v", 1)[0] for d in out["determining"])),
            policy_versions=out["policy_versions"],
            reason=f"simulate:engine:{out['source_set']}"
            + (f" errors={'; '.join(out['errors'])}" if out["errors"] else ""),
            latency_ms=0.0, gateway_id=gateway_id, engine_id=engine.id,
        )
        out["decision_id"] = row.id
    return out


# ── policies of an engine ─────────────────────────────────────────────────────


@router.get(ENGINES + "/{engine_id}/policies")
def list_policies(
    engine_id: str,
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    rows = policy_store.list_policies(db, engine.id, status=status)
    return {"policies": [_policy_out(db, p) for p in rows],
            "engine": policy_engines.engine_view(db, engine)}


@router.post(ENGINES + "/{engine_id}/policies", status_code=201)
def create_policy(
    engine_id: str,
    body: PolicyCreate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy = policy_store.create_policy(
        db, engine, name=body.name, statement=body.statement, description=body.description,
        prefill=body.prefill, operator=_operator(request),
        dialect=policy_store.detect_dialect(body.statement),
        enforcement_mode=body.enforcement_mode,
    )
    return _policy_out(db, policy)


@router.get(ENGINES + "/{engine_id}/policies/{policy_id}")
def get_policy(
    engine_id: str,
    policy_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = policy_engines.get_engine(db, ws.id, engine_id, include_deleted=True)
    policy = policy_store.get_policy(db, ws.id, policy_id, engine.id)
    return policy_store.policy_view(db, policy, versions=True)


@router.get(ENGINES + "/{engine_id}/policies/{policy_id}/versions")
def list_versions(
    engine_id: str,
    policy_id: str,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = policy_engines.get_engine(db, ws.id, engine_id, include_deleted=True)
    policy = policy_store.get_policy(db, ws.id, policy_id, engine.id)
    return {"versions": [policy_store.version_view(v)
                         for v in policy_store.list_versions(db, policy)]}


@router.put(ENGINES + "/{engine_id}/policies/{policy_id}")
def update_policy(
    engine_id: str,
    policy_id: str,
    body: PolicyUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy = policy_store.update_policy(
        db, engine, policy_id, statement=body.statement, description=body.description,
        operator=_operator(request),
        dialect=policy_store.detect_dialect(body.statement) if body.statement else None,
    )
    return _policy_out(db, policy)


@router.post(ENGINES + "/{engine_id}/policies/{policy_id}/publish")
def publish_policy(
    engine_id: str,
    policy_id: str,
    body: PolicyPublish,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy = policy_store.publish_policy(
        db, ws.context, engine, policy_id, version=body.version, reason=body.reason,
        operator=_operator(request),
    )
    return _policy_out(db, policy)


@router.put(ENGINES + "/{engine_id}/policies/{policy_id}/enforcement-mode")
def set_enforcement_mode(
    engine_id: str,
    policy_id: str,
    body: EnforcementModeUpdate,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy = policy_store.set_enforcement_mode(
        db, engine, policy_id, body.enforcement_mode, operator=_operator(request))
    return _policy_out(db, policy)


@router.post(ENGINES + "/{engine_id}/policies/{policy_id}/retire")
def retire_policy(
    engine_id: str,
    policy_id: str,
    body: PolicyRetire,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy = policy_store.retire_policy(
        db, engine, policy_id, reason=body.reason, operator=_operator(request),
    )
    return _policy_out(db, policy)


@router.delete(ENGINES + "/{engine_id}/policies/{policy_id}")
def delete_policy(
    engine_id: str,
    policy_id: str,
    request: Request,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    engine = _engine_or_404(db, ws, engine_id)
    policy_store.delete_policy(db, engine, policy_id, operator=_operator(request))
    return {"deleted": True, "id": policy_id}


# ── gateway attachments (Global: Gateway.policyEngineConfiguration) ───────────


@router.get("/governance/gateways/{gateway_id}/policy-engine")
def get_gateway_policy_engine(
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    view = policy_engines.attachment_view(db, gateway_id, workspace_id=ws.id)
    if view["attached"]:
        # an attachment of another workspace's engine is not this workspace's to see
        engine = db.get(PolicyEngine, view["engine_id"])
        if engine is None or engine.workspace_id != ws.id:
            raise NotFoundError("gateway.not_found", f"gateway {gateway_id} was not found")
    return view


@router.put("/governance/gateways/{gateway_id}/policy-engine")
def put_gateway_policy_engine(
    body: GatewayPolicyEngineAttach,
    request: Request,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    """Attach (or change the engine / mode of) the ONE policy engine of a gateway. Idempotent;
    the first attach also attaches the interceptor to that gateway."""
    _cedar()
    current = policy_engines.attachment_of(db, gateway_id)
    if current is not None:
        held = db.get(PolicyEngine, current.engine_id)
        if held is not None and held.workspace_id != ws.id:
            raise AppError("gateway.policy_engine_foreign",
                           "this gateway is attached to another workspace's policy engine",
                           status_code=409)
    policy_engines.attach(db, ws.id, gateway_id, engine_id=body.engine_id, mode=body.mode,
                          operator=_operator(request))
    return policy_engines.attachment_view(db, gateway_id)


@router.delete("/governance/gateways/{gateway_id}/policy-engine")
def delete_gateway_policy_engine(
    request: Request,
    gateway_id: str = GATEWAY_ID,
    db: Session = Depends(get_db),
    ws: WorkspaceScope = Depends(require_workspace),
) -> dict[str, Any]:
    _cedar()
    current = policy_engines.attachment_of(db, gateway_id)
    if current is not None:
        held = db.get(PolicyEngine, current.engine_id)
        if held is not None and held.workspace_id != ws.id:
            raise AppError("gateway.policy_engine_foreign",
                           "this gateway is attached to another workspace's policy engine",
                           status_code=409)
    detached = policy_engines.detach(db, ws.id, gateway_id, operator=_operator(request))
    return {"gateway_id": gateway_id, "detached": detached,
            **{k: v for k, v in policy_engines.attachment_view(db, gateway_id).items()
               if k != "gateway_id"}}
