"""Stream B1/B2: the gateway interceptor Lambda over the recorded P0 event shapes, against the
stage-0 fake engine (two shards) and a fake S3 (contract
docs/cn/interfaces/policy-principal-contracts.md I1-I4, I7, I8; design §13.5 routing, T7 fail
matrix). Every emitted audit line, JSON-RPC error and engine request is schema-checked."""

from __future__ import annotations

import ast
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.policy import contracts
from app.policy.interceptor import bundle as bundle_mod
from app.policy.interceptor import core, handler
from app.policy.interceptor.bundle import BundleSource, bundle_key, pointer_key
from app.policy.interceptor.engine_client import EngineClient
from tests.policy_principal.conftest import FIXTURES, load_fixture
from tests.policy_principal.fakes import EphemeralSigner, FakeEngine, FakeEngineDown

INTERCEPTOR_DIR = Path(core.__file__).resolve().parent
NOW = load_fixture("claims_valid.json")["now"]
GW = "launchpad-gw-exampleid0"
BUCKET = "launchpad-artifacts-example"
ROLE_ARN = "arn:aws-cn:iam::123456789012:role/launchpad-agent-hr-helpdesk-ab12cd34"
BYOC_ARN = "arn:aws-cn:iam::123456789012:role/launchpad-agent-expense-bot-ef56ab78"
EVAL_ARN = "arn:aws-cn:iam::123456789012:role/launchpad-agent-eval-worker-0a1b2c3d"
PAYOUT = "hr-database___create_payout"
STATELESS_TOOL = "hr-database___list_departments"


def _validator(name: str) -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(contracts.load_schema(name))


def _assert_valid(name: str, instance: Any) -> None:
    errors = sorted(_validator(name).iter_errors(instance), key=str)
    assert not errors, f"{name}: {[e.message for e in errors]}"


def _claims(name: str) -> dict[str, Any]:
    case = next(c for c in load_fixture("claims_valid.json")["cases"] if c["name"] == name)
    return copy.deepcopy(case["claims"])


ENGINE_ID = "pe_0123456789ab"  # the fixture bundle's attached policy engine (= Dogwood set key)
_ATTACHMENT_MODES = {"log_only": "LOG_ONLY", "enforce": "ENFORCE"}


def attachment_overrides(attachment: dict[str, Any], overrides: dict[str, Any]) -> dict | None:
    """The pre-/3 test vocabulary over the /3 attachment: ``mode="off"`` or
    ``gateway_interceptor=False`` = no engine attached (``attachment: null``); a lowercase mode
    is spelled the Global way; anything else updates the block."""
    overrides = dict(overrides)
    if overrides.pop("gateway_interceptor", True) is False or overrides.get("mode") == "off":
        return None
    if "mode" in overrides:
        overrides["mode"] = _ATTACHMENT_MODES.get(overrides["mode"], overrides["mode"])
    attachment.update(overrides)
    return attachment


class World:
    """Fake S3 + two fake engine shards + an audit capture, wired into ``core.Deps``."""

    def __init__(self, signer: EphemeralSigner, hmac_key: bytes, **ws_overrides: Any) -> None:
        self.signer = signer
        self.key = hmac_key
        self.engines = [FakeEngine(key=hmac_key, shard=0), FakeEngine(key=hmac_key, shard=1)]
        for engine in self.engines:
            engine.sets[ENGINE_ID] = "ps-17"
        self.objects: dict[tuple[str, str], bytes] = {}
        self.audit: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str, dict[str, Any], float]] = []
        self.s3_down = False
        bundle = load_fixture("gateway_bundle.json")
        bundle["pubkeys"] = {signer.kid: signer.public_spki_b64}
        ws = bundle["attachment"]
        # one governed tool that no temporal policy references (stateless routing)
        ws["governed_actions"].append(f'AgentCore::Action::"{STATELESS_TOOL}"')
        bundle["attachment"] = attachment_overrides(ws, ws_overrides)
        self.publish(bundle)
        self.source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=self.fetch,
                                   clock=lambda: 0.0)
        self.deps = core.Deps(source=self.source, engine=self.engine, audit=self.audit.append,
                              clock=lambda: float(NOW))

    def publish(self, bundle: dict[str, Any]) -> None:
        _assert_valid("gateway_bundle", bundle)
        self.bundle = bundle
        version = bundle["version"]
        self.objects[(BUCKET, bundle_key(GW, version))] = json.dumps(bundle).encode()
        current = bundle_mod.pointer(GW, version, "2026-10-26T14:00:00+00:00")
        _assert_valid("gateway_bundle_pointer", current)
        self.objects[(BUCKET, pointer_key(GW))] = json.dumps(current).encode()
        self.objects[(BUCKET, bundle["engine"]["key_s3_key"])] = self.key

    def fetch(self, bucket: str, key: str) -> bytes:
        if self.s3_down:
            raise ConnectionError("s3 unreachable")
        return self.objects[(bucket, key)]

    def transport(self, endpoint, method, path, body, headers, timeout):
        shard = self.bundle["engine"]["endpoints"].index(endpoint)
        payload = json.loads(body)
        self.requests.append((path, endpoint, payload, timeout))
        engine = self.engines[shard]
        if engine.slow_s > timeout:
            raise TimeoutError("engine slower than the client budget")
        try:
            return engine.handle(method, path, body, headers, now=float(NOW))
        except FakeEngineDown:
            raise ConnectionRefusedError("down") from None

    def engine(self, bundle, key) -> EngineClient:
        return EngineClient(endpoints=bundle.endpoints, shards=bundle.shards, key=key,
                            transport=self.transport, clock=lambda: float(NOW))

    def set_rule(self, rule) -> None:
        for engine in self.engines:
            engine.rule = rule

    def token(self, claims: dict[str, Any] | None = None) -> str:
        return self.signer.token(claims or _claims("user_member_chat"))

    def run(self, event: dict[str, Any]) -> dict[str, Any]:
        context = SimpleNamespace(aws_request_id="lambda-req-1", client_context=SimpleNamespace(
            custom={"GATEWAY_ARN": self.bundle["gateway_arn"], "REQUEST_ID": "gw-req-1"}))
        out = handler.handle(event, context, self.deps)
        assert '"eyJ' not in json.dumps(self.audit), "a token (JWS) reached the audit"
        for line in self.audit:
            if "launchpad.policy.decision" in line:
                _assert_valid("audit_decision_line", line)
        for path, _, payload, _ in self.requests:
            if path == "/v1/decide":
                _assert_valid("engine_decide_request", payload)
            elif path == "/v1/respond":
                _assert_valid("engine_respond_request", payload)
        return out

    def decisions(self) -> list[dict[str, Any]]:
        return [line["launchpad.policy.decision"] for line in self.audit
                if "launchpad.policy.decision" in line]


