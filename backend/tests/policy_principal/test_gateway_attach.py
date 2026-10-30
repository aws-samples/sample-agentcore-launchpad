"""Stream B3: the gateway bundle writer (I3) and the interceptor attach / detach on an existing
gateway. UpdateGateway REPLACES the configuration (P0), so the attach carries every field
GetGateway reports — proven here against a fake control plane that applies replace
semantics to the recorded (anonymised) ``launchpad-gw-t1`` GetGateway of the P0 probe."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.core.errors import AppError
from app.policy.interceptor import bundle as interceptor_bundle
from app.services import gateway_bootstrap as gb
from app.services import policy_bundle
from tests.policy_principal.fakes import EphemeralSigner

ACCT = "123456789012"
GW = "launchpad-gw-t1-exampleid"
LAMBDA = f"arn:aws-cn:lambda:cn-northwest-1:{ACCT}:function:launchpad-policy-interceptor-t1"
# P0 evidence get-gateway-launchpad-gw-t1-*.json (2026-10-26), account / ids anonymised,
# after the N17-obs474 repair (no exceptionLevel)
RECORDED_GET_GATEWAY = {
    "gatewayArn": f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:{ACCT}:gateway/{GW}",
    "gatewayId": GW,
    "gatewayUrl": f"https://{GW}.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
    "createdAt": "2026-10-10T07:03:08.288735+00:00",
    "updatedAt": "2026-10-10T07:03:08.930826+00:00",
    "status": "READY",
    "name": "launchpad-gw-t1",
    "description": "AgentCore Launchpad shared MCP gateway",
    "roleArn": f"arn:aws-cn:iam::{ACCT}:role/launchpad-gateway-role-cn-northwest-1-t1",
    "protocolType": "MCP",
    "authorizerType": "AWS_IAM",
    "workloadIdentityDetails": {"workloadIdentityArn": f"arn:aws-cn:bedrock-agentcore:"
                                f"cn-northwest-1:{ACCT}:workload-identity-directory/default/"
                                f"workload-identity/{GW}"},
}
OUTPUT_ONLY = ("gatewayArn", "gatewayId", "gatewayUrl", "createdAt", "updatedAt", "status",
               "workloadIdentityDetails", "statusReasons")


class ReplaceControl:
    """GetGateway / UpdateGateway with the service's REPLACE semantics: an update keeps the
    output-only fields and becomes exactly the request's configuration."""

    def __init__(self, detail: dict[str, Any]) -> None:
        self.detail = deepcopy(detail)
        self.updates: list[dict[str, Any]] = []

    def get_gateway(self, gatewayIdentifier: str) -> dict[str, Any]:
        assert gatewayIdentifier == self.detail["gatewayId"]
        return deepcopy(self.detail)

    def update_gateway(self, **payload: Any) -> dict[str, Any]:
        self.updates.append(deepcopy(payload))
        kept = {k: v for k, v in self.detail.items() if k in OUTPUT_ONLY}
        self.detail = {**kept, **{k: v for k, v in payload.items() if k != "gatewayIdentifier"}}
        return {"gatewayId": self.detail["gatewayId"], "status": "READY"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(gb.time, "sleep", lambda _s: None)


def _config(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in detail.items()
            if k not in OUTPUT_ONLY and k != "interceptorConfigurations"}


def test_attach_round_trips_every_recorded_field_and_adds_only_the_interceptor():
    control = ReplaceControl(RECORDED_GET_GATEWAY)
    report = gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True)
    assert report["action"] == "updated" and report["unchanged_fields_verified"]
    assert _config(control.detail) == _config(RECORDED_GET_GATEWAY)
    assert control.detail["interceptorConfigurations"] == [{
        "interceptor": {"lambda": {"arn": LAMBDA}}, "interceptionPoints": ["REQUEST", "RESPONSE"],
        "inputConfiguration": {"passRequestHeaders": False}}]
    assert gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True)["action"] == "none"
    assert len(control.updates) == 1


def test_detach_restores_the_recorded_configuration():
    control = ReplaceControl(RECORDED_GET_GATEWAY)
    gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True)
    report = gb.set_interceptor(control, GW, LAMBDA, attach=False, apply=True)
    assert report["action"] == "updated"
    assert "interceptorConfigurations" not in control.detail
    assert _config(control.detail) == _config(RECORDED_GET_GATEWAY)
    assert gb.set_interceptor(control, GW, LAMBDA, attach=False, apply=True)["action"] == "none"


