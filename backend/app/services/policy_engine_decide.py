"""Hub decisions on the Dogwood engine (stream A6, engine side; design §15.2, contract I4 in
docs/cn/interfaces/policy-principal-contracts.md; amendment 4 — the invoke item — is retired
by amendment 9: invoking an agent is not a policy decision, only Gateway tool calls are).

``policy_bundle.evaluate`` hands the simulator's ``call_tool`` decision here instead of to
cedarpy when the Dogwood engine holds the POLICY ENGINE's current set
(``policy_engine_sync.engine_decides``; Global model — the set key is the engine id). This
module only builds the I4 tool items, asks I5 ``authorize_stateless`` (any healthy shard —
``policy_engine_control``) and maps the answers back onto the hub's own ``Evaluation`` /
``AuthzDecision``. It never falls back: an engine that cannot answer raises
``policy_engine_control.EngineControlError`` and the simulator reports it.

``simulate`` is the console simulator's engine path: the chosen set (ACTIVE, drafts or one
pinned version) is compiled and decided by the engine's ``replay`` over a hand-written
history, so temporal rules are simulated too.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy.orm import Session

from app.models.ledger import Agent
from app.models.policy import PolicyEngine
from app.policy import principal_entities as pe
from app.policy.contracts import codec
from app.services import policy_engine_control, policy_store
from app.services.policy_backend import AuthzDecision
from app.services.policy_entities import Principal

log = logging.getLogger("launchpad.policy")
# where the hub mints no policy session (contract amendment 4)
NO_PSID = "none"
EVALUATION_ERROR = "policy_evaluation_error"


def _engine(db: Session, engine: PolicyEngine | str) -> PolicyEngine:
    """A ``PolicyEngine`` row, an engine id, or a workspace id (→ its default engine)."""
    from app.services import policy_store

    return policy_store._engine_arg(db, engine)


def engine_decides(db: Session, engine: PolicyEngine | str) -> bool:
    """THE routing switch of the hub's own engine consumers: the Dogwood engine decides only
    when it holds the hub's CURRENT ACTIVE set of ``engine`` (``policy_engine_sync``)."""
    from app.services import policy_engine_sync

    return policy_engine_sync.engine_decides(db, _engine(db, engine))


def role_arn(agent: Agent) -> str:
    """The agent's execution role ARN — the ``IamEntity`` the interceptor resolves it to
    (the Gateway bundle's ``role_map`` is keyed by the same ``agent_iam.role_name_for``)."""
    from app.core.config import get_settings
    from app.services import agent_iam
    from app.services.workspace import context_for_workspace

    name = agent_iam.role_name_for(agent.name, agent.id, get_settings().env_name or None)
    return context_for_workspace(agent.workspace_id or "").arn("iam", f"role/{name}",
                                                                region="")


def agent_block(agent: Agent) -> dict[str, Any]:
    return pe.agent_descriptor(role_arn(agent), agent.id, method=agent.method or "",
                               system=bool(agent.system_key), workspace_id=agent.workspace_id)


def principal_object(principal: Principal, agent: Agent | None) -> dict[str, Any]:
    """The I4 ``principal`` of a hub principal. An agent principal (the simulator's Stage-A
    question) is its own ``IamEntity`` in its ``Agent``, as for a token-less system agent."""
    if principal.kind == "agent":
        if agent is None:
            raise ValueError("an agent principal needs its agent")
        return {"entity": pe.ref("IamEntity", role_arn(agent)),
                "parents": [pe.ref("Agent", agent.id)], "attrs": {"agent_id": agent.id}}
    claim: dict[str, Any] = {"kind": principal.kind, "id": principal.id,
                             "label": principal.label or principal.id}
    if principal.kind == "user":
        claim["role"] = principal.role or principal.attrs.get("role")
        if principal.attrs.get("user_id"):
            claim["user_id"] = principal.attrs["user_id"]
    elif principal.kind == "api_key":
        claim["label"] = principal.attrs.get("name") or principal.label or principal.id
    return pe.principal_descriptor(claim)


def expect(engine: PolicyEngine) -> dict[str, Any]:
    """The set the hub believes is installed — a stale hub gets 409 ``set_mismatch``."""
    return {"policy_set_version": f"ps-{engine.policy_set_seq}",
            "temporal_set_version": int(engine.temporal_set_version)}


def session_key(workspace_id: str, principal: Principal, *, session_id: str | None,
                tsv: int) -> str:
    psid = codec.psid_for(workspace_id, session_id, tsv) if session_id else NO_PSID
    kind = "user" if principal.kind == "user" else principal.kind
    return codec.session_key(workspace_id, psid, kind, principal.id)


