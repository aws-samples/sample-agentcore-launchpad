"""Decision logic of the gateway interceptor (stream B1/B2; design §5.2, §12.3, §13.3-§13.5,
contract docs/cn/interfaces/policy-principal-contracts.md I1-I4, I7, I8).

Pure over its inputs: the parsed MCP event, a ``BundleSource``, an engine-client factory,
a clock and an audit sink. ``handler.py`` adds the Lambda entry point and the never-raise
wrapper; tests drive this module with the stage-0 fakes.

REQUEST, ``tools/call`` — the check order of contract amendment 3 (the first rule that
decides wins), after the bundle itself (cached, last good kept; none at all → deny
``bundle_unavailable``):

1. the calling agent from ``awsPrincipalArn`` (``…:role/<name>`` → ``role_map``; the hub role
   needs a token and takes its workspace from it). An UNKNOWN caller is denied
   ``unknown_caller`` when the gateway's ENFORCE attachment governs the tool, logged as
   ``would_deny``
   (reason ``unknown_caller``) under a log-only one, and passed otherwise;
2. the gateway's attachment (Global model, /3) or the caller's workspace block (/2): none →
   pass, no row; a /2 block unpublished → deny ``workspace_unpublished``; zero ACTIVE policies →
   ``empty_set_decision`` (D20-obs1); a tool no policy governs or references → pass, no row;
   an EXEMPT caller (``role_map[*].exempt``, default = ``system``: the presets — architect
   assistant, eval worker, Skill Lab worker; user ruling (12), GLOBAL-DEVIATIONS §0b-12) →
   allow row ``system_exempt``, no engine call — unconditional, there is no knob;
3. the principal token in ``params._meta["launchpad/principal"]`` (always stripped before the
   target): invalid → deny ``principal_invalid``; missing → the agent itself
   (``AgentCore::IamEntity``) for a ``system`` agent, else deny ``principal_missing`` (D2) —
   before the session check, since without a token there is no psid to require; a verified
   SYSTEM principal (``System::"eval"`` / ``System::"canary"`` — an evaluation run or a canary
   acting through any agent, user ruling (12)) → allow row ``system_exempt``, no engine call,
   no session requirement, nothing recorded for temporal rules;
4. no valid psid while the workspace has a temporal policy (T9) → ``-32602
   policy_session_required``;
5. the engine ``/v1/decide`` (routing in ``engine_client``; ``tsv`` / ``session_started_at``
   from the token, ``context.expect`` from the bundle; time is the engine's): allow → pass;
   deny → the I8 deny; 409 ``session_invalidated`` / ``history_lost`` → the I8 conflict;
   409 ``set_mismatch`` → deny ``temporal_set_mismatch`` and a bundle reload; anything else
   (unreachable, slow, 4xx/5xx) → deny (T7 fail closed).

Mode ``log_only`` turns every deny of steps 2-5 into a pass with a ``would_deny`` (policy)
or ``error`` (failure) row — the break-glass of §13.5.

RESPONSE: ``tools/call`` on a temporal-referenced tool submits the ``response`` / ``error``
event (output parsed from ``content[].text`` JSON, P0 c), best effort; ``tools/list`` is
filtered through ``/v1/list-filter`` in enforce mode, unfiltered on any failure (calls are
still decided). The gateway response itself is always passed through unchanged.

Audit (I7): one ``launchpad.policy.decision`` line per decided ``tools/call`` — argument
NAMES only, never a value, a token or a header.
"""

from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from . import codec
from .bundle import Attachment, Bundle, BundleError, BundleSource, Caller
from .engine_client import EngineClient, EngineRejected, EngineUnavailable

