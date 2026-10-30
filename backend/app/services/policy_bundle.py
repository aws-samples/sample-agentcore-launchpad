"""The simulator's decision path and the gateway bundle builder / writer (Global model,
``docs/cn/interfaces/policy-engines.md``).

``evaluate`` answers one ``(principal, action, resource, context)`` question against a POLICY
ENGINE's compiled ACTIVE set (cached in ``policy_cedar``) or, for the simulator, against drafts /
one pinned version (uncached). Only the simulator (``POST /api/policy-engines/{id}/simulate``)
and the engine-parity checks go through here — decisions on traffic are made by the Gateway
interceptor + Dogwood engine (Global parity, 2026-10-27); there is no hub invoke gate and no
agent scope (a policy of an engine applies to every call its gateways see).

The bundle half (``build_gateway_bundle`` / ``write_gateway_bundle``) composes the per-gateway
``launchpad.policy.gateway/3`` document the interceptor Lambda reads and writes it with the
forward-only CAS pointer.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyEngine
from app.services import policy_cedar, policy_store
from app.services.policy_backend import AuthzDecision, AuthzRequest, get_backend
from app.services.policy_cedar import CompiledSubset
from app.services.policy_entities import (
    Principal,
    build_entities,
    request_context,
    tool_ref,
)

log = logging.getLogger("launchpad.policy")

# How many `v<n>` keys `write_gateway_bundle` skips past when it finds its version already
# written (the key is immutable; the next free version is used instead).
_WRITE_ATTEMPTS = 5
# Global model: an engine with no matching permit denies (Cedar default-deny) — the bundle
# carries the constant `empty_set_decision: "deny"`; the former `allow` knob is gone
EMPTY_SET_DECISION = "deny"
_OUTCOME = {"allow": "ALLOW", "deny": "DENY", "no_decision": "NO_DECISION"}


@dataclass
class Evaluation:
    """What one decision returns to its caller (the simulator, the engine-parity checks)."""

    decision: AuthzDecision
    mode: str
    policy_versions: dict[str, int]
    source_set: str = "active"  # active | draft | pinned
    determining: list[str] = field(default_factory=list)  # ``<policy_id>:v<n>[#k]`` stamps
    # D20-obs1 (additive): why the engine was NOT consulted — ``no_active_policies`` when
    # the applicable set is empty and the knob says allow; ``None`` for a real decision.
    reason: str | None = None
    # A6 (additive): the policy engine's stage that denied — ``a`` (stateless) / ``bt``
    # (temporal) — when the engine decided; ``None`` for the hub's own Cedar and for allows.
    stage: str | None = None

    @property
    def outcome(self) -> str:
        return _OUTCOME[self.decision.decision]

    @property
    def determining_policy_ids(self) -> list[str]:
        """Determining policies as ``<policy_id>:v<n>`` (contract (m)); the per-policy
        ``#k`` suffix of a multi-policy statement is folded away."""
        return list(dict.fromkeys(s.split("#", 1)[0] for s in self.determining))

    @property
    def policy_ids(self) -> list[str]:
        """Bare policy ids behind the determining stamps (``pol_x:v3#1`` → ``pol_x``) —
        what a ``policy_decisions.policy_ids`` column stores."""
        return list(dict.fromkeys(s.split(":v", 1)[0] for s in self.determining))

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "determining_policy_ids": self.determining_policy_ids,
            "policy_ids": self.policy_ids,
            "determining": list(self.determining),
            "errors": list(self.decision.errors),
            "mode": self.mode,
            "policy_versions": dict(self.policy_versions),
            "source_set": self.source_set,
            "latency_ms": round(self.decision.latency_ms, 3),
            "reason": self.reason,
        }


def spec_policy_ids(agent: Agent | None) -> tuple[str, ...]:
    """``AgentSpec.policy_ids`` — INERT since the Global model (the retired agent scope): kept
    so stored specs still validate; it selects nothing any more."""
    if agent is None:
        return ()
    ids = (agent.spec or {}).get("policy_ids") or []
    return tuple(str(i) for i in ids if i)


def _subset_from_rows(db: Session, rows: list[tuple[Policy, int]]) -> CompiledSubset:
    """Compile an uncached subset from explicit ``(policy, version)`` choices."""
    texts, versions, dogwood = [], {}, []
    for policy, version in rows:
        row = policy_store.get_version(db, policy, version)
        versions[policy.id] = int(version)
        if (getattr(row, "dialect", None) or "cedar") == "dogwood":
            dogwood.append(f"{policy.id}:v{version}")
            continue
        texts.append(policy_cedar.annotate(row.statement, f"{policy.id}:v{version}"))
    text = "\n".join(texts)
    return CompiledSubset(
        text=text, handle=policy_cedar.compile_text(text), policy_versions=versions,
        dogwood=tuple(dogwood),
    )