@pytest.fixture
def world(signer, hmac_key) -> World:
    return World(signer, hmac_key)


def call_event(token: str | None, *, tool: str = PAYOUT, arn: str = ROLE_ARN,
               fixture: str = "request_tools_call_role.json") -> dict[str, Any]:
    event = copy.deepcopy(load_fixture(f"interceptor_events/{fixture}")["event"])
    request = event["mcp"]["gatewayRequest"]
    params = request["body"]["params"]
    params["name"] = tool
    params["arguments"] = {"employee_id": "EMP-1024", "amount": 420.0}
    if token is None:
        params["_meta"].pop("launchpad/principal")
    else:
        params["_meta"]["launchpad/principal"] = token
    request["context"]["identity"]["awsPrincipalArn"] = arn
    return event


def error_of(out: dict[str, Any]) -> dict[str, Any] | None:
    response = out["mcp"].get("transformedGatewayResponse")
    if response is None:
        return None
    _assert_valid("jsonrpc_policy_error", response["body"])
    assert response["statusCode"] == 200
    return response["body"]["error"]


def forwarded(out: dict[str, Any]) -> dict[str, Any]:
    body = out["mcp"]["transformedGatewayRequest"]["body"]
    assert "launchpad/principal" not in json.dumps(body), "the token must never reach a target"
    return body


def deny_payout(body):
    return ("allow", "deny") if body["action"].endswith(f'"{PAYOUT}"') else ("allow", "allow")


# ── allow / deny / routing ─────────────────────────────────────────────────


def test_allow_forwards_the_call_without_the_token_and_records_one_line(world):
    out = world.run(call_event(world.token()))
    body = forwarded(out)
    assert body["params"]["_meta"] == {"probe_mode": "pass"}
    assert body["params"]["arguments"] == {"employee_id": "EMP-1024", "amount": 420.0}
    (line,) = world.decisions()
    assert line["decision"] == "allow" and line["principal"] == {"kind": "user", "id": "hr-intern"}
    assert line["input_keys"] == ["amount", "employee_id"]
    assert line["session_psid"] == _claims("user_member_chat")["psid"]
    assert "EMP-1024" not in json.dumps(world.audit), "argument values never reach the audit"


def test_engine_deny_is_globals_text_with_the_determining_policy(world):
    world.set_rule(lambda body: ("allow", "deny"))
    for engine in world.engines:
        engine._decide = _with_determining(engine._decide)  # type: ignore[method-assign]
    error = error_of(world.run(call_event(world.token())))
    assert error["code"] == -32002
    reason = "Policy evaluation denied due to pol_payoutadm"
    assert error["message"] == contracts.deny_message(reason)
    assert error["data"]["stage"] == "bt" and error["data"]["policy_id"] == "pol_payoutadm"
    (line,) = world.decisions()
    assert line["decision"] == "deny" and line["determining"] == ["pol_payoutadm:v2"]
    assert line["decision_id"] == error["data"]["decision_id"]

    from app.services import policy_denials

    assert policy_denials.parse_tool_denial(error["message"]) is not None


def _with_determining(inner):
    def decide(body):
        answer = inner(body)
        answer["determining"] = [{"policy_id": "pol_payoutadm", "version": 2, "mode": "active",
                                  "stage": "bt"}]
        return answer
    return decide


def test_stage_a_deny_without_a_policy_is_the_default_deny_reason(world):
    world.set_rule(lambda body: ("deny", "allow"))
    error = error_of(world.run(call_event(world.token())))
    assert error["message"] == contracts.deny_message(contracts.DEFAULT_DENY_REASON)
    assert error["data"]["stage"] == "a"


def test_temporal_referenced_call_goes_to_the_owner_shard_recorded(world):
    from app.policy.contracts import codec

    world.run(call_event(world.token()))
    claims = _claims("user_member_chat")
    session = codec.session_key("default", claims["psid"], "user", "hr-intern")
    owner = codec.shard_for(session, 2)
    (path, endpoint, payload, timeout), = world.requests
    assert path == "/v1/decide" and payload["record"] is True and payload["session"] == session
    assert endpoint == world.bundle["engine"]["endpoints"][owner] and timeout == 1.5
    assert world.engines[owner].events and not world.engines[1 - owner].events


def test_owner_shard_down_fails_closed_without_failover(world):
    from app.policy.contracts import codec

    claims = _claims("user_member_chat")
    owner = codec.shard_for(codec.session_key("default", claims["psid"], "user", "hr-intern"), 2)
    world.engines[owner].down = True
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "engine_unavailable"
    assert len(world.requests) == 1, "a temporal-referenced call never fails over"


def test_stateless_call_retries_once_on_the_other_shard(world):
    from app.policy.contracts import codec

    claims = _claims("user_member_chat")
    owner = codec.shard_for(codec.session_key("default", claims["psid"], "user", "hr-intern"), 2)
    world.engines[owner].down = True
    out = world.run(call_event(world.token(), tool=STATELESS_TOOL))
    forwarded(out)
    assert [(r[2]["record"], r[3]) for r in world.requests] == [(False, 0.1), (False, 0.05)]
    assert world.requests[1][1] == world.bundle["engine"]["endpoints"][1 - owner]


def test_both_shards_down_fail_closed_for_a_stateless_call(world):
    for engine in world.engines:
        engine.down = True
    error = error_of(world.run(call_event(world.token(), tool=STATELESS_TOOL)))
    assert error["data"]["reason_code"] == "engine_unavailable"


def test_slow_engine_over_budget_fails_closed(world):
    for engine in world.engines:
        engine.slow_s = 2.0  # over the 1500 ms owner budget (raised by the fake transport)
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "engine_unavailable"


@pytest.mark.parametrize("code", ["session_invalidated", "history_lost"])
def test_engine_conflict_is_the_i8_conflict(world, code):
    for engine in world.engines:
        engine.force_error = {"status": 409, "code": code}
    error = error_of(world.run(call_event(world.token())))
    assert error["message"] == contracts.CONFLICT_MESSAGE
    assert error["data"] == {"reason_code": code}