OUTPUT_VERSION = "1.0"
META_KEY = "launchpad/principal"
# contract amendment 1: ONE namespace for the whole tool-call vocabulary
ACTION_NAMESPACE = "AgentCore::Action"
GATEWAY_ENTITY = "AgentCore::Gateway"
IAM_ENTITY = "AgentCore::IamEntity"
USER_ENTITY = "AgentCore::User"
ROLE_ENTITY = "AgentCore::Role"
API_KEY_ENTITY = "AgentCore::ApiKey"
SYSTEM_ENTITY = "AgentCore::System"
JSONRPC_DENY_CODE = -32002
JSONRPC_SESSION_REQUIRED_CODE = -32602
DENY_PREFIX = "Tool Execution Denied: Tool call not allowed due to policy enforcement"
DEFAULT_DENY_REASON = "No policy applies to the request (denied by default)."
SESSION_REQUIRED_MESSAGE = "policy_session_required"
CONFLICT_MESSAGE = (
    "ConflictException: the policy session was invalidated by a policy change; "
    "start a new session"
)
TOKEN_TTL_S = 900
TOKEN_TTL_BYOC_S = 300
# the token-less principal of a system agent has no psid; its (stateless) session key uses this
NO_PSID = "none"
_ROLE_ARN = re.compile(
    r"arn:[a-z-]+:(?:iam::(?P<iam>\d{12}):role/(?:[^/]+/)*|"
    r"sts::(?P<sts>\d{12}):assumed-role/)(?P<role>[\w+=,.@-]+)"
)
REASON_MAX = 2000
# the engine's errors[0] when a policy failed to evaluate (stream C review fix 1)
EVALUATION_ERROR = "policy_evaluation_error"
# engine data-plane limit on list-filter actions[] (stream C review fix 4)
LIST_FILTER_MAX_ACTIONS = 1024

Audit = Callable[[dict[str, Any]], None]
EngineFactory = Callable[[Bundle, bytes], EngineClient]


@dataclass
class Deps:
    source: BundleSource
    engine: EngineFactory
    audit: Audit
    clock: Callable[[], float] = time.time


@dataclass
class Call:
    """What one REQUEST/RESPONSE invocation learned — the never-raise wrapper reads ``mode``
    to choose between fail-closed (enforce / unknown) and pass (log_only)."""

    request_id: str
    gateway_arn: str | None
    mode: str | None = None
    started: float = field(default_factory=time.perf_counter)


# ── event access ───────────────────────────────────────────────────────────


def request_body(event: dict[str, Any]) -> Any:
    return ((event.get("mcp") or {}).get("gatewayRequest") or {}).get("body")


def caller_arn(event: dict[str, Any]) -> str:
    request = (event.get("mcp") or {}).get("gatewayRequest") or {}
    return str(((request.get("context") or {}).get("identity") or {}).get("awsPrincipalArn")
               or "")


def is_response(event: dict[str, Any]) -> bool:
    return (event.get("mcp") or {}).get("gatewayResponse") is not None


def role_name(arn: str, account: str | None = None) -> str | None:
    """The role name of a caller ARN — ``None`` when it is not a role, or (``account`` given)
    when the role lives in another account: ``role_map`` is keyed by name only, so a
    same-named role elsewhere must never pass for one of ours (nit 2)."""
    match = _ROLE_ARN.fullmatch(arn) or _ROLE_ARN.match(arn)
    if not match:
        return None
    if account is not None and (match["iam"] or match["sts"]) != account:
        return None
    return match["role"]


def stripped(body: Any) -> Any:
    """The body the target sees: ``_meta["launchpad/principal"]`` removed (I2), an emptied
    ``_meta`` dropped — in every element of a (passed) batch too."""
    if isinstance(body, list):
        return [stripped(item) for item in body]
    if not isinstance(body, dict):
        return body
    out = copy.deepcopy(body)
    params = out.get("params")
    if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
        params["_meta"].pop(META_KEY, None)
        if not params["_meta"]:
            params.pop("_meta")
    return out