def gateway_resource(db: Session, workspace_id: str, gateway_id: str | None = None) -> str:
    """The Gateway the interceptor names as the resource of every tool call: ``gateway_id``
    (the simulated gateway) else the workspace's own."""
    from app.services import policy_engines

    if gateway_id:
        return pe.gateway_resource(policy_engines.gateway_facts(
            db, gateway_id, workspace_id=workspace_id)["gateway_arn"])
    own_id, own_arn = policy_engines.workspace_gateway(db, workspace_id)
    if not own_arn:
        from app.services.workspace import context_for_workspace

        own_arn = context_for_workspace(workspace_id).arn(
            "bedrock-agentcore", f"gateway/{own_id or 'unknown'}")
    return pe.gateway_resource(str(own_arn))


def tool_item(db: Session, engine: PolicyEngine, principal: Principal, agent: Agent,
              tool_id: str, *, channel: str, arguments: dict[str, Any] | None = None,
              session_id: str | None = None, resource: str | None = None,
              gateway_id: str | None = None) -> dict[str, Any]:
    exp = expect(engine)
    return {
        "ws": engine.id, "record": False, "action": pe.ref("Action", tool_id),
        "session": session_key(engine.workspace_id, principal, session_id=session_id,
                               tsv=exp["temporal_set_version"]),
        "resource": resource or gateway_resource(db, engine.workspace_id, gateway_id),
        "agent": agent_block(agent),
        "principal": principal_object(principal, agent),
        "context": {"input": dict(arguments or {}), "agent_id": agent.id, "channel": channel,
                    "expect": exp},
    }


def _stamps(answer: dict[str, Any], stage: str | None = None) -> list[str]:
    return [f"{d['policy_id']}:v{d['version']}" for d in answer.get("determining") or []
            if stage is None or d.get("stage") == stage]


def _decision(answer: dict[str, Any], *, stage_a_only: bool, latency_ms: float,
              engine_id: str | None = None) -> AuthzDecision:
    """One engine answer as the hub's ``AuthzDecision``. An item the engine refused
    (``{status, code}``: 409 ``set_mismatch`` …) raises — the hub's view is not the engine's,
    so neither answer may be used. An evaluation error is the engine's fail-closed deny and
    stays a deny (with the error), exactly as at the Gateway."""
    if "decision" not in answer:
        code = str(answer.get("code") or "refused")
        if code == "set_mismatch" and engine_id:
            schedule_reconcile(engine_id)
        raise policy_engine_control.EngineControlError(
            code, str(answer.get("message") or "item refused"))
    verdict = answer["stage_a"] if stage_a_only else answer["decision"]
    errors = [str(e) for e in answer.get("errors") or []]
    if stage_a_only:
        errors = [e for e in errors if not e.startswith("temporal policies not evaluated")]
    return AuthzDecision("allow" if verdict == "allow" else "deny",
                         reasons=_stamps(answer, "a" if stage_a_only else None),
                         errors=errors, latency_ms=latency_ms)


def deny_stage(answer: dict[str, Any]) -> str | None:
    """The engine stage that denied (A6, the console's deny card): ``a`` when stage A denied,
    else ``bt`` (the temporal stage); ``None`` for an allow."""
    if answer.get("decision") != "deny":
        return None
    return "a" if answer.get("stage_a") == "deny" else "bt"


_RECONCILE_AFTER: dict[str, float] = {}
RECONCILE_INTERVAL_S = 60.0


def schedule_reconcile(engine_id: str) -> bool:
    """After a 409 ``set_mismatch`` (the Dogwood engine holds another set than the hub's ledger
    — an install whose commit failed, a hand install): the decision fails closed and a
    background ``reconcile_engine`` re-installs the hub's set, at most once a minute per
    policy engine."""
    import threading

    now = time.monotonic()
    if _RECONCILE_AFTER.get(engine_id, 0.0) > now:
        return False
    _RECONCILE_AFTER[engine_id] = now + RECONCILE_INTERVAL_S

    def run() -> None:
        from app.core.db import SessionLocal
        from app.services import policy_engine_sync

        db = SessionLocal()
        try:
            engine = db.get(PolicyEngine, engine_id)
            if engine is not None and policy_engine_sync.reconcile_engine(db, engine) is not None:
                db.commit()
        except Exception:  # noqa: BLE001 — best effort; the next mismatch retries
            db.rollback()
            log.exception("policy engine reconcile failed for engine %s", engine_id)
        finally:
            db.close()

    threading.Thread(target=run, name=f"policy-reconcile-{engine_id}", daemon=True).start()
    return True