def test_set_mismatch_fails_closed_and_forces_a_bundle_refresh(world):
    for engine in world.engines:
        engine.force_error = {"status": 409, "code": "set_mismatch", "have": "ps-16"}
    fetched = []
    inner = world.fetch
    world.source.fetch = lambda b, k: fetched.append(k) or inner(b, k)
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "temporal_set_mismatch"  # amendment 3 / §12.7
    assert bundle_key(GW, 7) in fetched, "the mismatch reloads the bundle"


def test_target_engine_epoch_drives_request_response_and_session(world):
    claims = _claims("user_member_chat")
    claims.update(tsv=99, generation=2, engine_versions={ENGINE_ID: 3, "pe_other": 19})
    token = world.token(claims)
    world.run(call_event(token))
    world.run(response_event(token))
    requests = [payload for path, _, payload, _ in world.requests if path == "/v1/decide"]
    responses = [payload for path, _, payload, _ in world.requests if path == "/v1/respond"]
    expected_psid = core.codec.psid_for(claims["ws"], claims["sid"], 3, 2)
    expected_session = core.codec.session_key(
        claims["ws"], expected_psid, claims["p"]["kind"], claims["p"]["id"])
    assert requests and responses
    assert requests[-1]["tsv"] == 3
    assert requests[-1]["session"] == responses[-1]["session"] == expected_session
    assert requests[-1]["agent"]["workspace_id"] == claims["ws"]
    assert responses[-1]["agent"] == requests[-1]["agent"]
    # An unrelated engine update must not reset this engine's temporal history.
    claims["engine_versions"]["pe_other"] = 20
    world.run(call_event(world.token(claims)))
    assert world.requests[-1][2]["session"] == expected_session


def test_token_without_target_engine_epoch_requires_a_new_turn(world):
    claims = _claims("user_member_chat")
    claims.update(generation=0, engine_versions={"pe_other": 3})
    result = world.run(call_event(world.token(claims)))
    assert error_of(result)["data"]["reason_code"] == "session_invalidated"
    assert not world.requests


@pytest.mark.parametrize("status, code", [(400, "bad_request"), (401, "unauthorized"),
                                          (503, "unavailable")])
def test_other_engine_errors_fail_closed(world, status, code):
    for engine in world.engines:
        engine.force_error = {"status": status, "code": code}
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == f"engine_{code}"


def test_wrong_hmac_key_is_refused_by_the_engine_and_fails_closed(world):
    world.objects[(BUCKET, world.bundle["engine"]["key_s3_key"])] = b"x" * 32
    world.source.refresh(force=True)
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "engine_unauthorized"


# ── principal (D2 / T9 / I1) ───────────────────────────────────────────────


def test_missing_token_is_principal_missing_before_the_session_check(world):
    """Amendment 3 order: with no token there is no psid, so the deny names the principal."""
    error = error_of(world.run(call_event(None)))
    assert error["message"] == contracts.deny_message("principal_missing")
    assert world.requests == []


EVAL_ROLE = "launchpad-agent-eval-worker-0a1b2c3d"


def test_tokenless_system_agent_in_a_temporal_workspace_is_session_required(world):
    # a system caller the hub did NOT mark exempt (never written today; pins the non-exempt path)
    world.bundle["role_map"][EVAL_ROLE]["exempt"] = False
    world.bundle["version"] = 8
    world.publish(world.bundle)
    world.source.refresh(force=True)
    error = error_of(world.run(call_event(None, arn=EVAL_ARN)))
    assert error == {"code": -32602, "message": "policy_session_required"}
    assert world.requests == []


def test_decide_carries_the_amendment_fields_and_never_a_time(world):
    world.run(call_event(world.token()))
    (_, _, payload, _), = world.requests
    claims = _claims("user_member_chat")
    assert (payload["tsv"], payload["session_started_at"]) == (claims["tsv"], claims["iat"])
    assert payload["context"]["expect"] == {"policy_set_version": "ps-17",
                                            "temporal_set_version": 3}
    assert "system" not in payload["context"] and "now" not in json.dumps(payload["context"])
    assert payload["principal"] == {
        "entity": 'AgentCore::User::"hr-intern"', "parents": ['AgentCore::Role::"member"'],
        "attrs": {"username": "hr-intern", "role": "member", "user_id": "u_hrintern01"}}
    assert payload["action"] == 'AgentCore::Action::"hr-database___create_payout"'
    assert "Launchpad::" not in json.dumps(payload)


def test_missing_token_without_temporal_policies_is_principal_missing(signer, hmac_key):
    world = World(signer, hmac_key, temporal_count=0, temporal_referenced_actions=[])
    error = error_of(world.run(call_event(None)))
    assert error["message"] == contracts.deny_message("principal_missing")
    assert error["data"]["reason_code"] == "principal_missing" and error["data"]["stage"] == "bt"


def test_system_agent_without_token_acts_as_itself(signer, hmac_key):
    world = World(signer, hmac_key, temporal_count=0, temporal_referenced_actions=[])
    world.bundle["role_map"][EVAL_ROLE]["exempt"] = False
    world.bundle["version"] = 8
    world.publish(world.bundle)
    world.source.refresh(force=True)
    forwarded(world.run(call_event(None, arn=EVAL_ARN)))
    (_, _, payload, _), = world.requests
    assert payload["principal"]["entity"] == f'AgentCore::IamEntity::"{EVAL_ARN}"'
    assert payload["record"] is False


def test_system_preset_is_exempt_without_an_engine_call(world):
    """`role_map[*].exempt` is the one mechanism (ruling 10: the bundle-level
    `exempt_system_presets` is gone with /3; a preset's entry carries `exempt: true`)."""
    forwarded(world.run(call_event(None, arn=EVAL_ARN)))
    assert world.requests == []
    assert world.decisions()[0]["reason"] == "system_exempt"


# ── user ruling (12), GLOBAL-DEVIATIONS §0b-12 option α: System principals are exempt ─────


def test_a_system_principal_token_passes_without_an_engine_call(world):
    """An evaluation run / a canary acts through an ORDINARY governed agent under a hub-signed
    `System::"eval"` token: the call is passed with an `allow` row `system_exempt` and the
    engine is never asked — the forbid that denies the member (`payout_admin_only`) does not
    apply to it. The member on the same agent / tool is still decided (and denied)."""
    world.set_rule(deny_payout)
    out = world.run(call_event(world.token(_claims("system_eval"))))
    forwarded(out)
    assert error_of(out) is None
    assert world.requests == []
    [row] = world.decisions()
    assert row["decision"] == "allow" and row["reason"] == "system_exempt"
    assert row["principal"] == {"kind": "system", "id": "eval"}
    assert row["agent_id"] == "ag_hrhelp01" and row["policy_ids"] == []
    # the same tool for a member is decided by the engine and denied
    error = error_of(world.run(call_event(world.token())))
    assert error is not None and error["data"]["reason_code"] == "policy"
    assert len(world.requests) == 1
    assert world.decisions()[1]["decision"] == "deny"


