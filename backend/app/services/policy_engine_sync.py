"""Publish → compile → engine install, per POLICY ENGINE (Global model, contract
``docs/cn/interfaces/policy-engines.md`` §Engine install; I5 / I6 in
docs/cn/interfaces/policy-principal-contracts.md).

Every change of an engine's ACTIVE set (publish, retire, enforcement-mode change) is compiled
by the Dogwood compiler (``app.policy.dogwood_compiler``) against the schema generated from the
live ``tools/list`` of the gateways the engine is attached to (the engine's workspace gateway
when none is) and installed on EVERY engine shard (``policy_engine_control.install_all``)
BEFORE the ledger commits the change:

* the set does not compile → 422 ``policy.invalid_statement`` (the compiler's errors);
* the engine refuses / cannot be reached / the compiler or the Gateway is unavailable →
  409 ``policy.engine_install_failed``;

and in both cases the caller rolls the change back, journals ``engine.install`` ``failed`` and
the previous set stays live. On success the engine's ``policy_set_seq`` advances (``ps-<n>``),
the temporal-set version advances exactly when the temporal items changed (T4: the Dogwood
engine invalidates open temporal sessions, and every new token's psid embeds it), and each
installed version records its compiled ``kind``. The Dogwood set key IS the engine id (the
install / decide messages' ``ws`` field carries ``pe_<hex>``): one set per engine, shared by
every gateway attached to it. Statements are installed as written (the retired agent scope
injected no clause any more — Global has no agent scope).

Inert (``None``) while the platform switch ``Settings.policy_gateway_interceptor`` is off.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import replace
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.policy import Policy, PolicyEngine, PolicyVersion

logger = logging.getLogger("launchpad.policy")


def schema_read_error(exc: Exception, code: str, *, attempts: int = 1) -> AppError:
    """Keep upstream diagnostics useful without exposing its URL, body or credentials."""
    detail: dict[str, Any] = {"source": "tools_list", "attempts": attempts}
    if isinstance(exc, AppError) and exc.code == "policy.schema_unavailable":
        detail.update({key: exc.detail[key] for key in ("reason", "upstream_status", "attempts")
                       if key in exc.detail})
    elif isinstance(exc, httpx.HTTPStatusError):
        detail.update(reason="HTTPStatusError", upstream_status=exc.response.status_code)
    elif isinstance(exc, AppError):
        detail["reason"] = exc.code
        if exc.code == "gateway.unauthorized":
            detail["upstream_status"] = exc.status_code
    else:
        detail["reason"] = type(exc).__name__
    status = detail.get("upstream_status")
    diagnostic = f"HTTP {status}" if status is not None else detail["reason"]
    return AppError(
        code, f"Gateway tools/list schema read failed ({diagnostic}; "
        f"{detail['attempts']} attempt(s)); the previous policy set remains active",
        detail, status_code=409,
    )


def _read_gateway_schema(ctx: Any) -> list[dict[str, Any]]:
    """Retry only this idempotent read, never compilation, installation or tool calls.

    Three attempts maximum per Gateway, 20-second HTTP inactivity timeout per attempt,
    and 0.25/0.5 second backoff. No cached schema can stand in for a failed read.
    """
    from app.services import mcp_client

    for attempt in range(1, 4):
        try:
            return mcp_client.tools_list(ctx, timeout=20)
        except Exception as exc:
            transient = (
                isinstance(exc, httpx.HTTPStatusError)
                and (exc.response.status_code == 429 or exc.response.status_code >= 500)
            ) or isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))
            if not transient or attempt == 3:
                raise schema_read_error(
                    exc, "policy.schema_unavailable", attempts=attempt,
                ) from None
            time.sleep(0.25 * attempt)
    raise AssertionError("unreachable")


def compiler() -> Any:
    """The Dogwood compiler (``app.policy.dogwood_compiler``) — a seam tests replace; its
    ``EngineUnavailable`` (binary missing / broken) is an install failure, never a crash."""
    from app.policy import dogwood_compiler

    return dogwood_compiler


def enabled() -> bool:
    """Whether the hub installs sets on the Dogwood engine at all: the platform switch
    (``Settings.policy_gateway_interceptor``). Off = the ledger alone changes."""
    return bool(get_settings().policy_gateway_interceptor)


def engine_statement(statement: str, dialect: str | None) -> tuple[str, list[str]]:
    """The text the engine compiles: a stored D20 (``cedar``, Launchpad vocabulary) statement
    goes through the one-way ``rewrite_d20`` (AgentCore vocabulary; the differential test pins
    identical decisions); a ``dogwood`` one as written. The stored row is never rewritten — the
    console shows the operator's text. A row made only of legacy ``invoke`` statements rewrites
    to ``""`` (amendment 9); ``active_set`` leaves it out."""
    if (dialect or "cedar") != "cedar":
        return statement, []
    rewritten = compiler().rewrite_d20(statement)
    return rewritten.statement, list(rewritten.warnings)


def gateway_actions(names: list[str] | tuple[str, ...]) -> list[str]:
    """The compiler's bare tool names (``hr-database___create_payout``) as the entity-formatted
    action ids the gateway bundle carries — ``AgentCore::Action::"hr-database___create_payout"``,
    exactly ``interceptor.core.action_entity(name)``: the interceptor compares the call's action
    entity with ``governed_actions`` / ``temporal_referenced_actions`` by string, so a bare name
    there governs nothing and every call passes (N17-obs488, FAIL-OPEN found live in M2 step 9u).
    This is the ONE place the hub composes those lists; the engine install message is untouched
    (the engine keeps bare names in ``actions`` / ``temporal_referenced_actions``)."""
    from app.policy import principal_entities as pe

    out: set[str] = set()
    for name in names:
        text = str(name)
        if "::" in text or not text:
            raise ValueError(f"not a bare Gateway tool name: {text!r}")
        out.add(pe.ref("Action", text))
    return sorted(out)


def engine_state_is_legacy(state: Any) -> bool:
    """A stored ``engine_state`` written before N17-obs488 (bare tool names in
    ``governed_actions`` / ``temporal_referenced_actions``), or one the migration carried over
    from the workspace era (``set_key`` ≠ the engine id — the Dogwood set still sits under the
    workspace key): the bundle composed from it governs nothing / names a set the engine does
    not hold under this key, so the reconcilers treat it as stale and re-install."""
    if not isinstance(state, dict):
        return False
    if state.get("migrated_from_workspace"):
        return True
    for key in ("governed_actions", "temporal_referenced_actions"):
        for item in state.get(key) or []:
            if not str(item).startswith("AgentCore::Action::"):
                return True
    return False


def tools_list(workspace_id: str) -> dict[str, Any]:
    """One workspace Gateway's live ``tools/list`` (the hub's own read)."""
    from app.services.workspace import context_for_workspace

    return {"tools": _read_gateway_schema(context_for_workspace(workspace_id))}


def schema_workspaces(db: Session, engine: PolicyEngine) -> list[str]:
    """The workspaces whose Gateway ``tools/list`` is the compile schema source for ``engine``:
    the owners of every gateway the engine is attached to, else the engine's own workspace
    (an engine must install — and simulate — before it is attached anywhere)."""
    from app.services import policy_engines

    owners: list[str] = []
    for attachment in policy_engines.attachments_of(db, engine.id):
        owner = policy_engines.gateway_owner(db, attachment.gateway_id)
        if owner and owner not in owners:
            owners.append(owner)
    return owners or [engine.workspace_id]


def engine_tools_list(db: Session, engine: PolicyEngine) -> dict[str, Any]:
    """The union of tools on the engine's default and attached gateways."""
    from app.services import policy_engines

    seen: dict[str, dict[str, Any]] = {}
    for workspace_id in schema_workspaces(db, engine):
        for tool in tools_list(workspace_id)["tools"]:
            name = str((tool or {}).get("name") or "") if isinstance(tool, dict) else ""
            if name and name not in seen:
                seen[name] = tool
    default_gateways = {policy_engines.workspace_gateway(db, ws)[0]
                        for ws in schema_workspaces(db, engine)}
    for attachment in policy_engines.attachments_of(db, engine.id):
        if attachment.gateway_id in default_gateways:
            continue
        for tool in gateway_tools(engine.workspace_id, attachment.gateway_id):
            name = str(tool.get("name") or "")
            if name and name not in seen:
                seen[name] = tool
    return {"tools": list(seen.values())}