def test_dry_run_changes_nothing_and_shows_the_payload():
    control = ReplaceControl(RECORDED_GET_GATEWAY)
    report = gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=False)
    assert report["action"] == "would_update" and control.updates == []
    assert report["payload"]["roleArn"] == RECORDED_GET_GATEWAY["roleArn"]


def test_another_interceptor_in_the_slot_is_never_replaced():
    detail = deepcopy(RECORDED_GET_GATEWAY)
    detail["interceptorConfigurations"] = [gb.interceptor_configuration(
        f"arn:aws-cn:lambda:cn-northwest-1:{ACCT}:function:someone-else")]
    control = ReplaceControl(detail)
    with pytest.raises(RuntimeError, match="another interceptor"):
        gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True)
    assert control.updates == []


@pytest.mark.parametrize("attach", [True, False])
def test_a_debug_gateway_is_refused_until_the_obs474_repair(attach):
    detail = dict(deepcopy(RECORDED_GET_GATEWAY), exceptionLevel="DEBUG")
    if not attach:
        detail["interceptorConfigurations"] = [gb.interceptor_configuration(LAMBDA)]
    control = ReplaceControl(detail)
    with pytest.raises(RuntimeError, match="repair_gateway_exception_level"):
        gb.set_interceptor(control, GW, LAMBDA, attach=attach, apply=True)
    assert control.updates == []


def test_a_read_back_that_lost_a_field_is_an_error():
    class Lossy(ReplaceControl):
        def update_gateway(self, **payload):
            out = super().update_gateway(**payload)
            self.detail.pop("description")
            return out

    with pytest.raises(RuntimeError, match="description"):
        gb.set_interceptor(Lossy(RECORDED_GET_GATEWAY), GW, LAMBDA, attach=True, apply=True)


# ── bundle writer ─────────────────────────────────────────────────────────


def _bundle(signer: EphemeralSigner, version: int = 1) -> dict[str, Any]:
    return policy_bundle.build_gateway_bundle(
        gateway_id=GW, gateway_arn=RECORDED_GET_GATEWAY["gatewayArn"], version=version,
        env="t1", hub_role="launchpad-ec2-role-t1", pubkeys={signer.kid: signer.public_spki_b64},
        role_map={"launchpad-agent-hr-ab12cd34-t1": {
            "agent_id": "ab12cd34ef", "ws": "default", "method": "zip_runtime",
            "system": False, "byoc": False, "exempt": False}},
        attachment=policy_bundle.GatewayAttachmentState(
            engine_id="pe_0123456789ab", mode="LOG_ONLY", active_count=2, temporal_count=0,
            policy_set_version="ps-3", temporal_set_version=0,
            governed_actions=('AgentCore::Action::"hr-database___get_employee"',)),
        endpoints=["10.0.1.10:7443", "10.0.1.10:7444"], shards=2,
    )


def test_built_bundle_matches_the_contract_schema_and_the_lambda_parser():
    import jsonschema

    from app.policy import contracts

    signer = EphemeralSigner()
    bundle = _bundle(signer)
    jsonschema.validate(bundle, contracts.load_schema("gateway_bundle"))
    parsed = interceptor_bundle.parse_bundle(bundle, expect_gateway_id=GW)
    assert parsed.key_s3_key == f"policy/gateway/{GW}/engine-key"
    # Global parity: the knob defaults to Cedar's default-deny
    assert parsed.empty_set_decision == "deny" and parsed.schema == interceptor_bundle.SCHEMA


def test_an_invalid_bundle_is_never_built():
    signer = EphemeralSigner()
    with pytest.raises(AppError) as err:
        policy_bundle.build_gateway_bundle(
            gateway_id=GW, gateway_arn="x", version=1, env="", hub_role="h",
            pubkeys={signer.kid: signer.public_spki_b64}, role_map={},
            attachment=None, endpoints=["not-an-ip:7443"], shards=2)
    assert err.value.code == "policy.gateway_bundle_invalid"


