"""Enforcement points of the self-hosted Policy module (D20 plan §2.3 — node P1-b).

Global parity (user ruling 2026-10-27, 「保持和 Global 同样的策略」): policies govern Gateway
tool calls, decided at the Gateway by the interceptor + policy engine. Invoking an agent is
NOT a policy decision — the former hub invoke gate (``gate_invoke``) is gone; a hub row with
``action = invoke`` is never written again. What remains here is the hub side of that model:
the per-user principal carrier the deployers render (and whether the live version carries it —
``policy_stale``), the audit of decisions the runtime stream surfaces, and the helpers the
simulator / deployers share.

* **(b) the deploy-time policy context** — ``stage_policy(ctx, agent, spec)`` tells a deploy
  whether this version renders the per-user **principal carrier** (``principal_carrier_for``: the
  platform switch on, Gateway tools declared, not an exempt preset, a template the platform
  renders — Global model: the carrier is part of every deploy, there is no workspace switch) and
  whether the workspace Gateway's interceptor is verified attached with a published engine
  (``interceptor_decides``). Every declared Gateway tool reaches ``LAUNCHPAD_GATEWAY_TOOLS`` and
  the execution role keeps ``AgentCoreGatewayInvoke`` — nothing is narrowed at deploy time any
  more; the interceptor decides each call. ``mark_applied`` snapshots the carrier the live version
  renders (``Agent.policy_applied``), ``is_stale`` compares it with what a deploy would render NOW
  (``policy_stale`` flips only when the interceptor switch does), and ``apply()`` re-deploys
  exactly those agents (one ``deploy_agent`` update job each).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyDecision
from app.schemas.agent import AgentSpec
from app.services import (
    policy_backend,
    policy_store,
)
from app.services.policy_entities import Principal
from app.services.runtime_discovery import DISCOVERED_METHOD
from app.templates.gateway_support import gateway_tool_patterns

log = logging.getLogger("launchpad.policy")

FEATURE = "policy"

_CHANNEL_BY_KIND = {"user": "chat", "api_key": "v1", "agent": "system", "system": "system"}


def active() -> bool:
    """Whether the gate / narrowing evaluate at all: the feature is on AND an engine is
    registered. Without an engine there is nothing to decide with — the hot path is left
    exactly as it was (the Memory / Registry / KB pattern: the module is absent, not
    broken)."""
    return FEATURE in get_settings().features and policy_backend.backend_or_none() is not None


def channel_for(principal: Principal | None) -> str:
    """``chat`` for a console user, ``v1`` for an API key, ``system`` otherwise."""
    if principal is None:
        return "system"
    return _CHANNEL_BY_KIND.get(principal.kind, "system")


def is_exempt(agent: Agent) -> bool:
    """System presets (``Agent.system_key``: the architect assistant, the eval worker, the
    Skill Lab worker) are exempt from policy — unconditionally (user ruling 2026-10-27,
    contract amendment 10: no knob): they render no principal carrier and the Gateway
    interceptor passes them."""
    return bool(getattr(agent, "system_key", None))




# ── (a′) runtime guard decisions seen in a stream (P2-a) ───────────────────

# the engine stages a frame / a gateway row may name (A6 deny card)
DENY_STAGES = ("a", "bt", "final")


def describe_denial(agent: Agent, data: dict[str, Any]) -> dict[str, Any]:
    """Additive facts for the console's deny card on a ``policy_denied`` /
    ``policy_decision`` frame (A6): ``policy_name`` — the denying policy's name, the frame
    names only its id — and ``stage`` — the frame's own (``a`` / ``bt`` / ``final``), else the
    ingested gateway row's for the frame's ``decision_id`` (the gateway audit line, when the
    ingester already wrote it), else None. Never raises and never blocks the stream."""
    stage = data.get("stage") if data.get("stage") in DENY_STAGES else None
    policy_id = str(data.get("policy_id") or "").split(":", 1)[0] or None
    name: str | None = None
    if not (policy_id or (stage is None and data.get("decision_id"))):
        return {"policy_name": None, "stage": stage}
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        if policy_id:
            name = policy_store.policy_names(
                db, agent.workspace_id or "", [policy_id]).get(policy_id)
        if stage is None and data.get("decision_id"):
            row = db.get(PolicyDecision, str(data["decision_id"]))
            if (row is not None and row.source == "gateway" and row.stage in DENY_STAGES
                    and row.workspace_id == (agent.workspace_id or "")):
                stage = row.stage
    except Exception:  # noqa: BLE001 — a card fact must never break the stream
        db.rollback()
        log.exception("policy guard: could not describe the denial for agent %s", agent.id)
    finally:
        db.close()
    return {"policy_name": name, "stage": stage}

def gateway_denial_from_tool_result(agent: Agent, data: Mapping[str, Any]) -> dict[str, Any] | None:
    """N17-obs507 (Global parity): the Gateway interceptor's denial reaches the hub only as the
    agent's TOOL RESULT text — derive the ``policy_denied`` frame the console's deny card and
    the ``policy`` history row are built from. ``data`` is a normalised ``tool_result`` frame
    (``name`` / ``status`` / ``summary`` / ``id``). Adds the denying policies' NAMES from the
    policy store (``policy_name`` = the first, ``policy_names`` = ``{id: name}``; an unknown
    id names itself). Never raises; None when the result is not a Gateway denial."""
    from app.services import policy_denials

    frame = policy_denials.gateway_denial_from_tool_result(
        data.get("name"), data.get("status"), data.get("summary"), tool_use_id=data.get("id"),
    )
    if frame is None:
        return None
    ids: list[str] = list(frame.get("policy_ids") or [])
    names: dict[str, str] = {pid: pid for pid in ids}
    if ids:
        from app.core.db import SessionLocal

        db = SessionLocal()
        try:
            names.update(policy_store.policy_names(db, agent.workspace_id or "", ids))
        except Exception:  # noqa: BLE001 — a card fact must never break the stream
            db.rollback()
            log.exception("policy: could not name the denying policies for agent %s", agent.id)
        finally:
            db.close()
    frame["policy_names"] = names
    frame["policy_name"] = names.get(ids[0]) if ids else None
    # N17-obs526: the DECIDING gateway is the tool's own (the agent's Gateway ToolRef), not
    # the workspace default — an agent may mount a foreign gateway with its own engine
    from app.core.db import SessionLocal
    from app.services import policy_engines

    db = SessionLocal()
    try:
        gateway_id = deciding_gateway_id(db, agent, frame.get("tool"))
        attachment = policy_engines.attachment_of(db, gateway_id)
        frame["gateway_id"] = gateway_id or None
        frame["engine_id"] = attachment.engine_id if attachment is not None else None
    except Exception:  # noqa: BLE001 — a card fact must never break the stream
        db.rollback()
        frame.setdefault("gateway_id", None)
        frame.setdefault("engine_id", None)
    finally:
        db.close()
    return frame


# the runtime's alias prefix on a Gateway tool (`launchpad_gw_<target>___<tool>`; the Gateway
# itself names it `<target>___<tool>` — `assistant/tool_catalog.py`, `evaluation/agent_tools.py`)
GATEWAY_TOOL_ALIAS = "launchpad_gw_"


def _gateway_refs(agent: Agent) -> list[dict[str, Any]]:
    """The ``type: gateway`` ToolRefs of a stored spec that name a gateway
    (``config.gateway_id``)."""
    spec = agent.spec if isinstance(agent.spec, Mapping) else {}
    refs: list[dict[str, Any]] = []
    for tool in spec.get("tools") or []:
        if not isinstance(tool, Mapping) or tool.get("type") != "gateway":
            continue
        config = tool.get("config") if isinstance(tool.get("config"), Mapping) else {}
        gateway_id = config.get("gateway_id")
        if isinstance(gateway_id, str) and gateway_id.strip():
            refs.append({"name": str(tool.get("name") or ""), "gateway_id": gateway_id.strip(),
                         "tools": [str(t) for t in config.get("tools") or []]
                         if isinstance(config.get("tools"), list) else None})
    return refs


def deciding_gateway_id(db: Session, agent: Agent, tool: str | None) -> str:
    """N17-obs526: the Gateway that decided a tool call, for the deny card's ``gateway_id`` /
    ``engine_id`` (the 「打开策略」 link). From the AGENT's own tool configuration first:
    ``tool`` (``launchpad_gw_<target>___<tool>`` or ``<target>___<tool>``) names its target;
    the Gateway ToolRef named ``<target>`` — or whose declared ``config.tools`` lists the
    ``<target>___<tool>`` name — gives ``config.gateway_id``. No match but exactly one Gateway
    ToolRef ⇒ that one. Otherwise the workspace's own gateway (the pre-obs526 behaviour; ""
    when not bootstrapped)."""
    from app.services import policy_engines

    refs = _gateway_refs(agent)
    name = str(tool or "").strip()
    if name.startswith(GATEWAY_TOOL_ALIAS):
        name = name[len(GATEWAY_TOOL_ALIAS):]
    target = name.partition("___")[0] if "___" in name else ""
    if refs and name:
        for ref in refs:
            if (target and ref["name"] == target) or (ref["tools"] and name in ref["tools"]):
                return ref["gateway_id"]
    if len(refs) == 1:
        return refs[0]["gateway_id"]
    return policy_engines.workspace_gateway(db, agent.workspace_id or "")[0]


# The hub-visible guard events (`services/agentcore/runtime.py` whitelists them):
# `policy_denied` = the tool was refused (mode enforce); `policy_decision` = a
# `would_deny` observation (mode log_only — the tool ran). Both become one
# `policy_decisions` row with `source="runtime"`; nothing flows runtime → hub except the
# event already in the stream (no network from the runtime to the hub).
RUNTIME_DECISION_EVENTS = ("policy_denied", "policy_decision")


def record_runtime_decision(
    agent: Agent, principal: Principal | None, data: dict[str, Any], *,
    session_id: str | None = None, channel: str | None = None,
) -> str | None:
    """The audit row for one guard decision the runtime surfaced in its stream
    (plan §2.3(c): same record shape, ``source=runtime``, channel per entry point —
    ``chat`` / ``v1`` / ``system``). Never raises; returns the row id (the card's
    ``decision_id``) or None. Written only while the module is active — an event from a
    runtime deployed under a guard that is now off is still audited if the engine is
    registered (the row is the system of record), skipped otherwise. A frame with
    ``source: "gateway"`` (the rendered template re-emitting an I8 Gateway denial,
    ``runtime_policy_denied_frame`` contract) writes NO row — the gateway line is the system
    of record; the frame only flows, with the id the Gateway gave it (may be None)."""
    if data.get("source") == "gateway":
        decision_id = data.get("decision_id")
        return str(decision_id) if decision_id else None
    if not active():
        return None
    who = principal or Principal.system()
    channel = channel or channel_for(who)
    tool = str(data.get("tool") or "")
    decision = str(data.get("decision") or "deny")
    if decision not in ("deny", "would_deny"):
        decision = "deny"
    reason = str(data.get("reason") or "")[:2000] or None
    policy_id = data.get("policy_id")
    if not policy_id and reason:
        from app.services import policy_denials

        policy_id = policy_denials.determining_policy_id(reason)
    bundle_version = data.get("bundle_version")
    try:
        bundle_version = int(bundle_version) if bundle_version is not None else None
    except (TypeError, ValueError):
        bundle_version = None
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        mode = str(data.get("mode") or gateway_mode(
            db, agent.workspace_id or "", gateway_id=deciding_gateway_id(db, agent, tool)))
        row = policy_store.record_decision(
            db, agent.workspace_id or "", source="runtime", channel=channel,
            principal_kind=who.kind, principal_id=who.id, principal_label=who.label or who.id,
            action="call_tool", resource_kind="tool", resource_id=tool or "?",
            agent_id=agent.id, session_id=session_id,
            request_id=str(data.get("tool_use_id") or "") or None, decision=decision,
            mode=mode, policy_ids=[str(policy_id)] if policy_id else [],
            policy_versions={}, bundle_version=bundle_version, reason=reason,
        )
        return row.id
    except Exception:  # noqa: BLE001 — the audit sink must never break the stream
        db.rollback()
        log.exception("policy guard: could not record the runtime %s for agent %s",
                      decision, agent.id)
        return None
    finally:
        db.close()


# ── (b) the deploy-time policy context: the principal carrier ───────────────


@dataclass
class DeployPolicy:
    """What one deploy renders for the per-user policy (Global parity, 2026-10-27: no
    deploy-time narrowing, no runtime guard — the Gateway interceptor decides every call).

    ``carrier``: this version renders the principal carrier (the turn's token into the
    Gateway ``_meta``, the sequential tool executor); ``interceptor_decides``: the carrier AND
    the workspace Gateway's interceptor verified attached; ``declared``: the spec's Gateway
    tool patterns — ALL of them reach the runtime; ``mode``: the workspace's gateway
    enforcement mode (the bundle's; informational here); ``active``: the module is on and the
    mode is not ``off``. ``reason``: ``policy_inactive`` | ``mode_off`` | ``system_exempt`` |
    ``no_gateway`` | None."""

    active: bool
    mode: str
    declared: list[str] = field(default_factory=list)
    carrier: bool = False
    interceptor_decides: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_snapshot(cls, data: Mapping[str, Any]) -> DeployPolicy:
        """Rebuild from an ``as_dict`` snapshot that went through JSON (the canary stores the
        candidate's on ``setup.deploy_policy``); unknown keys from an older writer (the retired
        narrowing's ``patterns`` / ``guard`` …) are dropped, missing ones take the defaults."""
        known = {f.name for f in fields(cls)}
        out = {k: v for k, v in data.items() if k in known}
        out.setdefault("active", bool(data.get("active", False)))
        out.setdefault("mode", str(data.get("mode") or "off"))
        return cls(**out)

    def summary(self) -> str:
        if not self.active:
            return f"policy: not evaluated ({self.reason or 'inactive'})"
        if self.reason == "system_exempt":
            return "policy: system preset — exempt from the per-user carrier"
        if not self.declared:
            return f"policy: {self.mode} · no Gateway tools"
        where = ("the Gateway interceptor decides every call" if self.interceptor_decides
                 else "the Gateway decides every call once its interceptor is attached")
        return (f"policy: {self.mode} · {len(self.declared)} Gateway tool pattern(s) rendered · "
                f"principal carrier {'on' if self.carrier else 'off'} · {where}")


def _declared_patterns(spec: dict[str, Any] | None) -> list[str]:
    """``gateway_tool_patterns`` over a stored spec dict (no pydantic — the listener scans
    every agent of a workspace)."""
    patterns: list[str] = []
    for tool in (spec or {}).get("tools") or []:
        if not isinstance(tool, dict) or tool.get("type") != "gateway":
            continue
        declared = (tool.get("config") or {}).get("tools")
        patterns.extend(
            [str(t) for t in declared] if isinstance(declared, list)
            else [f"{tool.get('name')}___*"]
        )
    return list(dict.fromkeys(patterns))


def interceptor_enabled(db: Session, workspace_id: str) -> bool:
    """Per-user principal: the Gateway interceptor path (token mint, ``_meta`` injection) is on
    for every workspace while the platform switch ``Settings.policy_gateway_interceptor`` is
    (Global model: no workspace switch — the carrier is part of every deploy; whether a call is
    decided is the gateway attachment's business)."""
    return bool(get_settings().policy_gateway_interceptor)


def gateway_mode(db: Session, workspace_id: str, *, gateway_id: str | None = None) -> str:
    """The Gateway's attachment mode as the deploy log / audit rows spell it: ``enforce`` /
    ``log_only``, or ``off`` when no engine is attached to it. ``gateway_id`` = a specific
    gateway (N17-obs526: the one that decided a call); default = the workspace's own."""
    from app.services import policy_engines

    if gateway_id is None:
        gateway_id = policy_engines.workspace_gateway(db, workspace_id)[0]
    engine_row = policy_engines.attachment_of(db, gateway_id)
    if engine_row is None:
        return "off"
    return "enforce" if engine_row.mode == "ENFORCE" else "log_only"


def interceptor_decides(db: Session, agent: Agent, spec: AgentSpec | None = None, *,
                        carrier: tuple[bool, bool] | None = None) -> bool:
    """THE rule for "the Gateway interceptor decides this agent's per-user tool rules": this
    deploy renders the principal carrier AND the workspace Gateway's interceptor is verified
    attached AND the hub has published the workspace to it — the engine holds the hub's
    CURRENT set and the bundle write after the install succeeded
    (``policy_gateway_publish.interceptor_governs``; review F1, N17-obs490). Shared by the
    runtime guard (disarmed only then) and the engine's deploy-time narrowing (Stage A only
    then; otherwise the full narrowing, review A6 D2), so the two can never disagree.
    ``carrier`` = the deploy job's decision."""
    if carrier is not None:
        return bool(carrier[1])
    return carrier_decision(db, agent, spec if spec is not None else _spec_or_none(agent))[1]


def carrier_decision(db: Session, agent: Agent, spec: AgentSpec | None) -> tuple[bool, bool]:
    """``(renders the principal carrier, the interceptor decides)`` for one deploy: the
    second is the first AND the workspace Gateway's interceptor verified attached AND the
    workspace published to it with its bundle written (review F1, N17-obs490)."""
    if spec is None or not principal_carrier_for(db, agent, spec):
        return False, False
    from app.models.ledger import Workspace
    from app.services import policy_gateway_publish, policy_gateway_state

    row = db.get(Workspace, agent.workspace_id or "")
    gateway_id = str(((row.resources if row else None) or {}).get("gateway_id") or "")
    return True, (policy_gateway_state.gateway_interceptor_attached(db, gateway_id)
                  and policy_gateway_publish.interceptor_governs(db, agent.workspace_id or ""))


def job_carrier_decision(ctx: Any, agent: Agent, spec: AgentSpec) -> tuple[bool, bool]:
    """``carrier_decision`` once per deploy job (``ctx.scratch``): the narrowing (guard
    armed or not) and the renderer (carrier or not) read the SAME answer, so a flag or an
    attach that changes mid-job can never disarm the guard of an entrypoint without the
    carrier (re-review nit b)."""
    cached = ctx.scratch.get("principal_carrier_decision")
    if isinstance(cached, tuple) and len(cached) == 2:
        return cached
    db = ctx.session()
    try:
        decision = carrier_decision(db, agent, spec)
    finally:
        db.close()
    ctx.scratch["principal_carrier_decision"] = decision
    return decision


def _spec_or_none(agent: Agent) -> AgentSpec | None:
    try:
        return AgentSpec.model_validate(agent.spec or {})
    except Exception:  # noqa: BLE001 — an unparseable legacy spec keeps the guard as today
        return None


def principal_carrier_for(db: Session, agent: Agent, spec: AgentSpec) -> bool:
    """Stream A3: whether this deploy renders the principal carrier (the token into the
    Gateway ``_meta``) and the sequential tool executor (T10). On for a governed agent — the
    interceptor on for its workspace, Gateway tools declared, not an exempt preset — whose
    entrypoint is the shared Strands template (a plain ``zip_runtime``: not a converted
    bundle, not A2A) or the self-built Harness bundle. Studio artifacts carry no Gateway
    client; converted bundles and BYOC carry the token through their own code (the BYOC
    helper)."""
    if is_exempt(agent) or not gateway_tool_patterns(spec):
        return False
    method = getattr(agent, "method", None) or spec.method
    if method == "harness":  # the self-built Harness bundle (A3b)
        return interceptor_enabled(db, agent.workspace_id or "")
    if method != "zip_runtime" or spec.code_bundle or spec.protocol == "a2a":
        return False
    return interceptor_enabled(db, agent.workspace_id or "")


def deploy_policy_for(
    db: Session, agent: Agent, spec: AgentSpec, *, workspace_id: str | None = None,
    carrier: tuple[bool, bool] | None = None,
) -> DeployPolicy:
    """The deploy-time policy context of one deploy: the declared Gateway patterns (all
    rendered) and the carrier decision (``carrier`` = the job's cached ``carrier_decision``,
    else read now). ``mode`` = the workspace Gateway's attachment mode (``off`` = no engine
    attached: the gateway is unguarded; the carrier is still rendered — reason ``mode_off`` is
    informational). Never raises on policy state: nothing here can fail a deploy."""
    declared = gateway_tool_patterns(spec)
    out = DeployPolicy(active=False, mode="off", declared=declared, reason="policy_inactive")
    if not active():
        return out
    ws = workspace_id or agent.workspace_id or ""
    out.mode = gateway_mode(db, ws)
    out.active = True
    if is_exempt(agent):
        out.reason = "system_exempt"
        return out
    if not declared:
        out.reason = "no_gateway"
        return out
    if out.mode == "off":
        out.reason = "mode_off"
    decision = carrier if carrier is not None else carrier_decision(db, agent, spec)
    out.carrier, out.interceptor_decides = bool(decision[0]), bool(decision[1])
    return out


def stage_policy(ctx: Any, agent: Agent, spec: AgentSpec) -> DeployPolicy:
    """The deployers' entry point: computed once per job (``ctx.scratch``), recomputed on a
    resumed job (scratch is process-local), logged to the job."""
    cached = ctx.scratch.get("deploy_policy")
    if isinstance(cached, DeployPolicy):
        return cached
    carrier = job_carrier_decision(ctx, agent, spec)
    db = ctx.session()
    try:
        policy = deploy_policy_for(db, agent, spec, workspace_id=ctx.workspace.id,
                                   carrier=carrier)
    finally:
        db.close()
    ctx.scratch["deploy_policy"] = policy
    ctx.log(policy.summary())
    return policy


def mark_applied(
    db: Session, agent: Agent, policy: DeployPolicy | None = None, *,
    job_id: str | None = None, operator: str | None = None, carrier: bool | None = None,
    interceptor: bool | None = None,
) -> None:
    """Deploy stage succeeded on the live version: snapshot what it renders onto the row
    (``policy_applied`` — what ``is_stale`` compares against, so the stale badge clears) and
    journal ``bundle.apply``. ``carrier``: whether the live version renders the principal
    carrier — the deploy job's own decision (``zip_runtime``), else, for a caller that does
    not know it, the module rule now (``_carrier_now``: what ``is_stale`` compares against), so
    such a caller never leaves the agent falsely stale. ``interceptor``: whether the Gateway's
    interceptor was verified attached at that deploy (informational since Global parity)."""
    if carrier is None:
        carrier = policy.carrier if policy is not None else _carrier_now(db, agent)
    if interceptor is None:
        interceptor = (policy.interceptor_decides if policy is not None
                       else bool(carrier) and interceptor_decides(db, agent))
    mode = policy.mode if policy is not None else gateway_mode(db, agent.workspace_id or "")
    applied: dict[str, Any] = {
        "mode": mode,
        "applied_at": datetime.now(UTC).isoformat(),
        "job_id": job_id,
    }
    if carrier:  # stream A: the live version renders the principal carrier (absent = no)
        applied["carrier"] = True
    if interceptor:  # the live version was deployed with the interceptor verified attached
        applied["interceptor_decides"] = True
    agent.policy_applied = applied
    if (policy is None or (policy.active and policy.reason != "system_exempt")) \
            and not is_exempt(agent) and active():
        policy_store.journal(
            db, agent.workspace_id or "", "bundle.apply", operator=operator or "deployer",
            requested={"agent_id": agent.id, "job_id": job_id, "mode": mode},
            after={"carrier": bool(carrier), "interceptor_decides": bool(interceptor)},
        )
    db.commit()


# ── stale: derived, never stored ───────────────────────────────────────────


def is_stale(db: Session, agent: Agent) -> bool:
    """``policy_stale`` (derived on read, never stored): the principal carrier a deploy would
    render NOW (``_carrier_now`` — the interceptor switches, the spec's Gateway tools) differs
    from the one the live version renders (``policy_applied.carrier``). Global parity: a
    publish, a retire or a mode change never makes an agent stale — policies apply at call
    time at the Gateway; only the interceptor switch flipping does. Only agents the carrier
    can touch qualify — a gateway ToolRef, not a system preset, not a discovered runtime."""
    if not active() or is_exempt(agent) or agent.method == DISCOVERED_METHOD:
        return False
    if not _declared_patterns(agent.spec):
        return False
    applied = agent.policy_applied
    return _carrier_now(db, agent) != bool(isinstance(applied, dict) and applied.get("carrier"))


def _carrier_now(db: Session, agent: Agent) -> bool:
    """Whether the next deploy of ``agent`` renders the principal carrier — ``False``, without
    a query, while the global interceptor switch is off (flag-off staleness is unchanged)."""
    if not get_settings().policy_gateway_interceptor:
        return False
    spec = _spec_or_none(agent)
    return spec is not None and principal_carrier_for(db, agent, spec)


def stale_flag(agent: Agent) -> bool:
    """``_agent_view``'s ``policy_stale`` — False (and no ledger read) while the policy
    module is inactive."""
    if not active():
        return False
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return is_stale(db, agent)
    finally:
        db.close()


def carrier_flag(agent: Agent) -> bool | None:
    """``_agent_view``'s ``policy_carrier``: whether the LIVE version renders the principal
    carrier (``policy_applied.carrier``), ``None`` until a deploy under the policy module
    stamped the row. A pure read of the row — no ledger query."""
    applied = agent.policy_applied
    if not isinstance(applied, dict) or not applied.get("applied_at"):
        return None
    return bool(applied.get("carrier"))


def stale_agent_ids(db: Session, workspace_id: str) -> list[str]:
    """The carrier-stale agents of a workspace (``apply``'s default set; informational since the
    Global model — the carrier flips only with the platform switch)."""
    if not active():
        return []
    rows = db.scalars(
        select(Agent).where(Agent.workspace_id == workspace_id, Agent.status != "deleted")
        .order_by(Agent.created_at, Agent.id)
    ).all()
    return [a.id for a in rows if is_stale(db, a)]


def validate_spec_policy_ids(db: Session, workspace_id: str, spec: AgentSpec) -> None:
    """``AgentSpec.policy_ids`` must name policies of THIS workspace (plan §4.2) — INERT since
    the Global model (the retired agent scope selects nothing); kept so a stored spec that
    pins ids still validates, an unknown or foreign id is 422 ``policy.unknown_policy``."""
    if not spec.policy_ids:
        return
    known = set(db.scalars(
        select(Policy.id).where(
            Policy.workspace_id == workspace_id, Policy.id.in_(list(spec.policy_ids)),
        )
    ).all())
    missing = [pid for pid in spec.policy_ids if pid not in known]
    if missing:
        raise AppError(
            "policy.unknown_policy",
            "policy_ids name policies that do not exist in this workspace",
            {"policy_ids": missing}, status_code=422,
        )


def apply(
    db: Session, workspace: Any, *, agent_ids: list[str] | None = None,
    operator: str | None = None,
) -> list[dict[str, Any]]:
    """Re-deploy the carrier-stale agents (or exactly ``agent_ids``) so the live Runtime version
    renders the principal carrier the platform switch now asks for (no route since the Global
    model — ``POST /api/policies/apply`` is 410; operators redeploy from the agent page) — one
    ``deploy_agent`` update job per agent
    (``skip_register``: identity is unchanged), tagged ``payload.policy_apply``. An agent that
    cannot be re-deployed right now (deploying, system preset, discovered runtime, unparseable
    spec) is listed with ``job_id: null`` and a ``skipped`` reason. Returns
    ``[{agent_id, job_id, carrier, skipped?}]``."""
    from app.deployer.pipeline import create_deployment, start_deploy_async

    workspace_id = workspace.id
    query = select(Agent).where(Agent.workspace_id == workspace_id, Agent.status != "deleted")
    if agent_ids is not None:
        rows = db.scalars(query.where(Agent.id.in_(list(agent_ids)))).all()
        found = {a.id for a in rows}
        unknown = [a for a in agent_ids if a not in found]
        if unknown:
            raise NotFoundError("agent.not_found", f"agent not found: {', '.join(unknown)}")
    else:
        stale = stale_agent_ids(db, workspace_id)
        rows = db.scalars(query.where(Agent.id.in_(stale))).all() if stale else []
    rows = sorted(rows, key=lambda a: (a.created_at or datetime.min.replace(tzinfo=UTC), a.id))
    out: list[dict[str, Any]] = []
    for agent in rows:
        entry: dict[str, Any] = {"agent_id": agent.id, "job_id": None, "carrier": False}
        skipped = _apply_skip_reason(db, agent)
        spec: AgentSpec | None = None
        if skipped is None:
            try:
                spec = AgentSpec(**agent.spec)
            except Exception as exc:  # noqa: BLE001 — a legacy spec that no longer validates
                skipped = f"spec does not validate: {type(exc).__name__}"
        if skipped or spec is None:
            entry["skipped"] = skipped or "spec does not validate"
            out.append(entry)
            continue
        policy = deploy_policy_for(db, agent, spec, workspace_id=workspace_id)
        agent.status = "deploying"
        agent.error = None
        agent.updated_at = datetime.now(UTC)
        db.flush()
        _, job = create_deployment(
            db, agent, mode="update", skip_register=True,
            payload_extra={"policy_apply": {"mode": policy.mode, "carrier": policy.carrier,
                                            "operator": operator}},
        )
        start_deploy_async(job.id)
        entry.update(job_id=job.id, carrier=policy.carrier)
        out.append(entry)
    if out:  # an apply that found nothing to touch leaves no journal row
        policy_store.journal(
            db, workspace_id, "bundle.apply", operator=operator,
            requested={"agent_ids": list(agent_ids) if agent_ids is not None else None,
                       "stale_only": agent_ids is None},
            after={"agents": [{"agent_id": e["agent_id"], "job_id": e["job_id"],
                               "skipped": e.get("skipped")} for e in out]},
        )
    db.commit()
    return out


def _apply_skip_reason(db: Session, agent: Agent) -> str | None:
    from app.services.canary_references import blocking_canaries

    if is_exempt(agent):
        return "system preset — exempt from the per-user carrier"
    if agent.method == DISCOVERED_METHOD:
        return "discovered runtime — externally owned"
    if agent.status == "deploying":
        return "a deployment is already in progress"
    if blocking_canaries(db, agent.id):
        # N17-obs16: the bulk re-publish honours the canary's ownership of the base the way
        # the redeploy route does (409 agent.in_use there) — listed, not re-deployed
        return "a Runtime canary is running on this agent — settle it first"
    if not agent.resource_id and agent.status != "active":
        return f"agent is {agent.status} — nothing is deployed to re-render"
    return None


def apply_summary(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """The ``publish.apply`` block: ``{agents[], job_ids[]}`` over the started jobs."""
    started = [e for e in entries if e.get("job_id")]
    return {"agents": [e["agent_id"] for e in started],
            "job_ids": [e["job_id"] for e in started]}