def test_an_invalid_system_token_is_still_principal_invalid(world):
    """Exemption needs a VERIFIED System token — a forged / expired one is refused as before."""
    claims = _claims("system_eval")
    claims["exp"] = claims["iat"] - 1
    error = error_of(world.run(call_event(world.token(claims))))
    assert error is not None and "principal_invalid" in error["message"]
    assert world.requests == []


def test_a_system_principal_response_records_and_filters_nothing(world):
    """RESPONSE under a System token: no `/v1/respond` (nothing was decided) and `tools/list`
    is returned unfiltered."""
    world.run(response_event(world.token(_claims("system_eval"))))
    assert world.requests == []
    world.set_rule(deny_payout)
    event = tools_list_response(world.token(_claims("system_eval")))
    out = world.run(event)
    assert out["mcp"]["transformedGatewayResponse"]["body"] == \
        event["mcp"]["gatewayResponse"]["body"]
    assert world.requests == []


INVALID = [c for c in load_fixture("claims_invalid.json")["cases"]]


@pytest.mark.parametrize("case", INVALID, ids=[c["name"] for c in INVALID])
def test_every_invalid_token_fixture_is_denied_with_its_code(world, case):
    claims = _claims(load_fixture("claims_invalid.json")["base"])
    for name, value in case["patch"].items():
        if value == "$delete":
            claims.pop(name, None)
        else:
            claims[name] = value
    error = error_of(world.run(call_event(world.token(claims))))
    assert error["data"]["reason_code"] == "principal_invalid"
    assert error["message"] == contracts.deny_message(f"principal_invalid:{case['expect']}")
    assert world.requests == []


def test_token_from_another_key_and_garbage_are_denied(world):
    other = EphemeralSigner(kid=world.signer.kid).token(_claims("user_member_chat"))
    assert "bad_signature" in error_of(world.run(call_event(other)))["message"]
    assert "malformed" in error_of(world.run(call_event("not-a-jws")))["message"]
    assert "malformed" in error_of(world.run(call_event({"nested": "x"})))["message"]


def test_byoc_agent_needs_the_5_minute_token(world):
    claims = _claims("user_member_chat")
    claims["agt"], claims["aud"] = "ag_byoc0001", ["ag_byoc0001"]
    error = error_of(world.run(call_event(world.token(claims), arn=BYOC_ARN)))
    assert error["message"].endswith("[principal_invalid:ttl_too_long]")
    short = _claims("user_member_byoc_5min")
    short["agt"], short["aud"] = "ag_byoc0001", ["ag_byoc0001"]
    forwarded(world.run(call_event(world.token(short), arn=BYOC_ARN)))


def test_token_for_another_agent_is_not_in_audience(world):
    claims = _claims("user_member_chat")
    claims["agt"], claims["aud"] = "ag_other001", ["ag_other001"]
    error = error_of(world.run(call_event(world.token(claims))))
    assert error["message"].endswith("[principal_invalid:agent_not_in_audience]")


def test_api_key_principal_has_no_parent(world):
    forwarded(world.run(call_event(world.token(_claims("api_key_v1_no_role")))))
    (_, _, payload, _), = world.requests
    assert payload["principal"]["entity"].startswith('AgentCore::ApiKey::"')
    assert payload["principal"]["parents"] == []
    assert set(payload["principal"]["attrs"]) == {"name"}  # amendment 1: name required


# ── callers, modes, fast paths ─────────────────────────────────────────────


def test_unknown_caller_is_denied_when_an_enforcing_workspace_governs_the_tool(world):
    error = error_of(world.run(call_event(
        world.token(), arn="arn:aws-cn:iam::123456789012:role/somebody-else")))
    assert error["data"]["reason_code"] == "unknown_caller" and error["data"]["stage"] == "a"
    assert world.decisions()[0]["principal"] == {"kind": "unknown", "id": "somebody-else"}
    assert world.decisions()[0]["reason"] == "unknown_caller"


def test_unknown_caller_on_an_ungoverned_tool_is_denied(world):
    result = world.run(call_event(None, tool="other___tool",
                                 arn="arn:aws-cn:iam::123456789012:user/example-operator"))
    assert error_of(result)["data"]["reason_code"] == "unknown_caller"


def test_hub_role_needs_a_token_and_takes_its_workspace_from_it(world):
    hub_arn = f"arn:aws-cn:iam::123456789012:role/{world.bundle['hub_role']}"
    forwarded(world.run(call_event(world.token(_claims("user_admin_console")), arn=hub_arn)))
    assert world.requests[0][2]["agent"]["agent_id"] == "hub"
    world.requests.clear()
    assert error_of(world.run(call_event(None, arn=hub_arn)))["data"]["reason_code"] == \
        "unknown_caller"


@pytest.mark.parametrize("overrides", [{"mode": "off"}, {"gateway_interceptor": False}])
def test_workspace_off_passes_without_engine_or_row(signer, hmac_key, overrides):
    world = World(signer, hmac_key, **overrides)
    forwarded(world.run(call_event(None)))
    assert world.requests == [] and world.decisions() == []


def test_zero_active_policies_deny(signer, hmac_key):
    world = World(signer, hmac_key, active_count=0)
    assert error_of(world.run(call_event(None)))["data"]["reason_code"] == "empty_set"
    assert world.requests == []
    assert [d["decision"] for d in world.decisions()] == ["deny"]


@pytest.mark.parametrize("mode,decision", [("enforce", "deny"), ("log_only", "would_deny")])
def test_empty_attached_engine_applies_to_unknown_callers(signer, hmac_key, mode, decision):
    world = World(signer, hmac_key, active_count=0, mode=mode)
    out = world.run(call_event(None, arn="arn:aws-cn:iam::111122223333:role/unknown"))
    if mode == "enforce":
        assert error_of(out)["data"]["reason_code"] == "unknown_caller"
    else:
        forwarded(out)
    assert [d["decision"] for d in world.decisions()] == [decision]


def test_empty_attached_engine_refuses_a_batch(signer, hmac_key):
    world = World(signer, hmac_key, active_count=0)
    event = call_event(None)
    event["mcp"]["gatewayRequest"]["body"] = [event["mcp"]["gatewayRequest"]["body"]]
    assert error_of(world.run(event))["data"]["reason_code"] == "batch_unsupported"