def dogwood_denial(subset: CompiledSubset) -> AuthzDecision | None:
    """The cedarpy fail-closed guard: an applicable ACTIVE / draft / pinned ``dialect='dogwood'``
    row means this subset can only be decided by the Dogwood engine. Off the engine route that
    is an evaluation error and a DENY (reason ``policy_evaluation_error``) — never a skipped
    row, never an allow."""
    if not subset.dogwood:
        return None
    return AuthzDecision("deny", reasons=list(subset.dogwood), errors=[
        "policy_evaluation_error",
        "dogwood policies need the policy engine: " + ", ".join(subset.dogwood),
    ])


def effective_subset(
    db: Session,
    engine_id: str,
    mode: str,
    *,
    include_drafts: bool = False,
    pinned: dict[str, int] | None = None,
) -> tuple[CompiledSubset, str]:
    """The compiled policies of ``engine_id``: ACTIVE only → the cached engine set.
    ``include_drafts`` swaps every policy's draft version in (and includes draft-only
    policies); ``pinned`` evaluates exactly those versions on top. Both are uncached: the
    simulator pays the parse."""
    if not include_drafts and not pinned:
        return policy_cedar.compiled_set(db, engine_id, mode).subset(), "active"
    rows: list[tuple[Policy, int]] = []
    for policy in policy_store.list_policies(db, engine_id):
        version: int | None = policy.active_version if policy.status == "active" else None
        if include_drafts and policy.draft_version is not None:
            version = policy.draft_version
        if pinned and policy.id in pinned:
            version = pinned[policy.id]
        if version is not None:
            rows.append((policy, version))
    return _subset_from_rows(db, rows), ("pinned" if pinned else "draft")


def gateway_mode(db: Session, gateway_id: str | None) -> str:
    """The simulator's ``mode``: the gateway attachment's (``LOG_ONLY`` → ``log_only``,
    ``ENFORCE`` → ``enforce``); ``log_only`` for an unattached / unnamed gateway (nothing is
    enforced there — the answer is informational)."""
    from app.services import policy_engines

    row = policy_engines.attachment_of(db, gateway_id or "")
    return "enforce" if row is not None and row.mode == "ENFORCE" else "log_only"


def evaluate(
    db: Session,
    engine: PolicyEngine | str,
    request: AuthzRequest,
    *,
    principal: Principal,
    agent: Agent | None = None,
    tool_ids: tuple[str, ...] | list[str] = (),
    gateway_id: str | None = None,
    mode: str | None = None,
    include_drafts: bool = False,
    pinned: dict[str, int] | None = None,
    entities: list[dict[str, Any]] | None = None,
) -> Evaluation:
    """One decision (the simulator's cedarpy path, or the Dogwood engine when it holds the
    engine's current ACTIVE set). ``engine`` may be a workspace id (→ its default engine);
    ``mode`` defaults to the named gateway's attachment mode; ``entities`` may be supplied by a
    caller that already built them."""
    engine = policy_store._engine_arg(db, engine)
    mode = mode or gateway_mode(db, gateway_id)
    subset, source_set = effective_subset(
        db, engine.id, mode, include_drafts=include_drafts, pinned=pinned,
    )
    if source_set == "active" and agent is not None and request.action == "call_tool":
        from app.services import policy_engine_decide, policy_engine_sync

        if policy_engine_sync.engine_decides(db, engine):
            # A6: the installed Dogwood set decides (an engine that cannot answer raises
            # EngineControlError — the simulator reports it, never a permit)
            channel = str(request.context.get("channel") or "chat")
            return policy_engine_decide.evaluate_tool(
                db, engine, principal, agent, request.resource.id, channel=channel,
                mode=mode, gateway_id=gateway_id)
    denial = dogwood_denial(subset)
    if denial is not None:
        return Evaluation(decision=denial, mode=mode, policy_versions=dict(subset.policy_versions),
                          source_set=source_set, determining=list(denial.reasons),
                          reason="policy_evaluation_error")
    if entities is None:
        entities = build_entities(
            principal, workspace_id=engine.workspace_id, agent=agent, tool_ids=tool_ids,
            gateway_id=gateway_id,
        )
    decision = get_backend().authorize(request, subset.handle, entities)
    return Evaluation(
        decision=decision, mode=mode, policy_versions=dict(subset.policy_versions),
        source_set=source_set, determining=list(decision.reasons),
    )