def _versions(stamps: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for stamp in stamps:
        pid, _, version = stamp.partition(":v")
        if pid and version.split("#", 1)[0].isdigit():
            out[pid.split("#", 1)[0]] = int(version.split("#", 1)[0])
    return out


def evaluate_tool(db: Session, engine: PolicyEngine | str, principal: Principal, agent: Agent,
                  tool_id: str, *, channel: str, arguments: dict[str, Any] | None = None,
                  mode: str | None = None, gateway_id: str | None = None):
    """``policy_bundle.evaluate`` for one tool call (stateless; temporal rules are decided at
    the Gateway, and the answer says so in ``errors``)."""
    from app.services.policy_bundle import Evaluation, gateway_mode

    engine = _engine(db, engine)
    item = tool_item(db, engine, principal, agent, tool_id, channel=channel,
                     arguments=arguments, gateway_id=gateway_id)
    started = time.perf_counter()
    (answer,) = policy_engine_control.authorize_stateless(engine.id, [item])
    decision = _decision(answer, stage_a_only=False,
                         latency_ms=(time.perf_counter() - started) * 1000,
                         engine_id=engine.id)
    return Evaluation(
        decision=decision, mode=mode or gateway_mode(db, gateway_id),
        policy_versions=_versions(decision.reasons), source_set="active",
        determining=list(decision.reasons), stage=deny_stage(answer),
    )


# ── the console simulator on the engine (temporal included) ─────────────────────────


def simulation_policies(
    db: Session, engine: PolicyEngine, *, include_drafts: bool = False,
    pinned: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int], str]:
    """``[{id, version, mode, statement}]`` for the engine compiler, chosen exactly like the
    cedarpy simulator chooses (``policy_bundle.effective_subset``): ACTIVE, every draft, or one
    pinned version on top. D20 statements are rewritten to the Global vocabulary
    (``engine_statement``)."""
    from app.services import policy_engine_sync

    out, versions = [], {}
    for policy in policy_store.list_policies(db, engine.id):
        version: int | None = policy.active_version if policy.status == "active" else None
        if include_drafts and policy.draft_version is not None:
            version = policy.draft_version
        if pinned and policy.id in pinned:
            version = pinned[policy.id]
        if version is None:
            continue
        row = policy_store.get_version(db, policy, version)
        statement, _warnings = policy_engine_sync.engine_statement(
            row.statement, getattr(row, "dialect", None))
        out.append({"id": policy.id, "version": int(version),
                    "mode": policy.enforcement_mode or "active", "statement": statement})
        versions[policy.id] = int(version)
    source = "pinned" if pinned else ("draft" if include_drafts else "active")
    return out, versions, source


def simulate(
    db: Session, engine: PolicyEngine | str, *, principal: Principal, agent: Agent,
    tool: str, arguments: dict[str, Any] | None = None, at_s: int | None = None,
    history: list[dict[str, Any]] | None = None, channel: str = "chat",
    include_drafts: bool = False, pinned: dict[str, int] | None = None,
    gateway_id: str | None = None,
) -> dict[str, Any]:
    """One simulated decision after ``history`` on the engine's own code path (``replay``):
    ``{stage_a, stage_bt, final, determining[], errors[], trace[], source_set,
    policy_versions, warnings[]}`` for one ``tools/call`` of ``tool`` by ``agent``. History events
    are ``{tool, kind: request|response|error, principal?, input, output, at_s}`` (a missing
    principal is the simulated one). Raises ``AppError`` 422 when the set does not compile."""
    from app.core.errors import AppError
    from app.services import policy_engine_sync

    engine = _engine(db, engine)
    policies, versions, source = simulation_policies(
        db, engine, include_drafts=include_drafts, pinned=pinned)
    who = principal_object(principal, agent)
    now = int(at_s if at_s is not None else time.time())
    events = []
    resource = gateway_resource(db, engine.workspace_id, gateway_id)
    for e in history or []:
        events.append({
            "tool": str(e["tool"]), "kind": str(e.get("kind") or "request"),
            "principal": e.get("principal_object") or who,
            "input": dict(e.get("input") or {}), "output": dict(e.get("output") or {}),
            "at_s": int(e["at_s"]),
            "resource": resource,
        })
    request: dict[str, Any] = {"tool": tool, "kind": "request", "principal": who,
                               "input": dict(arguments or {}), "at_s": now,
                               "resource": resource}
    out = policy_engine_sync.compiler().replay(
        policy_engine_sync.engine_tools_list(db, engine), policies, agent=agent_block(agent),
        request=request, history=events, channel=channel, ws=engine.id)
    if not out.get("ok"):
        raise AppError("policy.invalid_statement", "the simulated set does not compile",
                       {"errors": list(out.get("errors") or [])}, status_code=422)
    answer = out.get("decision") or {}
    if "decision" not in answer:
        raise AppError("policy.invalid_simulation", "the engine refused the simulated request",
                       {"code": answer.get("code"), "message": answer.get("message")},
                       status_code=422)
    return {
        "stage_a": answer["stage_a"], "stage_bt": answer["stage_bt"],
        "final": answer["decision"], "log_only_would": answer.get("log_only_would"),
        "determining": _stamps(answer), "errors": list(answer.get("errors") or []),
        "trace": list(out.get("trace") or []), "source_set": source,
        "policy_versions": versions, "warnings": list(out.get("warnings") or []),
    }