def test_system_caller_is_exempt_even_when_engine_has_no_policies(signer, hmac_key):
    world = World(signer, hmac_key, active_count=0)
    forwarded(world.run(call_event(None, arn=EVAL_ARN)))
    assert world.requests == []
    assert world.decisions()[-1]["reason"] == "system_exempt"


def test_ungoverned_tool_is_denied_without_an_engine_call(world):
    out = world.run(call_event(world.token(), tool="hr-database___not_governed"))
    assert error_of(out)["data"]["reason_code"] == "policy"
    assert world.requests == [] and world.decisions()[-1]["decision"] == "deny"


def test_log_only_turns_denies_and_failures_into_passes_with_rows(signer, hmac_key):
    world = World(signer, hmac_key, mode="log_only")
    world.set_rule(lambda body: ("allow", "deny"))
    forwarded(world.run(call_event(world.token())))
    forwarded(world.run(call_event(None)))  # session required
    for engine in world.engines:
        engine.down = True
    forwarded(world.run(call_event(world.token())))
    assert [d["decision"] for d in world.decisions()] == ["would_deny", "would_deny", "error"]
    assert all(d["mode"] == "LOG_ONLY" for d in world.decisions())


def test_other_methods_pass_with_the_token_stripped(world):
    event = copy.deepcopy(load_fixture("interceptor_events/request_tools_list_user.json")["event"])
    event["mcp"]["gatewayRequest"]["body"]["params"] = {"_meta": {"launchpad/principal": "x"}}
    assert forwarded(world.run(event))["params"] == {}
    for method in ("initialize", "ping", "notifications/initialized"):
        event["mcp"]["gatewayRequest"]["body"]["method"] = method
        forwarded(world.run(event))
    assert world.requests == []


# ── bundle source ──────────────────────────────────────────────────────────


def test_no_bundle_ever_loaded_fails_closed(world):
    world.s3_down = True
    world.source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=world.fetch)
    world.deps.source = world.source
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "bundle_unavailable"


def test_s3_failure_keeps_the_last_good_bundle(world):
    clock = [0.0]
    world.source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=world.fetch,
                                clock=lambda: clock[0])
    world.deps.source = world.source
    forwarded(world.run(call_event(world.token())))
    world.s3_down = True
    clock[0] = 100.0
    forwarded(world.run(call_event(world.token())))
    assert world.source.last_error.startswith("bundle_fetch_failed")


def test_pointer_swap_is_picked_up_after_the_ttl(world):
    """N17-obs483: past the TTL the cached bundle is served while ONE background refresh
    runs; the refreshed bundle governs from the next request."""
    clock, spawned = [0.0], []
    world.source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=world.fetch,
                                clock=lambda: clock[0], spawn=spawned.append)
    world.deps.source = world.source
    forwarded(world.run(call_event(world.token())))
    bundle = copy.deepcopy(world.bundle)
    bundle["version"] = 8
    bundle["attachment"] = None  # the engine was detached
    world.publish(bundle)
    clock[0] = 10.0
    world.requests.clear()
    forwarded(world.run(call_event(world.token())))
    assert world.requests and spawned == [], "inside the TTL the cached bundle still governs"
    clock[0] = 31.0
    world.requests.clear()
    forwarded(world.run(call_event(world.token())))
    assert world.requests, "the request that finds the TTL expired is served from the cache"
    assert len(spawned) == 1, "and starts exactly one background refresh"
    world.run(call_event(world.token()))
    assert len(spawned) == 1, "never a second refresh while one is in flight"
    spawned.pop()()  # the background refresh runs
    world.requests.clear()
    forwarded(world.run(call_event(world.token())))
    assert world.requests == [], "v8 (mode off) governs from the next request"


@pytest.mark.parametrize("mutate", [
    lambda b: b.update(schema="other/1"),
    lambda b: b.update(gateway_id="launchpad-gw-elsewhere"),
    lambda b: b["engine"].update(endpoints=["host.example:7443"]),
    lambda b: b["attachment"].update(mode="sometimes"),
    lambda b: b.pop("role_map"),
    # user ruling (12): `role_map[*].exempt` is a boolean when present
    lambda b: b["role_map"][EVAL_ROLE].update(exempt="yes"),
])
def test_invalid_bundles_are_rejected(signer, mutate):
    raw = load_fixture("gateway_bundle.json")
    raw["pubkeys"] = {signer.kid: signer.public_spki_b64}
    mutate(raw)
    with pytest.raises(bundle_mod.BundleError):
        bundle_mod.parse_bundle(raw, expect_gateway_id=GW)


def test_short_hmac_key_is_never_used(world):
    world.objects[(BUCKET, world.bundle["engine"]["key_s3_key"])] = b"short"
    source = BundleSource(bucket=BUCKET, gateway_id=GW, fetch=world.fetch)
    with pytest.raises(bundle_mod.BundleError):
        source.get()
    assert source.last_error == "engine_key_invalid"