class FakeS3:
    """put / get with S3's conditional-write semantics (IfNoneMatch="*", IfMatch=<etag>)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []
        self.before_put = None  # hook: runs inside put_object, before the condition check

    @staticmethod
    def _etag(body: bytes) -> str:
        import hashlib

        return '"' + hashlib.md5(body).hexdigest() + '"'  # S3's ETag shape

    def get_object(self, *, Bucket, Key):
        import io

        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": self._etag(body)}

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, IfMatch=None):
        if self.before_put is not None:
            hook, self.before_put = self.before_put, None
            hook(Key)
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        if IfMatch is not None and (Key not in self.objects
                                    or self._etag(self.objects[Key]) != IfMatch):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.objects[Key] = Body
        self.order.append(Key)


class Workspace:
    def __init__(self, s3: FakeS3) -> None:
        self.s3 = s3

    def client(self, name: str):
        assert name == "s3"
        return self.s3


def test_writer_puts_the_immutable_bundle_then_swaps_the_pointer_the_lambda_follows():
    signer = EphemeralSigner()
    s3 = FakeS3()
    key, version = policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer))
    assert (key, version) == (f"policy/gateway/{GW}/v1.json", 1)
    pointer = f"policy/gateway/{GW}/current.json"
    assert s3.order == [key, pointer]
    s3.objects[f"policy/gateway/{GW}/engine-key"] = b"k" * 32  # exactly 32 raw bytes
    source = interceptor_bundle.BundleSource(bucket="bkt", gateway_id=GW,
                                             fetch=lambda _b, k: s3.objects[k])
    bundle, hmac_key = source.get()
    assert bundle.version == 1 and hmac_key == b"k" * 32
    # a taken version (a resumed publish) moves to the next free one; the pointer follows
    key2, version2 = policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer))
    current = json.loads(s3.objects[pointer])
    assert version2 == 2 and (current["version"], current["key"]) == (2, key2)
    import jsonschema

    from app.policy import contracts

    jsonschema.validate(current, contracts.load_schema("gateway_bundle_pointer"))
    source.refresh()
    assert source.get()[0].version == 2


def test_role_map_names_every_live_agent_by_its_execution_role():
    from app.core.db import SessionLocal
    from app.models.ledger import Agent
    from app.services import agent_iam

    with SessionLocal() as db:
        live = Agent(name="hr-help", method="zip_runtime", status="ready", spec={})
        preset = Agent(name="eval-worker", method="zip_runtime", status="ready", spec={},
                       system_key="eval_worker", workspace_id="default")
        byoc = Agent(name="exp", method="byoc", status="ready", spec={}, workspace_id="ws2")
        gone = Agent(name="old", method="zip_runtime", status="deleted", spec={})
        db.add_all([live, preset, byoc, gone])
        db.commit()
        role_map = policy_bundle.gateway_role_map(db)
        name = agent_iam.role_name_for
        assert role_map[name(live.name, live.id)] == {
            "agent_id": live.id, "ws": "default", "method": "zip_runtime", "system": False,
            "byoc": False, "exempt": False}
        # user ruling (12): a system preset is exempt from policy (additive `exempt` field)
        assert role_map[name(preset.name, preset.id)]["system"] is True
        assert role_map[name(preset.name, preset.id)]["exempt"] is True
        assert role_map[name(byoc.name, byoc.id)] == {
            "agent_id": byoc.id, "ws": "ws2", "method": "byoc", "system": False, "byoc": True,
            "exempt": False}
        # the composed role_map round-trips through the Lambda parser with `exempt` read
        signer = EphemeralSigner()
        bundle = policy_bundle.build_gateway_bundle(
            gateway_id=GW, gateway_arn=RECORDED_GET_GATEWAY["gatewayArn"], version=1, env="",
            hub_role="h", pubkeys={signer.kid: signer.public_spki_b64},
            role_map=role_map, attachment=None, endpoints=["10.0.1.10:7443"], shards=1)
        parsed = interceptor_bundle.parse_bundle(bundle, expect_gateway_id=GW)
        assert parsed.role_map[name(preset.name, preset.id)].exempt is True
        assert parsed.role_map[name(live.name, live.id)].exempt is False
        assert name(gone.name, gone.id) not in role_map


def test_attach_script_targets_only_platform_or_probe_gateways_and_our_function():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "attach_policy_interceptor.py"
    spec = importlib.util.spec_from_file_location("attach_policy_interceptor", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.target_allowed("launchpad-gw", probe=False)
    assert mod.target_allowed("launchpad-gw-t1", probe=False)
    assert not mod.target_allowed("launchpad-probe-gw", probe=False)
    assert mod.target_allowed("launchpad-probe-gw", probe=True)
    assert not mod.target_allowed("launchpad-gw", probe=True)
    assert mod._FUNCTION.match(LAMBDA)
    assert not mod._FUNCTION.match(LAMBDA.replace("policy-interceptor-t1", "evalfn-x"))
    assert mod.main(["--function-arn", "arn:aws-cn:lambda:x"]) == 2


def _pointer_version(s3: FakeS3) -> int:
    return json.loads(s3.objects[f"policy/gateway/{GW}/current.json"])["version"]


def test_a_writer_never_moves_the_pointer_back_to_an_older_bundle():
    """D1: writer A (v5) and writer B (v6) race; A's pointer write arriving last must not
    roll every Lambda back to v5."""
    signer = EphemeralSigner()
    s3 = FakeS3()
    policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer, version=6))
    assert _pointer_version(s3) == 6
    key, version = policy_bundle.write_gateway_bundle(Workspace(s3), "bkt",
                                                      _bundle(signer, version=5))
    assert (key, version) == (f"policy/gateway/{GW}/v5.json", 5)  # the bundle itself is kept
    assert _pointer_version(s3) == 6


def test_a_pointer_changed_between_read_and_write_is_re_read():
    """The swap is conditional on the ETag it read: a newer pointer landing in between makes
    the write fail its precondition, the writer re-reads and then keeps the newer one."""
    signer = EphemeralSigner()
    s3 = FakeS3()
    policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer, version=1))
    pointer = f"policy/gateway/{GW}/current.json"

    def newer_lands(key):
        if key == pointer:
            from app.policy.interceptor.bundle import pointer as make

            s3.objects[pointer] = json.dumps(make(GW, 9, "t")).encode()
            s3.before_put = None

    s3.before_put = lambda key: setattr(s3, "before_put", newer_lands) if key != pointer \
        else newer_lands(key)
    policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer, version=2))
    assert _pointer_version(s3) == 9


def test_the_lambda_does_not_follow_a_pointer_backwards_unless_forced():
    from app.policy.interceptor.bundle import pointer as make

    signer = EphemeralSigner()
    s3 = FakeS3()
    for v in (1, 2):
        policy_bundle.write_gateway_bundle(Workspace(s3), "bkt", _bundle(signer, version=v))
    s3.objects[f"policy/gateway/{GW}/engine-key"] = b"k" * 32
    clock = [0.0]
    source = interceptor_bundle.BundleSource(bucket="bkt", gateway_id=GW,
                                             fetch=lambda _b, k: s3.objects[k],
                                             clock=lambda: clock[0])
    assert source.get()[0].version == 2
    s3.objects[f"policy/gateway/{GW}/current.json"] = json.dumps(make(GW, 1, "t")).encode()
    clock[0] = 100.0
    assert source.get()[0].version == 2
    assert source.last_error == "pointer_behind_loaded"
    source.refresh(force=True)
    assert source.get()[0].version == 1


def test_a_service_added_default_on_our_entry_does_not_fail_the_read_back():
    class Defaults(ReplaceControl):
        def update_gateway(self, **payload):
            out = super().update_gateway(**payload)
            for entry in self.detail.get("interceptorConfigurations") or []:
                entry.setdefault("inputConfiguration", {})["somethingNew"] = True
            return out

    report = gb.set_interceptor(Defaults(RECORDED_GET_GATEWAY), GW, LAMBDA, attach=True,
                                apply=True)
    assert report["action"] == "updated"


# ── persisted attach state (stream A's guard-disarm reads it) ────────────────


def _recorder():
    from app.core.db import SessionLocal
    from app.services import policy_gateway_state

    def record(attached: bool) -> None:
        with SessionLocal() as db:
            if attached:
                policy_gateway_state.record_attach(db, GW, LAMBDA)
            else:
                policy_gateway_state.record_detach(db, GW)
    return record


def _state():
    from app.core.db import SessionLocal
    from app.services import policy_gateway_state

    with SessionLocal() as db:
        return (policy_gateway_state.gateway_interceptor_attached(db, GW),
                policy_gateway_state.attachment(db, GW))


def test_attach_sets_and_detach_clears_the_persisted_state():
    control = ReplaceControl(RECORDED_GET_GATEWAY)
    assert _state() == (False, None)
    gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True, record=_recorder())
    attached, row = _state()
    assert attached and row["interceptor_arn"] == LAMBDA and row["attached_at"]
    assert row["detached_at"] is None
    gb.set_interceptor(control, GW, LAMBDA, attach=False, apply=True, record=_recorder())
    attached, row = _state()
    assert not attached and row["detached_at"]


def test_a_failed_read_back_leaves_the_state_unset():
    class Lossy(ReplaceControl):
        def update_gateway(self, **payload):
            out = super().update_gateway(**payload)
            self.detail.pop("description")
            return out

    with pytest.raises(RuntimeError):
        gb.set_interceptor(Lossy(RECORDED_GET_GATEWAY), GW, LAMBDA, attach=True, apply=True,
                           record=_recorder())
    assert _state() == (False, None)


def test_dry_runs_and_refusals_never_record():
    calls = []
    gb.set_interceptor(ReplaceControl(RECORDED_GET_GATEWAY), GW, LAMBDA, attach=True,
                       apply=False, record=calls.append)
    detail = dict(deepcopy(RECORDED_GET_GATEWAY), exceptionLevel="DEBUG")
    with pytest.raises(RuntimeError):
        gb.set_interceptor(ReplaceControl(detail), GW, LAMBDA, attach=True, apply=True,
                           record=calls.append)
    assert calls == []


def test_an_already_attached_gateway_reconciles_the_state():
    """The gateway already carries our entry (attached outside this ledger, or a lost
    write): the verified no-op still records it."""
    detail = deepcopy(RECORDED_GET_GATEWAY)
    entry = gb.interceptor_configuration(LAMBDA)
    entry["inputConfiguration"]["serviceDefault"] = True  # compared on our fields only
    detail["interceptorConfigurations"] = [entry]
    control = ReplaceControl(detail)
    report = gb.set_interceptor(control, GW, LAMBDA, attach=True, apply=True,
                                record=_recorder())
    assert report["action"] == "none" and control.updates == []
    assert _state()[0] is True


# ── N17-obs488 point 3: the bundle refuses bare action ids (fail closed at compose + parse) ──


def test_a_bundle_with_bare_action_ids_is_refused_by_the_schema_the_parser_and_the_builder():
    """The hub-written gw-t1 bundle v2 (2026-10-27) carried `governed_actions:
    ["hr-database___get_employee", …]` — bare tool names that govern nothing because the Lambda
    compares `AgentCore::Action::"<tool>"` strings. The schema `pattern`, the Lambda's parser and
    (through the parser) `build_gateway_bundle` all refuse such a bundle now; an entity-formatted
    one — the M1 shape and what the hub composes — is accepted."""
    import copy

    import jsonschema

    from app.policy import contracts
    from app.policy.interceptor import bundle as ib

    signer = EphemeralSigner()
    good = _bundle(signer)
    jsonschema.validate(good, contracts.load_schema("gateway_bundle"))
    parsed = interceptor_bundle.parse_bundle(good, expect_gateway_id=GW)
    assert parsed.any_enforcing('AgentCore::Action::"hr-database___get_employee"') == (False, True)

    bare = copy.deepcopy(good)
    bare["attachment"]["governed_actions"] = ["hr-database___get_employee"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bare, contracts.load_schema("gateway_bundle"))
    with pytest.raises(ib.BundleError) as err:
        interceptor_bundle.parse_bundle(bare, expect_gateway_id=GW)
    assert "governed_actions" in str(err.value)
    bare_temporal = copy.deepcopy(good)
    bare_temporal["attachment"]["temporal_referenced_actions"] = ["hr-database___x"]
    with pytest.raises(ib.BundleError):
        interceptor_bundle.parse_bundle(bare_temporal, expect_gateway_id=GW)

    # the builder goes through the same parser: a bare state never becomes a published bundle
    with pytest.raises(AppError) as built:
        policy_bundle.build_gateway_bundle(
            gateway_id=GW, gateway_arn=RECORDED_GET_GATEWAY["gatewayArn"], version=2, env="t1",
            hub_role="launchpad-ec2-role-t1", pubkeys={signer.kid: signer.public_spki_b64},
            role_map={}, attachment=policy_bundle.GatewayAttachmentState(
                engine_id="pe_0123456789ab", mode="ENFORCE", active_count=2, temporal_count=0,
                policy_set_version="ps-3", temporal_set_version=0,
                governed_actions=("hr-database___get_employee",)),
            endpoints=["10.0.1.10:7443", "10.0.1.10:7444"], shards=2)
    assert built.value.code == "policy.gateway_bundle_invalid"
    assert "governed_actions" in str(built.value.message)