def token_of(body: Any) -> Any:
    params = body.get("params") if isinstance(body, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta.get(META_KEY) if isinstance(meta, dict) else None


def tool_name(body: Any) -> str | None:
    params = body.get("params") if isinstance(body, dict) else None
    name = params.get("name") if isinstance(params, dict) else None
    return name if isinstance(name, str) and name else None


def arguments(body: Any) -> dict[str, Any]:
    params = body.get("params") if isinstance(body, dict) else None
    args = params.get("arguments") if isinstance(params, dict) else None
    return args if isinstance(args, dict) else {}


def entity(type_name: str, entity_id: str) -> str:
    escaped = str(entity_id).replace("\\", "\\\\").replace('"', '\\"')
    return f'{type_name}::"{escaped}"'


def action_entity(name: str) -> str:
    return entity(ACTION_NAMESPACE, name)


# ── outputs ────────────────────────────────────────────────────────────────


def pass_request(body: Any) -> dict[str, Any]:
    return {"interceptorOutputVersion": OUTPUT_VERSION,
            "mcp": {"transformedGatewayRequest": {"body": stripped(body)}}}


def short_circuit(jsonrpc_id: Any, error: dict[str, Any]) -> dict[str, Any]:
    return {"interceptorOutputVersion": OUTPUT_VERSION, "mcp": {"transformedGatewayResponse": {
        "statusCode": 200, "body": {"jsonrpc": "2.0", "id": jsonrpc_id, "error": error}}}}


def pass_response(event: dict[str, Any], body: Any = None) -> dict[str, Any]:
    response = (event.get("mcp") or {}).get("gatewayResponse") or {}
    return {"interceptorOutputVersion": OUTPUT_VERSION, "mcp": {"transformedGatewayResponse": {
        "statusCode": response.get("statusCode", 200),
        "body": response.get("body") if body is None else body}}}


def deny_error(reason: str, *, decision_id: str, stage: str, reason_code: str,
               policy_id: str | None = None) -> dict[str, Any]:
    return {"code": JSONRPC_DENY_CODE, "message": f"{DENY_PREFIX} [{reason}]",
            "data": {"decision_id": decision_id, "policy_id": policy_id, "stage": stage,
                     "reason_code": reason_code}}


def deny(jsonrpc_id: Any, reason: str, **data: Any) -> dict[str, Any]:
    """The I8 deny with Global's exact text (what ``policy_denials.parse_tool_denial`` reads)."""
    return short_circuit(jsonrpc_id, deny_error(reason, **data))


# ── the decision ───────────────────────────────────────────────────────────


@dataclass
class Principal:
    kind: str  # user | api_key | system | agent | unknown
    id: str
    entity: str
    parents: list[str]
    attrs: dict[str, Any]
    psid: str | None = None
    jti: str | None = None
    channel: str = "direct"

    tsv: int | None = None
    iat: int | None = None

    @classmethod
    def of_claims(cls, claims: dict[str, Any], engine_id: str | None = None) -> Principal:
        """Amendment 1 entities, with exactly the schema's attributes (a missing required
        one is a fail-closed deny at the engine; extra token ``attrs`` are not forwarded)."""
        p = claims["p"]
        kind, pid = p["kind"], p["id"]
        if kind == "user":
            ent, parents = entity(USER_ENTITY, pid), [entity(ROLE_ENTITY, p["role"])]
            attrs = {"username": pid, "role": p["role"]}
            if p.get("user_id"):
                attrs["user_id"] = p["user_id"]
        elif kind == "api_key":  # D1 revised: its own type, no parent
            ent, parents = entity(API_KEY_ENTITY, pid), []
            attrs = {"name": str(p.get("label") or pid)}
        else:
            ent, parents, attrs = entity(SYSTEM_ENTITY, pid), [], {}
        session = claims
        if engine_id is not None and "engine_versions" in claims and kind != "system":
            version = claims["engine_versions"].get(engine_id)
            if version is None:
                raise codec.PrincipalTokenError("session_invalidated", "engine session absent")
            session = {"tsv": version, "psid": codec.psid_for(
                claims["ws"], claims["sid"], version, claims["generation"])}
        return cls(kind=kind, id=pid, entity=ent, parents=parents, attrs=attrs,
                   psid=session["psid"], jti=claims["jti"], channel=claims["ch"],
                   tsv=session["tsv"], iat=claims["iat"])


# the I7 line spells the gateway mode the Global way (ruling: rows carry LOG_ONLY | ENFORCE)
LINE_MODES = {"log_only": "LOG_ONLY", "enforce": "ENFORCE"}


@dataclass
class Row:
    """Accumulates one I7 decision line."""

    call: Call
    ws: str
    agent_id: str | None
    principal: dict[str, str]
    action: str
    mode: str
    input_keys: list[str]
    jsonrpc_id: Any = None
    jti: str | None = None
    psid: str | None = None
    gateway_id: str | None = None  # Global model: every decision names its gateway + engine
    engine_id: str | None = None

    def decision_id(self) -> str:
        nonce = self.jti or f"req:{self.call.request_id}"
        return codec.decision_id(nonce, self.jsonrpc_id, "final")

    def line(self, decision: str, *, reason: str | None, stage_a: str | None = None,
             stage_bt: str | None = None, determining: list[dict[str, Any]] | None = None,
             event_seq: int | None = None, store_epoch: str | None = None) -> dict[str, Any]:
        determining = determining or []
        return {"launchpad.policy.decision": {
            "decision_id": self.decision_id(), "source": "gateway", "stage": "final",
            "ws": self.ws, "gateway_id": self.gateway_id, "engine_id": self.engine_id,
            "agent_id": self.agent_id, "principal": self.principal,
            "action": self.action, "decision": decision,
            "mode": LINE_MODES.get(self.mode, self.mode),
            "policy_ids": sorted({str(d.get("policy_id")) for d in determining
                                  if d.get("policy_id")}),
            "determining": [f"{d.get('policy_id')}:v{d.get('version')}" for d in determining
                            if d.get("policy_id")],
            "stage_a": stage_a, "stage_bt": stage_bt, "session_psid": self.psid,
            "event_seq": event_seq if isinstance(event_seq, int) else None,
            "store_epoch": store_epoch if isinstance(store_epoch, str) else None,
            "input_keys": self.input_keys,
            "reason": (reason or None) and reason[:REASON_MAX],
            "latency_ms": round(max(0.0, (time.perf_counter() - self.call.started) * 1000), 3),
            "ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }}


def agent_object(caller: Caller, arn: str, *, schema: str = "") -> dict[str, Any]:
    """The I4 ``agent`` object — the same on decide, respond and list-filter (amendment 2)."""
    descriptor = {"entity": entity(IAM_ENTITY, arn), "agent_id": caller.agent_id,
                  "method": caller.method, "system": caller.system}
    # R2 is installed before R1: /2 requests must remain readable by the old engine.
    if schema == "launchpad.policy.gateway/3":
        descriptor["workspace_id"] = caller.ws
    return descriptor


def principal_object(principal: Principal) -> dict[str, Any]:
    return {"entity": principal.entity, "parents": principal.parents, "attrs": principal.attrs}


def _verify(bundle: Bundle, token: str, *, ws: str, agent_id: str, byoc: bool,
            now: float) -> codec.VerifiedToken:
    return codec.verify_token(
        token, bundle.pubkeys(), now=now, workspace_id=ws, env=bundle.env, agent_id=agent_id,
        max_ttl_s=TOKEN_TTL_BYOC_S if byoc else TOKEN_TTL_S,
    )


def _hub_caller(bundle: Bundle, token: Any, now: float) -> tuple[Caller, codec.VerifiedToken]:
    """The hub's own MCP client (console tool tests, ``mcp_client``): its workspace and
    audience come from the token it must carry."""
    if not isinstance(token, str):
        raise codec.PrincipalTokenError("principal_missing")
    _, claims = codec.decode_unverified(token)
    ws, agt = claims.get("ws"), claims.get("agt")
    if not isinstance(ws, str) or not isinstance(agt, str):
        raise codec.PrincipalTokenError("bad_claims", "ws / agt")
    verified = _verify(bundle, token, ws=ws, agent_id=agt, byoc=False, now=now)
    return Caller(role=bundle.hub_role, agent_id="hub", ws=ws, method="hub", system=True,
                  byoc=False), verified


def on_request(event: dict[str, Any], call: Call, deps: Deps) -> dict[str, Any]:
    body = request_body(event)
    if isinstance(body, list):
        return _batch(event, body, call, deps)
    if not isinstance(body, dict) or body.get("method") != "tools/call":
        return pass_request(body)
    jsonrpc_id = body.get("id")
    try:
        bundle, key = deps.source.get()
    except BundleError as exc:
        deps.audit({"launchpad.policy.interceptor": {"event": "bundle_unavailable",
                                                     "detail": exc.code}})
        return deny(jsonrpc_id, "interceptor_error", decision_id=codec.decision_id(
            f"req:{call.request_id}", jsonrpc_id, "final"), stage="final",
            reason_code=exc.code)  # bundle_unavailable | bundle_stale (N17-obs483)
    if call.gateway_arn and call.gateway_arn != bundle.gateway_arn:
        return deny(jsonrpc_id, "interceptor_error", decision_id=codec.decision_id(
            f"req:{call.request_id}", jsonrpc_id, "final"), stage="final",
            reason_code="gateway_mismatch")
    name = tool_name(body)
    if name is None:
        return pass_request(body)  # not a well-formed call: the gateway answers it
    action = action_entity(name)
    now = deps.clock()
    token = token_of(body)
    arn = caller_arn(event)
    role = role_name(arn, bundle.account)
    input_keys = sorted(str(k) for k in arguments(body))

    verified: codec.VerifiedToken | None = None
    caller = bundle.role_map.get(role) if role else None
    if caller is None and role and role == bundle.hub_role:
        try:
            caller, verified = _hub_caller(bundle, token, now)
        except codec.PrincipalTokenError:
            caller = None
    if caller is None:
        return _unknown_caller(body, call, bundle, action, name, arn, input_keys, deps)

    w = bundle.block_for(caller.ws)
    if w is None:
        return pass_request(body)  # no policy engine attached: the gateway is unguarded
    call.mode = w.mode
    row = Row(call=call, ws=caller.ws, agent_id=caller.agent_id,
              principal={"kind": "agent", "id": caller.agent_id}, action=name, mode=w.mode,
              input_keys=input_keys, jsonrpc_id=jsonrpc_id, gateway_id=bundle.gateway_id,
              engine_id=w.engine_id)
    if caller.exempt and caller.method != "hub":
        # user ruling (12): an exempt caller (system presets) is never evaluated — no knob
        deps.audit(row.line("allow", reason="system_exempt"))
        return pass_request(body)

    # the principal (I1 / D2 / T9)
    if verified is None and isinstance(token, str):
        try:
            verified = _verify(bundle, token, ws=caller.ws, agent_id=caller.agent_id,
                               byoc=caller.byoc, now=now)
        except codec.PrincipalTokenError as exc:
            row.principal = {"kind": "unknown", "id": "invalid-token"}
            return _refuse(body, row, w, deps, reason=f"principal_invalid:{exc.code}",
                           reason_code="principal_invalid", stage="bt")
    elif verified is None and token is not None:
        row.principal = {"kind": "unknown", "id": "invalid-token"}
        return _refuse(body, row, w, deps, reason="principal_invalid:malformed",
                       reason_code="principal_invalid", stage="bt")
    if verified is not None:
        try:
            principal = Principal.of_claims(
                verified.claims, w.engine_id if bundle.schema.endswith("/3") else None)
        except codec.PrincipalTokenError:
            return _conflict(body, row, w, deps, "session_invalidated")
        row.principal = {"kind": principal.kind, "id": principal.id}
        row.jti, row.psid = principal.jti, principal.psid
        if principal.kind == "system":
            # user ruling (12), GLOBAL-DEVIATIONS §0b-12 option α: an evaluation run / a canary
            # acts through this agent under a hub-signed System token — exempt from policy: an
            # allow row, no engine call, no session requirement (before this step a forbid such
            # as payout_admin_only could deny it)
            deps.audit(row.line("allow", reason="system_exempt"))
            return pass_request(body)
    elif w.active_count == 0:
        if bundle.empty_set_decision == "allow":
            return pass_request(body)
        return _refuse(body, row, w, deps, reason=DEFAULT_DENY_REASON, reason_code="empty_set",
                       stage="final")
    elif caller.system:
        principal = Principal(kind="agent", id=caller.agent_id, entity=entity(IAM_ENTITY, arn),
                              parents=[], attrs={})
    else:
        return _refuse(body, row, w, deps, reason="principal_missing",
                       reason_code="principal_missing", stage="bt")
    if not w.published:
        return _refuse(body, row, w, deps, reason="workspace_unpublished",
                       reason_code="workspace_unpublished", stage="final")
    if w.active_count == 0:
        if bundle.empty_set_decision == "allow":
            return pass_request(body)
        return _refuse(body, row, w, deps, reason=DEFAULT_DENY_REASON, reason_code="empty_set",
                       stage="final")
    if action not in w.governed_actions and action not in w.temporal_referenced_actions:
        if bundle.schema != "launchpad.policy.gateway/3":
            return pass_request(body)
        return _refuse(body, row, w, deps, reason=DEFAULT_DENY_REASON,
                       reason_code="policy", stage="final")
    if w.temporal_count and not principal.psid:
        return _session_required(body, row, w, deps)

    session = codec.session_key(caller.ws, principal.psid or NO_PSID, principal.kind,
                                principal.id)
    request = {
        "ws": w.engine_id, "session": session, "record": False, "action": action,
        "resource": entity(GATEWAY_ENTITY, bundle.gateway_arn),
        "agent": agent_object(caller, arn, schema=bundle.schema),
        "principal": principal_object(principal),
        "context": {"input": arguments(body), "agent_id": caller.agent_id,
                    "channel": principal.channel,
                    "expect": {"policy_set_version": w.policy_set_version,
                               "temporal_set_version": w.temporal_set_version}},
    }
    if principal.tsv is not None:  # amendment 2: T4 invalidation / store-epoch checks
        request["tsv"] = principal.tsv
        request["session_started_at"] = principal.iat
    owner_only = verified is not None and action in w.temporal_referenced_actions
    client = deps.engine(bundle, key)
    try:
        answer = client.decide(request, owner_only=owner_only)
    except EngineRejected as exc:
        if exc.code in ("session_invalidated", "history_lost"):
            return _conflict(body, row, w, deps, exc.code)
        if exc.code == "set_mismatch":
            deps.source.refresh(force=True)
            return _failure(body, row, w, deps, "temporal_set_mismatch")
        return _failure(body, row, w, deps, f"engine_{exc.code}")
    except EngineUnavailable as exc:
        return _failure(body, row, w, deps, f"engine_{exc.code}")

    determining = [d for d in answer.get("determining") or [] if isinstance(d, dict)]
    notes = []
    if answer.get("policy_set_version") not in (None, w.policy_set_version):
        # N17-obs492 (re-review F6): the engine decided on another set than the bundle's —
        # the same situation as a 409 set_mismatch, so in `enforce` it fails closed the same
        # way (an engine that ignored `context.expect` could otherwise decide silently);
        # `log_only` keeps the note and lets the call through
        deps.source.refresh(force=True)
        if w.mode == "enforce":
            return _failure(body, row, w, deps, "temporal_set_mismatch")
        notes.append(f"engine set {answer.get('policy_set_version')} ≠ bundle "
                     f"{w.policy_set_version}")
    if answer.get("log_only_would"):
        notes.append(f"log_only policy would {answer['log_only_would']}")
    facts = {"stage_a": answer.get("stage_a"), "stage_bt": answer.get("stage_bt"),
             "determining": determining, "event_seq": answer.get("event_seq"),
             "store_epoch": answer.get("store_epoch")}
    if answer["decision"] == "allow":
        deps.audit(row.line("allow", reason="; ".join(notes) or None, **facts))
        return pass_request(body)
    active = [d for d in determining if d.get("mode", "active") == "active"]
    errors = [str(e) for e in answer.get("errors") or []]
    reason_code = "policy"
    if errors and errors[0] == EVALUATION_ERROR:
        # an evaluation error in a policy is a decided deny, not an outage (no retry)
        reason, reason_code = EVALUATION_ERROR, EVALUATION_ERROR
    elif active:
        reason = f"Policy evaluation denied due to {', '.join(str(d['policy_id']) for d in active)}"
    else:
        reason = DEFAULT_DENY_REASON
    stage = "a" if answer.get("stage_a") == "deny" else "bt"
    if w.mode == "log_only":
        deps.audit(row.line("would_deny", reason="; ".join([reason, *notes]), **facts))
        return pass_request(body)
    deps.audit(row.line("deny", reason="; ".join([reason, *notes]), **facts))
    return deny(body.get("id"), reason, decision_id=row.decision_id(), stage=stage,
                reason_code=reason_code,
                policy_id=str(active[0]["policy_id"]) if active else None)


def _batch(event: dict[str, Any], body: list[Any], call: Call, deps: Deps) -> dict[str, Any]:
    """A JSON-RPC batch is not supported (nit 3): its calls cannot be decided one by one in a
    single short-circuit. Denied (``batch_unsupported``, one error with ``id: null``) when an
    enforcing workspace with ACTIVE policies applies — the caller's, or any on the gateway when
    the caller is unknown; a pass with a ``would_deny`` row under log-only; else passed."""
    try:
        bundle, _key = deps.source.get()
    except BundleError as exc:
        return short_circuit(None, deny_error(
            "interceptor_error", decision_id=codec.decision_id(f"req:{call.request_id}", None,
                                                               "final"),
            stage="final", reason_code=exc.code))
    role = role_name(caller_arn(event), bundle.account)
    caller = bundle.role_map.get(role) if role else None
    blocks = [bundle.block_for(caller.ws)] if caller else bundle.blocks
    live = [w for w in blocks if w is not None and
            (w.active_count or bundle.schema == "launchpad.policy.gateway/3")]
    if not live:
        return pass_request(body)
    mode = "enforce" if any(w.mode == "enforce" for w in live) else "log_only"
    call.mode = mode
    row = Row(call=call, ws=caller.ws if caller else "unknown",
              agent_id=caller.agent_id if caller else None,
              principal={"kind": "agent", "id": caller.agent_id} if caller else
              {"kind": "unknown", "id": role or "unknown"},
              action="(batch)", mode=mode, input_keys=[], gateway_id=bundle.gateway_id,
              engine_id=live[0].engine_id)
    if mode == "log_only":
        deps.audit(row.line("would_deny", reason="batch_unsupported"))
        return pass_request(body)
    deps.audit(row.line("deny", reason="batch_unsupported"))
    return deny(None, "batch_unsupported", decision_id=row.decision_id(), stage="final",
                reason_code="batch_unsupported")


def _unknown_caller(body: dict[str, Any], call: Call, bundle: Bundle, action: str, name: str,
                    arn: str, input_keys: list[str], deps: Deps) -> dict[str, Any]:
    enforce, log_only = bundle.any_enforcing(action)
    if not (enforce or log_only):
        return pass_request(body)
    row = Row(call=call, ws="unknown", agent_id=None,
              principal={"kind": "unknown", "id": role_name(arn) or "unknown"}, action=name,
              mode="enforce" if enforce else "log_only", input_keys=input_keys,
              jsonrpc_id=body.get("id"), gateway_id=bundle.gateway_id,
              engine_id=bundle.attachment.engine_id if bundle.attachment else None)
    call.mode = row.mode
    if not enforce:
        deps.audit(row.line("would_deny", reason="unknown_caller"))
        return pass_request(body)
    deps.audit(row.line("deny", reason="unknown_caller"))
    return deny(body.get("id"), "unknown_caller", decision_id=row.decision_id(), stage="a",
                reason_code="unknown_caller")


def _refuse(body: dict[str, Any], row: Row, w: Attachment, deps: Deps, *, reason: str,
            reason_code: str, stage: str) -> dict[str, Any]:
    """A deny the interceptor decides itself (no engine verdict)."""
    if w.mode == "log_only":
        deps.audit(row.line("would_deny", reason=reason))
        return pass_request(body)
    deps.audit(row.line("deny", reason=reason))
    return deny(body.get("id"), reason, decision_id=row.decision_id(), stage=stage,
                reason_code=reason_code)


def _failure(body: dict[str, Any], row: Row, w: Attachment, deps: Deps,
             reason_code: str) -> dict[str, Any]:
    """The engine could not decide (T7): fail closed in enforce, pass + ``error`` row in
    log_only."""
    if w.mode == "log_only":
        deps.audit(row.line("error", reason=reason_code))
        return pass_request(body)
    deps.audit(row.line("deny", reason=reason_code))
    return deny(body.get("id"), reason_code, decision_id=row.decision_id(), stage="final",
                reason_code=reason_code)


def _session_required(body: dict[str, Any], row: Row, w: Attachment,
                      deps: Deps) -> dict[str, Any]:
    if w.mode == "log_only":
        deps.audit(row.line("would_deny", reason=SESSION_REQUIRED_MESSAGE))
        return pass_request(body)
    deps.audit(row.line("deny", reason=SESSION_REQUIRED_MESSAGE))
    return short_circuit(body.get("id"), {"code": JSONRPC_SESSION_REQUIRED_CODE,
                                          "message": SESSION_REQUIRED_MESSAGE})


def _conflict(body: dict[str, Any], row: Row, w: Attachment, deps: Deps,
              code: str) -> dict[str, Any]:
    if w.mode == "log_only":
        deps.audit(row.line("error", reason=code))
        return pass_request(body)
    deps.audit(row.line("deny", reason=code))
    return short_circuit(body.get("id"), {"code": JSONRPC_DENY_CODE, "message": CONFLICT_MESSAGE,
                                          "data": {"reason_code": code}})


# ── RESPONSE ───────────────────────────────────────────────────────────────


def _response_body(event: dict[str, Any]) -> Any:
    return ((event.get("mcp") or {}).get("gatewayResponse") or {}).get("body")


def response_event(response: Any) -> tuple[str, dict[str, Any]] | None:
    """``(kind, output)`` of the executed or short-circuited call, or ``None`` when the
    REQUEST never reached the engine (our own non-policy refusals: nothing was recorded)."""
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    if isinstance(error, dict):
        data = error.get("data") if isinstance(error.get("data"), dict) else {}
        if data.get("reason_code") not in (None, "policy", EVALUATION_ERROR):
            return None
        return "error", {"ok": False}
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    if result.get("isError") is True:
        return "error", {"ok": False}
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                parsed = json.loads(item.get("text") or "")
            except ValueError:
                break
            if isinstance(parsed, dict):
                return "response", parsed
            break
    return "response", {"ok": True}


def on_response(event: dict[str, Any], call: Call, deps: Deps) -> dict[str, Any]:
    # N17-obs498: the RESPONSE invocation that follows every REQUEST is where a pending bundle
    # refresh completes (a Lambda freezes between invocations, so a background thread never
    # finishes on its own under spaced traffic) — off the decision path, within its budget
    deps.source.settle()
    body = request_body(event)
    method = body.get("method") if isinstance(body, dict) else None
    if method not in ("tools/call", "tools/list"):
        return pass_response(event)
    try:
        bundle, key = deps.source.get()
    except BundleError:
        return pass_response(event)
    role = role_name(caller_arn(event), bundle.account)
    caller = bundle.role_map.get(role) if role else None
    token = token_of(body)
    now = deps.clock()
    verified = None
    try:
        if caller is None and role and role == bundle.hub_role:
            caller, verified = _hub_caller(bundle, token, now)
        elif caller is not None and isinstance(token, str):
            verified = _verify(bundle, token, ws=caller.ws, agent_id=caller.agent_id,
                               byoc=caller.byoc, now=now)
    except codec.PrincipalTokenError:
        return pass_response(event)
    w = bundle.block_for(caller.ws) if caller else None
    if caller is None or w is None or w.active_count == 0 or verified is None:
        return pass_response(event)
    try:
        principal = Principal.of_claims(
            verified.claims, w.engine_id if bundle.schema.endswith("/3") else None)
    except codec.PrincipalTokenError:
        return pass_response(event)
    if principal.kind == "system":
        # user ruling (12): exempt — nothing was decided, so nothing is recorded or filtered
        return pass_response(event)
    session = codec.session_key(caller.ws, principal.psid or NO_PSID, principal.kind,
                                principal.id)
    client = deps.engine(bundle, key)
    if method == "tools/list":
        return _filter_list(event, bundle, caller, principal, session, w, client, deps)
    name = tool_name(body)
    action = action_entity(name) if name else None
    if action is None or action not in w.temporal_referenced_actions:
        return pass_response(event)
    shape = response_event(_response_body(event))
    if shape is None:
        return pass_response(event)
    kind, output = shape
    try:
        client.respond({"ws": w.engine_id, "session": session, "action": action, "kind": kind,
                        "input": arguments(body), "output": output,
                        "principal": principal_object(principal),
                        "agent": agent_object(caller, caller_arn(event), schema=bundle.schema),
                        "resource": entity(GATEWAY_ENTITY, bundle.gateway_arn)})
    except (EngineRejected, EngineUnavailable) as exc:
        deps.audit({"launchpad.policy.interceptor": {
            "event": "respond_failed", "ws": caller.ws, "action": name, "kind": kind,
            "code": getattr(exc, "code", "unavailable"), "session_psid": principal.psid}})
    return pass_response(event)


def _filter_list(event: dict[str, Any], bundle: Bundle, caller: Caller, principal: Principal,
                 session: str, w: Attachment, client: EngineClient,
                 deps: Deps) -> dict[str, Any]:
    response = _response_body(event)
    result = response.get("result") if isinstance(response, dict) else None
    tools = result.get("tools") if isinstance(result, dict) else None
    if w.mode != "enforce" or not isinstance(tools, list):
        return pass_response(event)
    names = {action_entity(t["name"]): t["name"] for t in tools
             if isinstance(t, dict) and isinstance(t.get("name"), str)}
    governed = [a for a in names if a in w.governed_actions]
    if not governed:
        return pass_response(event)
    if len(governed) > LIST_FILTER_MAX_ACTIONS:
        # over the engine's limit: unfiltered (visibility only — every call is still decided)
        deps.audit({"launchpad.policy.interceptor": {
            "event": "list_filter_skipped", "level": "WARN", "ws": caller.ws,
            "governed": len(governed), "limit": LIST_FILTER_MAX_ACTIONS}})
        return pass_response(event)
    try:
        visible = set(client.list_filter({
            "ws": w.engine_id,
            "agent": agent_object(caller, caller_arn(event), schema=bundle.schema),
            "principal": principal_object(principal), "actions": governed,
            "resource": entity(GATEWAY_ENTITY, bundle.gateway_arn)}, session))
    except (EngineRejected, EngineUnavailable) as exc:
        deps.audit({"launchpad.policy.interceptor": {
            "event": "list_filter_failed", "ws": caller.ws,
            "code": getattr(exc, "code", "unavailable")}})
        return pass_response(event)
    hidden = {names[a] for a in governed if a not in visible}
    kept = [t for t in tools if not (isinstance(t, dict) and t.get("name") in hidden)]
    filtered = copy.deepcopy(response)
    filtered["result"]["tools"] = kept
    return pass_response(event, filtered)