def gateway_tools(workspace_id: str, gateway_id: str) -> list[dict[str, Any]]:
    """Read an additional account gateway using its own endpoint and the hub identity."""
    from app.services.agentcore.client import control_client
    from app.services.agentcore.gateway import get_gateway
    from app.services.workspace import context_for_workspace

    ctx = context_for_workspace(workspace_id)
    gateway = get_gateway(control_client(ctx), gateway_id)
    target = replace(ctx, resources={**ctx.resources, "gateway_id": gateway_id,
                                    "gateway_url": gateway["gatewayUrl"],
                                    "gateway_arn": gateway["gatewayArn"]})
    return _read_gateway_schema(target)


def has_user_permit(items: list[dict[str, Any]]) -> bool:
    """Diagnostic only: an active permit in the user stage can admit users."""
    for item in items:
        text = re.sub(r"//[^\n]*", "", str(item.get("text") or ""))
        if item.get("stage") != "bt" or item.get("mode", "active") != "active":
            continue
        if re.search(r"\bpermit\s*\(", text) and not re.search(
                r"\bprincipal\s+(?:is|==)\s+(?:AgentCore|Launchpad)::(?:ApiKey|System)",
                text):
            return True
    return False


def active_set(db: Session, engine_id: str) -> list[dict[str, Any]]:
    """The engine's ACTIVE policies as the session sees them (the caller's pending change
    included): ``[{id, version, mode, statement}]`` in id order, statements as written (after
    the D20 vocabulary rewrite)."""
    db.flush()
    rows = (
        db.query(Policy, PolicyVersion)
        .join(PolicyVersion, (PolicyVersion.policy_id == Policy.id)
              & (PolicyVersion.version == Policy.active_version))
        .filter(Policy.engine_id == engine_id, Policy.status == "active")
        .order_by(Policy.id)
        .all()
    )
    out = []
    for p, v in rows:
        statement, _warnings = engine_statement(v.statement, v.dialect)
        if not statement.strip():
            # amendment 9 compat drop: a stored D20 row made only of `invoke` statements
            # rewrites to nothing — it decides no Gateway call; the operator retires it
            logger.warning("policy: %s v%s rewrites to no statement (invoke retired, "
                           "contract amendment 9) — not installed; retire the policy",
                           p.id, v.version)
            continue
        out.append({"id": p.id, "version": v.version, "mode": p.enforcement_mode or "active",
                    "statement": statement})
    return out