def call_tool_request(
    principal: Principal, tool_id: str, *, agent_id: str, channel: str,
) -> AuthzRequest:
    return AuthzRequest(
        principal=principal.ref(), action="call_tool", resource=tool_ref(tool_id),
        context=request_context("call_tool", channel=channel, agent_id=agent_id),
    )


# ── gateway interceptor bundle (schema /3) ───────────────────────────────────


GATEWAY_BUNDLE_SCHEMA = "launchpad.policy.gateway/3"  # Global model: one attachment per gateway


@dataclass(frozen=True)
class GatewayAttachmentState:
    """The bundle's ``attachment`` block of one gateway: which engine (set id), the gateway
    mode and the engine's installed set facts (the compiler's counts, governed and
    temporal-referenced actions)."""

    engine_id: str
    mode: str  # LOG_ONLY | ENFORCE
    active_count: int
    temporal_count: int
    policy_set_version: str
    temporal_set_version: int
    governed_actions: tuple[str, ...] = ()
    temporal_referenced_actions: tuple[str, ...] = ()

    def as_bundle(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id, "mode": self.mode,
            "active_count": int(self.active_count), "temporal_count": int(self.temporal_count),
            "policy_set_version": self.policy_set_version,
            "temporal_set_version": int(self.temporal_set_version),
            "governed_actions": sorted(set(self.governed_actions)),
            "temporal_referenced_actions": sorted(set(self.temporal_referenced_actions)),
        }


def gateway_role_map(db: Session, *, env_name: str | None = None) -> dict[str, dict[str, Any]]:
    """``role name → {agent_id, ws, method, system, byoc, exempt}`` for every live agent: the
    per-agent execution role (``agent_iam.role_name_for``) is how the interceptor knows the
    calling agent (P0 a: the SigV4 caller arrives as the plain role ARN). ``exempt`` (user
    ruling (12), GLOBAL-DEVIATIONS §0b-12): the caller is passed without evaluation — today the
    system presets (``Agent.system_key``), unconditionally (no knob)."""
    from sqlalchemy import select

    from app.core.db import DEFAULT_WORKSPACE_ID
    from app.services import agent_iam

    rows = db.execute(select(Agent).where(Agent.status != "deleted")).scalars().all()
    return {
        agent_iam.role_name_for(agent.name, agent.id, env_name): {
            "agent_id": agent.id, "ws": agent.workspace_id or DEFAULT_WORKSPACE_ID,
            "method": agent.method, "system": bool(agent.system_key),
            "byoc": agent.method == "byoc", "exempt": bool(agent.system_key),
        }
        for agent in sorted(rows, key=lambda a: a.id)
    }


def build_gateway_bundle(
    *, gateway_id: str, gateway_arn: str, version: int, env: str, hub_role: str,
    pubkeys: dict[str, str], role_map: dict[str, dict[str, Any]],
    attachment: GatewayAttachmentState | None, endpoints: list[str], shards: int,
    issued_at: datetime | None = None,
) -> dict[str, Any]:
    """The /3 bundle, checked by the SAME parser the interceptor Lambda reads it with
    (``app.policy.interceptor.bundle.parse_bundle``), so the hub never writes a bundle the
    Lambda would reject. ``attachment`` None ⇒ ``"attachment": null`` (unguarded gateway);
    ``empty_set_decision`` is the constant ``deny``; ``role_map[*].exempt`` is always written
    (the former bundle-level ``exempt_system_presets`` is gone — amendment 10 / ruling 10)."""
    from app.policy.interceptor import bundle as interceptor_bundle

    bundle = {
        "schema": GATEWAY_BUNDLE_SCHEMA, "gateway_id": gateway_id, "gateway_arn": gateway_arn,
        "version": int(version),
        "issued_at": (issued_at or datetime.now(UTC)).isoformat(timespec="seconds"),
        "env": env, "empty_set_decision": EMPTY_SET_DECISION, "hub_role": hub_role,
        "pubkeys": dict(sorted(pubkeys.items())),
        "role_map": {k: role_map[k] for k in sorted(role_map)},
        "attachment": attachment.as_bundle() if attachment is not None else None,
        "engine": {"endpoints": list(endpoints), "shards": int(shards),
                   "key_s3_key": gateway_engine_key(gateway_id)},
    }
    try:
        interceptor_bundle.parse_bundle(bundle, expect_gateway_id=gateway_id)
    except interceptor_bundle.BundleError as exc:
        raise AppError("policy.gateway_bundle_invalid",
                       f"the gateway interceptor bundle is not valid: {exc}",
                       {"gateway_id": gateway_id}, status_code=500) from exc
    return bundle