def test_gateway_arn_mismatch_fails_closed(world):
    context = SimpleNamespace(aws_request_id="x", client_context=SimpleNamespace(
        custom={"GATEWAY_ARN": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:gateway/other"}))
    out = handler.handle(call_event(world.token()), context, world.deps)
    assert error_of(out)["data"]["reason_code"] == "gateway_mismatch"


# ── RESPONSE ───────────────────────────────────────────────────────────────


def response_event(token: str | None, *, fixture: str = "response_tools_call_role.json",
                   tool: str = PAYOUT) -> dict[str, Any]:
    event = copy.deepcopy(load_fixture(f"interceptor_events/{fixture}")["event"])
    params = event["mcp"]["gatewayRequest"]["body"]["params"]
    params["name"] = tool
    params["_meta"]["launchpad/principal"] = token
    return event


def test_executed_call_submits_the_response_event_with_parsed_output(world):
    event = response_event(world.token())
    out = world.run(event)
    assert out["mcp"]["transformedGatewayResponse"]["body"] == \
        event["mcp"]["gatewayResponse"]["body"]
    (path, _, payload, _), = world.requests
    assert path == "/v1/respond" and payload["kind"] == "response"
    assert payload["output"]["approved"] is True and payload["output"]["amount_cents"] == 1234
    # amendment 2: pinned to the same (session, principal), with agent + resource
    assert payload["principal"]["entity"] == 'AgentCore::User::"hr-intern"'
    assert payload["agent"]["entity"] == f'AgentCore::IamEntity::"{ROLE_ARN}"'
    assert payload["resource"].startswith('AgentCore::Gateway::"')


def test_policy_short_circuit_submits_an_error_event(world):
    event = response_event(world.token(), fixture="response_after_short_circuit_deny.json")
    world.run(event)
    (path, _, payload, _), = world.requests
    assert path == "/v1/respond" and payload["kind"] == "error"
    assert payload["output"] == {"ok": False}


def test_own_refusals_and_untracked_tools_submit_nothing(world):
    event = response_event(world.token(), fixture="response_after_short_circuit_deny.json")
    event["mcp"]["gatewayResponse"]["body"]["error"]["data"] = {"reason_code": "principal_missing"}
    world.run(event)
    world.run(response_event(world.token(), tool=STATELESS_TOOL))
    assert world.requests == []


def test_response_submit_failure_still_passes_the_response(world):
    for engine in world.engines:
        engine.down = True
    event = response_event(world.token())
    out = world.run(event)
    assert out["mcp"]["transformedGatewayResponse"]["body"] == \
        event["mcp"]["gatewayResponse"]["body"]
    assert world.audit[-1]["launchpad.policy.interceptor"]["event"] == "respond_failed"


def test_non_json_tool_output_records_ok_only():
    assert core.response_event({"result": {"content": [{"type": "text", "text": "plain"}]}}) \
        == ("response", {"ok": True})
    assert core.response_event({"result": {"isError": True, "content": []}}) == \
        ("error", {"ok": False})


def tools_list_response(token: str) -> dict[str, Any]:
    event = response_event(token)
    request = event["mcp"]["gatewayRequest"]["body"]
    request["method"] = "tools/list"
    request["params"] = {"_meta": {"launchpad/principal": token}}
    event["mcp"]["gatewayResponse"]["body"] = {"jsonrpc": "2.0", "id": request["id"], "result": {
        "tools": [{"name": PAYOUT}, {"name": "hr-database___get_employee"},
                  {"name": "x_amz_bedrock_agentcore_search"}]}}
    return event


def test_tools_list_hides_tools_the_principal_may_not_call(world):
    world.set_rule(deny_payout)
    out = world.run(tools_list_response(world.token()))
    names = [t["name"] for t in out["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]]
    assert names == ["hr-database___get_employee", "x_amz_bedrock_agentcore_search"]
    assert world.requests[-1][2]["agent"]["workspace_id"] == "default"


def test_legacy_bundle_omits_the_new_agent_field_for_the_old_engine(world):
    bundle = copy.deepcopy(world.bundle)
    attachment = bundle.pop("attachment")
    attachment.pop("engine_id")
    attachment.update(mode="enforce", published=True, gateway_interceptor=True)
    bundle.update(schema="launchpad.policy.gateway/2", workspaces={"default": attachment})
    # This legacy bundle is intentionally outside the current /3 JSON schema; use the
    # dual-schema parser/source, as R2 does before the hub upgrade.
    world.bundle = bundle
    world.objects[(BUCKET, bundle_key(GW, bundle["version"]))] = json.dumps(bundle).encode()
    world.source.refresh(force=True)
    world.run(call_event(world.token()))
    world.run(response_event(world.token()))
    world.run(tools_list_response(world.token()))
    assert {path for path, *_ in world.requests} == {
        "/v1/decide", "/v1/respond", "/v1/list-filter"}
    assert all("workspace_id" not in payload["agent"] for _, _, payload, _ in world.requests)


def test_tools_list_is_unfiltered_when_the_engine_is_down_or_in_log_only(world, signer, hmac_key):
    for engine in world.engines:
        engine.down = True
    event = tools_list_response(world.token())
    out = world.run(event)
    assert out["mcp"]["transformedGatewayResponse"]["body"] == \
        event["mcp"]["gatewayResponse"]["body"]
    log_only = World(signer, hmac_key, mode="log_only")
    log_only.set_rule(deny_payout)
    out = log_only.run(tools_list_response(log_only.token()))
    assert len(out["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]) == 3


# ── never raise ────────────────────────────────────────────────────────────


def test_an_internal_error_is_a_deny_not_a_raise(world, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(core, "_verify", boom)
    error = error_of(world.run(call_event(world.token())))
    assert error["data"]["reason_code"] == "interceptor_error"
    assert world.audit[-1] == {"launchpad.policy.interceptor": {"event": "internal_error",
                                                               "error": "RuntimeError"}}


def test_an_internal_error_in_log_only_passes(signer, hmac_key, monkeypatch):
    world = World(signer, hmac_key, mode="log_only")
    monkeypatch.setattr(core, "_verify", lambda *a, **k: 1 / 0)
    forwarded(world.run(call_event(world.token())))


def test_handler_without_configuration_never_raises(monkeypatch):
    monkeypatch.setattr(handler, "_DEPS", None)
    monkeypatch.delenv("LAUNCHPAD_POLICY_BUCKET", raising=False)
    out = handler.handler(call_event("t"), None)
    assert out["mcp"]["transformedGatewayResponse"]["body"]["error"]["code"] == -32002


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=12),
    lambda children: st.lists(children, max_size=3) | st.dictionaries(
        st.sampled_from(["mcp", "gatewayRequest", "gatewayResponse", "body", "method", "params",
                         "_meta", "launchpad/principal", "name", "arguments", "context",
                         "identity", "awsPrincipalArn", "id", "result", "content", "tools",
                         "error", "data"]), children, max_size=4),
    max_leaves=25,
)


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(event=_json, method=st.sampled_from(["tools/call", "tools/list", "ping", 3, None]))
def test_fuzzed_events_never_raise(world, event, method):
    if isinstance(event, dict) and isinstance(event.get("mcp"), dict):
        request = event["mcp"].get("gatewayRequest")
        if isinstance(request, dict) and isinstance(request.get("body"), dict):
            request["body"]["method"] = method
    out = handler.handle(event, None, world.deps)
    assert out["interceptorOutputVersion"] == "1.0"
    assert set(out["mcp"]) <= {"transformedGatewayRequest", "transformedGatewayResponse"}


# ── packaging guards ───────────────────────────────────────────────────────


def test_vendored_codec_is_byte_identical_to_the_contract():
    contract = Path(contracts.__file__).resolve().parent / "codec.py"
    assert (INTERCEPTOR_DIR / "codec.py").read_bytes() == contract.read_bytes()


def test_interceptor_package_imports_nothing_from_the_hub():
    allowed = {"__future__", "base64", "binascii", "collections", "copy", "cryptography",
               "dataclasses", "datetime", "hashlib", "hmac", "http", "json", "os", "re", "sys",
               "threading",
               "time", "typing", "uuid", "boto3", "botocore"}
    for path in INTERCEPTOR_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                roots = {(node.module or "").split(".")[0]}
            else:
                continue
            assert roots <= allowed, f"{path.name} imports {roots - allowed}"


def test_recorded_fixtures_are_still_the_ones_the_tests_read():
    assert sorted(p.name for p in (FIXTURES / "interceptor_events").glob("*.json")) == [
        "request_tools_call_headers_on.json", "request_tools_call_role.json",
        "request_tools_list_user.json", "response_after_short_circuit_deny.json",
        "response_tools_call_role.json",
    ]


def test_interceptor_constants_match_the_contract():
    """The Lambda cannot import the contracts package; its copies must not drift."""
    gw = "gw-x"
    assert bundle_mod.pointer_key(gw) == contracts.BUNDLE_POINTER_KEY.format(gateway_id=gw)
    assert bundle_mod.bundle_key(gw, 3) == contracts.BUNDLE_VERSION_KEY.format(
        gateway_id=gw, version=3)
    assert bundle_mod.engine_key_key(gw) == contracts.ENGINE_KEY_S3_KEY.format(gateway_id=gw)
    assert bundle_mod.HMAC_KEY_BYTES == contracts.ENGINE_KEY_BYTES
    assert core.META_KEY == contracts.META_KEY
    assert core.DENY_PREFIX == contracts.DENY_PREFIX
    assert core.DEFAULT_DENY_REASON == contracts.DEFAULT_DENY_REASON
    assert core.CONFLICT_MESSAGE == contracts.CONFLICT_MESSAGE
    assert core.SESSION_REQUIRED_MESSAGE == contracts.SESSION_REQUIRED_MESSAGE
    assert (core.ACTION_NAMESPACE, core.GATEWAY_ENTITY, core.IAM_ENTITY) == (
        contracts.ACTION_NAMESPACE, contracts.GATEWAY_ENTITY, contracts.IAM_ENTITY)
    from app.policy.interceptor import engine_client

    assert (engine_client.STATELESS_TIMEOUT_S, engine_client.STATELESS_RETRY_TIMEOUT_S,
            engine_client.OWNER_TIMEOUT_S) == (
        contracts.ENGINE_STATELESS_TIMEOUT_S, contracts.ENGINE_STATELESS_RETRY_TIMEOUT_S,
        contracts.ENGINE_OWNER_TIMEOUT_S)


def test_a_signature_segment_of_impossible_base64_length_is_malformed(world, signer):
    """Nit 1: a length % 4 == 1 segment raised binascii.Error out of verify_token."""
    from app.policy.contracts import codec as contract_codec

    head, body, _sig = world.token().split(".")
    bad = f"{head}.{body}.AAAAA"  # 5 chars: % 4 == 1
    try:
        contract_codec.verify_token(bad, signer.pubkeys(), now=NOW, workspace_id="default",
                                    env="", agent_id="ag_hrhelp01")
    except contract_codec.PrincipalTokenError as exc:
        assert exc.code == "malformed"
    else:  # pragma: no cover
        raise AssertionError("accepted")
    error = error_of(world.run(call_event(bad)))
    assert error["message"].endswith("[principal_invalid:malformed]")


def test_a_same_named_role_in_another_account_is_an_unknown_caller(world):
    """Nit 2: role_map is keyed by name; the caller's account must be the gateway's."""
    foreign = ROLE_ARN.replace("123456789012", "210987654321")
    error = error_of(world.run(call_event(world.token(), arn=foreign)))
    assert error["data"]["reason_code"] == "unknown_caller"
    assert world.requests == []
    assert core.role_name(ROLE_ARN, "123456789012") == "launchpad-agent-hr-helpdesk-ab12cd34"
    assert core.role_name(
        "arn:aws-cn:sts::123456789012:assumed-role/launchpad-agent-x/session", "123456789012"
    ) == "launchpad-agent-x"


def _batch_event(world, arn=ROLE_ARN):
    event = call_event(world.token(), arn=arn)
    request = event["mcp"]["gatewayRequest"]
    request["body"] = [request["body"], dict(request["body"], id="second")]
    return event


def test_a_jsonrpc_batch_is_denied_under_an_enforcing_workspace(world):
    """Nit 3: batches are not decided call by call, so they are refused, never passed."""
    out = world.run(_batch_event(world))
    error = error_of(out)
    assert out["mcp"]["transformedGatewayResponse"]["body"]["id"] is None
    assert error["data"]["reason_code"] == "batch_unsupported"
    assert world.decisions()[0]["reason"] == "batch_unsupported"
    assert world.requests == []


def test_a_batch_under_log_only_passes_stripped_with_a_row(signer, hmac_key):
    world = World(signer, hmac_key, mode="log_only")
    out = world.run(_batch_event(world))
    body = out["mcp"]["transformedGatewayRequest"]["body"]
    assert isinstance(body, list) and "launchpad/principal" not in json.dumps(body)
    assert world.decisions()[0]["decision"] == "would_deny"


def test_a_batch_on_an_ungoverned_gateway_passes(signer, hmac_key):
    world = World(signer, hmac_key, mode="off")
    assert isinstance(world.run(_batch_event(world))["mcp"]["transformedGatewayRequest"][
        "body"], list)


def test_a_policy_evaluation_error_is_its_own_deny_reason_not_an_outage(world):
    """Stream C fix 1: decision deny + errors[0] == policy_evaluation_error."""
    for engine in world.engines:
        inner = engine._decide

        def decide(body, inner=inner):
            answer = inner(body)
            answer.update(decision="deny", stage_bt="deny",
                          errors=["policy_evaluation_error", "cedar: …"])
            return answer
        engine._decide = decide  # type: ignore[method-assign]
    error = error_of(world.run(call_event(world.token(), tool=STATELESS_TOOL)))
    assert error["message"] == contracts.deny_message("policy_evaluation_error")
    assert error["data"]["reason_code"] == "policy_evaluation_error"
    assert len(world.requests) == 1, "a decided deny is never retried on the other shard"
    # its RESPONSE still submits the error event (the engine decided the call)
    assert core.response_event({"error": {"code": -32002, "message": "x", "data": {
        "reason_code": "policy_evaluation_error"}}}) == ("error", {"ok": False})


def test_tools_list_over_the_engine_limit_is_unfiltered_with_one_warn_line(world):
    many = [f"hr-database___t{i}" for i in range(1025)]
    world.bundle["attachment"]["governed_actions"] = [
        f'AgentCore::Action::"{n}"' for n in many]
    world.bundle["version"] = 8
    world.publish(world.bundle)
    world.source.refresh(force=True)
    event = tools_list_response(world.token())
    event["mcp"]["gatewayResponse"]["body"]["result"]["tools"] = [{"name": n} for n in many]
    out = world.run(event)
    assert len(out["mcp"]["transformedGatewayResponse"]["body"]["result"]["tools"]) == 1025
    assert world.requests == []
    warns = [line["launchpad.policy.interceptor"] for line in world.audit
             if line.get("launchpad.policy.interceptor", {}).get("event") == "list_filter_skipped"]
    assert warns == [{"event": "list_filter_skipped", "level": "WARN", "ws": "default",
                      "governed": 1025, "limit": 1024}]


# ── N17-obs492 (re-review F6): the engine decided on another set than the bundle's ──────


def test_an_engine_answer_from_another_set_fails_closed_in_enforce(world):
    """An engine that ignored ``context.expect`` (or a shard ahead of the bundle) answers with
    its own ``policy_set_version``: in ``enforce`` that is the 409 ``set_mismatch`` case —
    deny ``temporal_set_mismatch`` + a forced bundle refresh — never a silent pass."""
    for engine in world.engines:
        engine.sets[ENGINE_ID] = "ps-99"  # the bundle says ps-17
    error = error_of(world.run(call_event(world.token(), tool=STATELESS_TOOL)))
    assert error is not None and error["data"]["reason_code"] == "temporal_set_mismatch"
    assert [d["decision"] for d in world.decisions()] == ["deny"]
    assert world.decisions()[0]["reason"] == "temporal_set_mismatch"


def test_an_engine_answer_from_another_set_is_a_note_in_log_only(signer, hmac_key):
    world = World(signer, hmac_key, mode="log_only")
    for engine in world.engines:
        engine.sets[ENGINE_ID] = "ps-99"
    out = world.run(call_event(world.token(), tool=STATELESS_TOOL))
    assert error_of(out) is None
    forwarded(out)
    (line,) = world.decisions()
    assert line["decision"] == "allow" and "ps-99" in (line["reason"] or "")


def test_the_response_invocation_settles_a_pending_refresh(world):
    """N17-obs498: the RESPONSE event completes the refresh a REQUEST left in flight (the
    container was frozen the moment the REQUEST handler returned — modelled by a gated S3
    read), so the bundle is fresh for the next REQUEST without a fetch on its path."""
    import threading

    clock = [0.0]
    gate = threading.Event()
    real_fetch = world.source.fetch

    def gated(bucket: str, key: str) -> bytes:
        assert gate.wait(5), "test gate never released"
        return real_fetch(bucket, key)

    world.source.clock = lambda: clock[0]
    gate.set()
    world.run(call_event(world.token(), tool=STATELESS_TOOL))  # loads the bundle at t=0
    confirmed = world.source._confirmed_at
    gate.clear()
    world.source.fetch = gated
    clock[0] = 31.0
    world.run(call_event(world.token(), tool=STATELESS_TOOL))  # past the TTL: spawned, frozen
    assert world.source._in_flight is True
    # the container runs again for the RESPONSE invocation: the thread can only progress once
    # `settle` is entered (otherwise it would finish before the handler looks, a test race)
    real_settle = world.source.settle

    def settle(budget_s: float = bundle_mod.SETTLE_BUDGET_S) -> bool:
        gate.set()
        return real_settle(budget_s)

    world.source.settle = settle
    fixture = load_fixture("interceptor_events/response_tools_call_role.json")
    world.run(copy.deepcopy(fixture["event"]))
    assert world.source.settled == 1 and world.source._confirmed_at == 31.0 != confirmed
    assert world.source._in_flight is False


def test_settle_runs_only_on_the_response_path_never_on_a_request(world):
    """The REQUEST decision latency never carries a refresh: `settle` is called by RESPONSE
    invocations only; a REQUEST past the TTL spawns (frozen) and returns at once."""
    import threading

    clock = [0.0]
    gate = threading.Event()
    gate.set()
    real_fetch = world.source.fetch
    world.source.fetch = lambda bucket, key: (gate.wait(5), real_fetch(bucket, key))[1]
    world.source.clock = lambda: clock[0]
    calls: list[str] = []
    real_settle = world.source.settle

    def settle(budget_s: float = bundle_mod.SETTLE_BUDGET_S) -> bool:
        calls.append("settle")
        gate.set()
        return real_settle(budget_s)

    world.source.settle = settle
    world.run(call_event(world.token(), tool=STATELESS_TOOL))  # t=0: cold load
    clock[0] = 31.0
    gate.clear()  # S3 would stall: a REQUEST must not wait on it
    started = time.perf_counter()
    out = world.run(call_event(world.token(), tool=STATELESS_TOOL))
    assert time.perf_counter() - started < 0.5 and error_of(out) is None
    assert calls == [] and world.source._in_flight is True  # spawned, not settled, not waited
    clock[0] = 100.0
    world.run(call_event(world.token(), tool=STATELESS_TOOL))  # still under max_stale: cached
    assert calls == [] and world.source.settled == 0
    fixture = load_fixture("interceptor_events/response_tools_call_role.json")
    world.run(copy.deepcopy(fixture["event"]))  # the RESPONSE is where it settles
    assert calls == ["settle"] and world.source.settled == 1


def test_a_denied_request_leaves_no_response_and_fail_closed_holds_past_max_stale(world):
    """A denied REQUEST short-circuits — the gateway never invokes the RESPONSE slot, so no
    `settle` runs; with S3 down the next REQUEST past MAX_STALE still refreshes synchronously
    and is denied `bundle_stale` (fail closed exactly as before obs498)."""
    clock = [0.0]
    world.source.clock = lambda: clock[0]
    world.run(call_event(world.token(), tool=STATELESS_TOOL))  # t=0: loaded
    world.set_rule(lambda body: ("deny", "deny"))
    world.s3_down = True  # S3 is down from now on
    clock[0] = 40.0
    out = world.run(call_event(world.token(), tool=STATELESS_TOOL))  # past the TTL: spawned
    assert error_of(out)["data"]["reason_code"] == "policy"  # denied: no RESPONSE will follow
    deadline = time.monotonic() + 5  # the spawned refresh fails; nothing ever settles it
    while world.source._in_flight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert world.source.settled == 0
    clock[0] = 121.0  # past MAX_STALE with no RESPONSE in between
    out = world.run(call_event(world.token(), tool=STATELESS_TOOL))
    error = error_of(out)
    assert error["data"]["reason_code"] == "bundle_stale"