def active_digest(db: Session, engine_id: str) -> str:
    """The digest of the hub's CURRENT ACTIVE set of ``engine_id`` as the engine would install
    it — every ACTIVE policy's id, version, mode and dialect (pending changes included).
    ``engine_decides`` requires it to equal the digest stored on the last successful install
    (review A6 D1), so a publish / retire made while the engine was unreachable never leaves
    the engine deciding on the old set."""
    db.flush()
    rows = (
        db.query(Policy.id, Policy.active_version, Policy.enforcement_mode, PolicyVersion.dialect)
        .join(PolicyVersion, (PolicyVersion.policy_id == Policy.id)
              & (PolicyVersion.version == Policy.active_version))
        .filter(Policy.engine_id == engine_id, Policy.status == "active")
        .order_by(Policy.id)
        .all()
    )
    material = [[pid, int(v), mode or "active", dialect or "cedar"]
                for pid, v, mode, dialect in rows]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def installed_set_digest(engine: PolicyEngine) -> str:
    """The ``active_digest`` the engine's last successful install recorded ("" = none)."""
    state = engine.engine_state
    return str(state.get("set_digest") or "") if isinstance(state, dict) else ""


def engine_decides(db: Session, engine: PolicyEngine) -> bool:
    """The Dogwood engine holds the hub's CURRENT ACTIVE set of ``engine``: the platform
    switch is on, a set was accepted (``policy_set_seq > 0``), its stored digest equals the
    current set's (review D1) and the state is not a legacy / migrated one."""
    if not enabled() or int(engine.policy_set_seq or 0) <= 0:
        return False
    if engine_state_is_legacy(engine.engine_state):
        return False
    installed = installed_set_digest(engine)
    return bool(installed) and installed == active_digest(db, engine.id)