def gateway_bundle_key(gateway_id: str, version: int) -> str:
    from app.policy.interceptor.bundle import bundle_key as interceptor_bundle_key

    return interceptor_bundle_key(gateway_id, version)


def gateway_engine_key(gateway_id: str) -> str:
    """The engine HMAC key object — readable by the interceptor role only (B3 IAM)."""
    from app.policy.interceptor.bundle import engine_key_key

    return engine_key_key(gateway_id)


def _s3_code(exc: ClientError) -> str:
    return str((exc.response or {}).get("Error", {}).get("Code") or "")


def write_gateway_bundle(
    workspace: Any, bucket: str, bundle: dict[str, Any], *, log_line: Any = None,
) -> tuple[str, int]:
    """Write the immutable ``policy/gateway/<gw>/v<n>.json`` (``IfNoneMatch="*"``, the next
    free version on a taken one), then advance the mutable ``current.json`` pointer the
    Lambda polls (``advance_gateway_pointer``). Returns ``(key, version)``; raises
    ``policy.bundle_write_failed``. The pointer is written only after the bundle exists, so
    the Lambda never follows it to a missing key."""
    s3 = workspace.client("s3")
    gateway_id = str(bundle["gateway_id"])
    version = int(bundle["version"])
    for _attempt in range(_WRITE_ATTEMPTS):
        bundle["version"] = version
        key = gateway_bundle_key(gateway_id, version)
        body = json.dumps(bundle, ensure_ascii=False, sort_keys=True).encode("utf-8")
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                          IfNoneMatch="*")
        except ClientError as exc:
            if _s3_code(exc) in {"PreconditionFailed", "412"}:
                version += 1
                continue
            raise AppError(
                "policy.bundle_write_failed",
                f"could not write the gateway policy bundle to S3: {exc}",
                {"bucket": bucket, "key": key}, status_code=502,
            ) from exc
        advanced = advance_gateway_pointer(s3, bucket, gateway_id, version)
        if log_line is not None:
            attachment = bundle.get("attachment")
            who = f"{attachment['engine_id']} {attachment['mode']}" if attachment else "none"
            log_line(f"policy: gateway bundle v{version} → s3://{bucket}/{key} "
                     f"(attachment {who}, {len(bundle['role_map'])} agent role(s))"
                     + ("" if advanced else " — pointer already at a newer version, kept"))
        return key, version
    raise AppError(
        "policy.bundle_write_failed",
        f"could not find a free gateway bundle version after {_WRITE_ATTEMPTS} attempts",
        {"bucket": bucket, "gateway_id": gateway_id, "version": version}, status_code=502,
    )


def advance_gateway_pointer(s3: Any, bucket: str, gateway_id: str, version: int) -> bool:
    """Compare-and-swap ``current.json`` to ``version`` — only FORWARD. Reads the pointer and
    its ETag; a pointer already at ``version`` or newer is kept (False: a racing writer of a
    newer bundle won); otherwise the write is conditional on that ETag (``IfMatch``, or
    ``IfNoneMatch="*"`` when there is no pointer yet) and a lost race re-reads and retries.
    So two racing publishers can never move the pointer back to the older bundle."""
    from app.policy.interceptor.bundle import pointer as interceptor_pointer
    from app.policy.interceptor.bundle import pointer_key

    key = pointer_key(gateway_id)
    for _attempt in range(_WRITE_ATTEMPTS):
        try:
            current = s3.get_object(Bucket=bucket, Key=key)
            etag = current.get("ETag")
            try:
                held = json.loads(current["Body"].read()).get("version")
            except (ValueError, AttributeError):
                held = None
        except ClientError as exc:
            if _s3_code(exc) not in {"NoSuchKey", "404"}:
                raise AppError("policy.bundle_write_failed",
                               f"could not read the gateway bundle pointer: {exc}",
                               {"bucket": bucket, "key": key}, status_code=502) from exc
            etag = held = None
        if isinstance(held, int) and not isinstance(held, bool) and held >= version:
            return False
        body = json.dumps(interceptor_pointer(gateway_id, version, datetime.now(UTC).isoformat(
            timespec="seconds")), sort_keys=True).encode()
        condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json",
                          **condition)
            return True
        except ClientError as exc:
            if _s3_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict",
                                 "409"}:
                continue  # another writer moved it in between: re-read
            raise AppError("policy.bundle_write_failed",
                           f"could not write the gateway bundle pointer: {exc}",
                           {"bucket": bucket, "key": key}, status_code=502) from exc
    raise AppError("policy.bundle_write_failed",
                   f"the gateway bundle pointer kept changing ({_WRITE_ATTEMPTS} attempts)",
                   {"bucket": bucket, "key": key}, status_code=502)