def reconcile_engine(db: Session, engine: PolicyEngine) -> dict[str, Any] | None:
    """Re-install the hub's ACTIVE set when the engine is not provably on it: the stored digest
    differs from the current set (a publish / retire the engine missed, review A6 D1), the state
    is legacy / migrated (N17-obs488; the workspace-era key), or a shard reports another set
    version than ``ps-<policy_set_seq>`` (an install whose ledger commit failed afterwards).
    ``None`` when nothing was needed or the switch is off. Raises like ``install_engine``; the
    caller commits."""
    from app.services import policy_engine_control

    if not enabled():
        return None
    stale = (installed_set_digest(engine) != active_digest(db, engine.id)
             or engine_state_is_legacy(engine.engine_state))
    if not stale:
        want = f"ps-{engine.policy_set_seq}"
        try:
            shards = policy_engine_control.status()
        except Exception:  # noqa: BLE001 — unreachable: the decisions fail closed meanwhile
            return None
        disagree = any(sh.get("ready") and (sh.get("sets") or {}).get(engine.id) != want
                       for sh in shards)
        # N17-obs492: a shard already on `want` (a partial install the hub could not roll
        # back) refuses the same version again — the re-sync goes past the CAS with `force`
        return install_engine(db, engine, force=True) if disagree else None
    return install_engine(db, engine, force=bool(engine_state_is_legacy(engine.engine_state)))


def _temporal_digest(items: list[dict[str, Any]]) -> str:
    temporal = sorted((str(i["id"]), int(i["version"]), str(i["text"]))
                      for i in items if i.get("kind") == "temporal")
    return hashlib.sha256(json.dumps(temporal).encode()).hexdigest() if temporal else ""


def install_engine(db: Session, engine: PolicyEngine, *,
                   force: bool = False) -> dict[str, Any] | None:
    """Compile + install the engine's ACTIVE set (pending change included) on every shard
    under the set key ``engine.id``; ``None`` while the switch is off. Raises ``AppError``
    (422 / 409) — the caller rolls back, journals and re-raises (``policy_store``)."""
    from app.services import policy_engine_control

    if not enabled():
        return None
    try:
        policies = active_set(db, engine.id)
    except Exception as exc:  # noqa: BLE001 — the rewrite runs in the compiler module
        raise AppError("policy.engine_install_failed", "the policy compiler did not answer",
                       {"reason": type(exc).__name__}, status_code=409) from None
    try:
        tools = engine_tools_list(db, engine)
    except Exception as exc:
        raise schema_read_error(exc, "policy.engine_install_failed") from None
    try:
        compiled = compiler().compile(tools, policies)
    except AppError:
        raise
    except Exception as exc:  # EngineUnavailable: the binary is missing / failed to answer
        raise AppError("policy.engine_install_failed", "the policy compiler did not answer",
                       {"reason": type(exc).__name__}, status_code=409) from None
    if not compiled.ok:
        diagnostics = "; ".join(str(e.get("message") or e) if isinstance(e, dict) else str(e)
                                for e in compiled.errors)
        raise AppError("policy.invalid_statement",
                       "the ACTIVE set does not compile against the attached gateways' tools"
                       + (f": {diagnostics}" if diagnostics else ""),
                       {"errors": list(compiled.errors)}, status_code=422)
    digest = _temporal_digest(compiled.items)
    tsv = engine.temporal_set_version + (1 if digest != (engine.temporal_digest or "") else 0)
    psv = f"ps-{engine.policy_set_seq + 1}"
    message = {
        "op": "install", "ws": engine.id, "policy_set_version": psv,
        "temporal_set_version": tsv, "policies": list(compiled.items),
        "schema": compiled.schema, "event_schema": getattr(compiled, "event_schema", "") or "",
    }
    if force:  # N17-obs492: re-sync shards that disagree / roll back — past the engine's CAS
        message["force"] = True
    # N17-obs488: the bundle's action ids, converted BEFORE anything reaches the engine so a
    # compiler whose output format changed fails the install as a 409 (review r17-bl), never a
    # 500 after the shards already hold the new set
    try:
        governed = gateway_actions(getattr(compiled, "actions", []) or [])
        temporal_referenced = gateway_actions(
            getattr(compiled, "temporal_referenced_actions", []) or [])
    except ValueError as exc:
        raise AppError("policy.engine_install_failed",
                       "the policy compiler's tool names are not in the expected form — "
                       "the previous set stays live",
                       {"reason": "compiler_output_format", "errors": [str(exc)]},
                       status_code=409) from None
    try:
        policy_engine_control.install_all(message)
    except policy_engine_control.EngineControlError as exc:
        raise AppError("policy.engine_install_failed",
                       "the policy engine did not accept the set — the previous one stays live",
                       {"reason": exc.code, "errors": exc.errors[:20]},
                       status_code=409) from None
    engine.policy_set_seq += 1
    engine.temporal_set_version = tsv
    engine.temporal_digest = digest
    installed = {
        "policy_set_version": psv, "temporal_set_version": tsv,
        "temporal_count": int(getattr(compiled, "temporal_count", 0) or 0),
        "active_count": len(policies),
        "has_user_permit": has_user_permit(compiled.items),
        # N17-obs488: entity-formatted, the form the interceptor compares (contract amendment 6)
        "governed_actions": governed,
        "temporal_referenced_actions": temporal_referenced,
    }
    # every attached gateway's bundle is composed from this; the write after the commit clears
    # `bundle_pending` (policy_gateway_publish)
    engine.engine_state = {**installed, "bundle_pending": True,
                           # review A6 D1: what `engine_decides` compares the hub's set with
                           "set_digest": active_digest(db, engine.id)}
    kinds: dict[tuple[str, int], str] = {}
    for item in compiled.items:  # a split statement (`pol_x#k`) belongs to `pol_x`
        key = (str(item["id"]).split("#", 1)[0], int(item["version"]))
        kinds[key] = "temporal" if item.get("kind") == "temporal" or \
            kinds.get(key) == "temporal" else "stateless"
    for (pid, version), kind in kinds.items():
        row = db.query(PolicyVersion).filter_by(policy_id=pid, version=version).first()
        if row is not None:
            row.kind = kind
    return dict(installed)


# ── pre-engine call forms (the workspace's default engine) ───────────────────────────────


def _default_engine(db: Session, workspace_id: str) -> PolicyEngine:
    from app.services import policy_store

    return policy_store.settings_row(db, workspace_id)


def publish_enabled(db: Session, workspace_id: str) -> bool:
    """Pre-engine form of ``enabled()`` (the workspace switches are gone)."""
    return enabled()


def install_workspace(db: Session, workspace_id: str, *, force: bool = False):
    """``install_engine`` on the workspace's default engine."""
    return install_engine(db, _default_engine(db, workspace_id), force=force)


def reconcile_workspace(db: Session, workspace_id: str):
    """``reconcile_engine`` on the workspace's default engine."""
    return reconcile_engine(db, _default_engine(db, workspace_id))
