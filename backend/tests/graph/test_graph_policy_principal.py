# ruff: noqa: F811  — pytest fixtures are imported by name from their modules
"""E07 — one policy ENGINE attached to two gateways, walked through the real routes (round 17;
rewritten 2026-10-28 by pg-contracts for contract amendment 11 — the Global AgentCore Policy
model; the r17-bl shape walked the workspace switch / stale carrier / apply, all removed).

Graph: ``tests/graph/graphs/policy_principal.yaml``. Hermetic: the policy store's real publish
path installs the engine's ONE Dogwood set on the stage-0 fake shards (``test_engine_sync``'s
``compiler`` / ``engines``), the gateway bundle writer runs over a CAS fake S3
(``test_gateway_publish.cas``), the Lambda attach runs against a replace-semantics fake Gateway
control plane holding TWO gateways, the KMS signer is an in-memory P-256 key (``kms``), and the
interceptor Lambda runs in-process over the REAL composed ``/3`` bundle of each gateway (the test
signer's public key swapped into ``pubkeys``) with the fake shards keyed by the ENGINE id.
Accounts: the administrator creates / attaches / detaches, bob is a member (403).

Until the hub's engine routes land (pg-backend, feature-detected on
``app.services.policy_engines``) every path is a STRICT xfail — the moment the module exists the
marks vanish and the paths must pass.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
import time
import uuid
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.models.policy import Policy, PolicyChange, PolicyDecision
from app.policy import contracts
from app.policy.interceptor import bundle as bundle_mod
from app.policy.interceptor import core
from app.policy.interceptor.bundle import BundleSource, bundle_key, pointer_key
from app.policy.interceptor.engine_client import EngineClient
from app.services import agent_iam, policy_audit, policy_gateway_state
from app.services.agentcore import client as agentcore_client
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.walks import run_walk, walk_seeds
from tests.policy_principal.conftest import signer  # noqa: F401
from tests.policy_principal.fakes import EphemeralSigner, FakeEngineDown, ephemeral_hmac_key
from tests.policy_principal.test_engine_sync import compiler, engines  # noqa: F401
from tests.policy_principal.test_gateway_attach import OUTPUT_ONLY, RECORDED_GET_GATEWAY
from tests.policy_principal.test_gateway_publish import (  # noqa: F401
    BUCKET,
    GW,
    GW_ARN,
    cas,
    gateway,
)
from tests.policy_principal.test_interceptor import NOW, _claims, call_event, error_of, forwarded
from tests.policy_principal.test_mint_entrances import kms  # noqa: F401
from tests.test_policy_deploy_narrowing import _agent, engine, s3  # noqa: F401
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

# Global model (policy-engines contract, 2026-10-28): the E07 graph's `switch_on` / `apply`
# edges (workspace interceptor switch → stale carrier → POST /api/policies/apply) are retired
# behaviour — the carrier is part of every deploy and a gateway's posture is its engine
# attachment. The graph (tests/graph/graphs/policy_principal.yaml) and this world need the
# attach-based rewrite (register row from the lead); until then the module is skipped, never
# weakened.

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
WS = DEFAULT_WORKSPACE_ID
LANDED = importlib.util.find_spec("app.services.policy_engines") is not None
PENDING = pytest.mark.xfail(not LANDED, strict=True, reason="pg-backend pending")
pytestmark = PENDING

GW_A, GW_A_ARN = GW, GW_ARN
GW_B = "launchpad-gw-shared02"
GW_B_ARN = f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/{GW_B}"
LAMBDA = "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:launchpad-policy-interceptor"
SCHEMA_V3 = "launchpad.policy.gateway/3"
GET_EMPLOYEE, CREATE_PAYOUT = "hr-database___get_employee", "hr-database___create_payout"
FORBID_PAYOUT = (
    'forbid(principal, action == Launchpad::Action::"call_tool", '
    'resource == Launchpad::Tool::"hr-database___create_payout");'
)
ENGINES = "/api/policy-engines"


def _attach_url(gateway_id: str) -> str:
    return f"/api/governance/gateways/{gateway_id}/policy-engine"


# ── fake Gateway control plane: two gateways, replace semantics ──────────────────────────


class TwoGatewayControl:
    """GetGateway / UpdateGateway for gateways A and B with the service's REPLACE semantics
    (``test_gateway_attach.ReplaceControl``, generalised). ``bundle_seen_at_update`` records,
    per update, whether the gateway's bundle pointer already existed in the CAS S3 — the
    amendment-11 order "bundle first, then the Lambda"."""

    def __init__(self, s3: Any) -> None:
        self.s3 = s3
        self.gateways: dict[str, dict[str, Any]] = {}
        for gid, arn in ((GW_A, GW_A_ARN), (GW_B, GW_B_ARN)):
            detail = copy.deepcopy(RECORDED_GET_GATEWAY)
            detail.update(gatewayId=gid, gatewayArn=arn, name=gid,
                          gatewayUrl=f"https://{gid}.gateway.example.test/mcp")
            self.gateways[gid] = detail
        self.updates: list[tuple[str, list[Any]]] = []
        self.bundle_seen_at_update: dict[str, list[bool]] = {GW_A: [], GW_B: []}

    def list_gateways(self, **_kw: Any) -> dict[str, Any]:
        return {"items": [copy.deepcopy(g) for g in self.gateways.values()]}

    def get_gateway(self, gatewayIdentifier: str) -> dict[str, Any]:
        return copy.deepcopy(self.gateways[gatewayIdentifier])

    def update_gateway(self, **payload: Any) -> dict[str, Any]:
        gid = payload["gatewayIdentifier"]
        detail = self.gateways[gid]
        kept = {k: v for k, v in detail.items() if k in OUTPUT_ONLY}
        self.gateways[gid] = {**kept, **{k: v for k, v in payload.items()
                                         if k != "gatewayIdentifier"}}
        self.updates.append((gid, list(payload.get("interceptorConfigurations") or [])))
        self.bundle_seen_at_update[gid].append(pointer_key(gid) in self.s3.objects)
        return {"gatewayId": gid, "status": "READY"}

    def list_tags_for_resource(self, **_kw: Any) -> dict[str, Any]:
        return {"tags": {}}

    def list_gateway_targets(self, **_kw: Any) -> dict[str, Any]:
        return {"items": []}


@pytest.fixture
def seams(monkeypatch, engine, s3, cas, gateway, compiler, engines):
    """Every seam the path reaches (module docstring): the fake shards (keyed by the engine id
    once the hub installs under it), the CAS S3 the bundle writer and the interceptor read, the
    two-gateway control plane behind every ``control_client`` import, the interceptor Lambda ARN."""
    control = TwoGatewayControl(cas)
    from app.services import policy_engines

    monkeypatch.setattr(policy_engines, "gateway_snapshot",
                        lambda facts: control.get_gateway(facts["gateway_id"]))
    monkeypatch.setattr(policy_engines, "interceptor_hook",
                        policy_engines._default_interceptor_hook)
    monkeypatch.setattr(policy_engines, "_ensure_engine_key", lambda facts: None)
    monkeypatch.setattr(policy_engines, "_grant_gateway_invoke", lambda facts: None)
    original = agentcore_client.control_client
    monkeypatch.setattr(agentcore_client, "control_client", lambda _ctx=None: control)
    for module in list(sys.modules.values()):
        if getattr(module, "control_client", None) is original:
            monkeypatch.setattr(module, "control_client", lambda _ctx=None: control)
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    for name in ("policy_interceptor_lambda_arn", "policy_interceptor_arn"):
        if hasattr(get_settings(), name):
            monkeypatch.setattr(get_settings(), name, LAMBDA)
    shards, seen = engines
    signer = EphemeralSigner()
    return {"control": control, "cas": cas, "shards": shards, "seen": seen,
            "signer": signer, "hmac_key": ephemeral_hmac_key(), "compiler": compiler}


# ── the interceptor over a REAL composed bundle of one gateway ─────────────────────────────


class GatewayInterceptor:
    """The Lambda in-process over ONE gateway's live bundle (the test signer's key swapped into
    ``pubkeys``), two fake shards keyed by the ENGINE id the attachment names."""

    def __init__(self, seams: dict[str, Any], gateway_id: str, bundle: dict[str, Any]) -> None:
        from tests.policy_principal.fakes import FakeEngine

        self.gateway_id = gateway_id
        self.bundle = copy.deepcopy(bundle)
        self.bundle["pubkeys"] = {seams["signer"].kid: seams["signer"].public_spki_b64}
        self.signer, self.key = seams["signer"], seams["hmac_key"]
        self.engines = [FakeEngine(key=self.key, shard=i) for i in range(2)]
        attachment = self.bundle.get("attachment")
        if attachment:
            for shard in self.engines:
                shard.sets[attachment["engine_id"]] = attachment["policy_set_version"]
        version = self.bundle["version"]
        self.objects: dict[tuple[str, str], bytes] = {
            (BUCKET, bundle_key(gateway_id, version)): json.dumps(self.bundle).encode(),
            (BUCKET, pointer_key(gateway_id)): json.dumps(bundle_mod.pointer(
                gateway_id, self.bundle["version"], "2026-10-28T02:00:00+00:00")).encode(),
            (BUCKET, self.bundle["engine"]["key_s3_key"]): self.key,
        }
        self.audit: list[dict[str, Any]] = []
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.source = BundleSource(bucket=BUCKET, gateway_id=gateway_id, fetch=self.fetch,
                                   clock=lambda: 0.0)
        self.deps = core.Deps(source=self.source, engine=self.engine, audit=self.audit.append,
                              clock=lambda: float(NOW))

    def fetch(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    def transport(self, endpoint, method, path, body, headers, timeout):
        shard = self.bundle["engine"]["endpoints"].index(endpoint)
        self.requests.append((path, json.loads(body)))
        try:
            return self.engines[shard].handle(method, path, body, headers, now=float(NOW))
        except FakeEngineDown:
            raise ConnectionRefusedError("down") from None

    def engine(self, bundle, key) -> EngineClient:
        return EngineClient(endpoints=bundle.endpoints, shards=bundle.shards, key=key,
                            transport=self.transport, clock=lambda: float(NOW))

    def set_rule(self, rule) -> None:
        for shard in self.engines:
            shard.rule = rule

    def run(self, event: dict[str, Any]) -> dict[str, Any]:
        from types import SimpleNamespace

        from app.policy.interceptor import handler

        context = SimpleNamespace(aws_request_id="lambda-req-1", client_context=SimpleNamespace(
            custom={"GATEWAY_ARN": self.bundle["gateway_arn"], "REQUEST_ID": "gw-req-1"}))
        out = handler.handle(event, context, self.deps)
        assert '"eyJ' not in json.dumps(self.audit), "a token (JWS) reached the audit"
        return out

    def decisions(self) -> list[dict[str, Any]]:
        return [line["launchpad.policy.decision"] for line in self.audit
                if "launchpad.policy.decision" in line]


def _strip_volatile(bundle: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in bundle.items()
           if k not in ("version", "issued_at", "gateway_id", "gateway_arn")}
    if isinstance(out.get("attachment"), dict):
        out["attachment"] = {k: v for k, v in out["attachment"].items() if k != "mode"}
    out["engine"] = {k: v for k, v in out["engine"].items() if k != "key_s3_key"}
    return out


# ── the world ───────────────────────────────────────────────────────────────────────────


class PolicyPrincipalWorld(World):
    graph = "policy_principal"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}

    def __init__(self, clients, seams) -> None:
        super().__init__(clients)
        self.seams = seams
        self.cas = seams["cas"]
        self.control: TwoGatewayControl = seams["control"]
        self.name = f"g_pe_{uuid.uuid4().hex[:8]}"
        self.engine_id: str | None = None
        self.policy_id: str | None = None
        self.set_version: str | None = None
        # the calling agent the interceptor maps through the bundle role_map (gateway tools)
        self.agent_id = f"ag_pp_{uuid.uuid4().hex[:8]}"
        _agent(agent_id=self.agent_id, name=f"pp-{self.agent_id[-8:]}")
        self.ids.add(self.agent_id)
        self.interceptors: dict[str, GatewayInterceptor] = {}
        self.events: list[dict[str, Any]] = []

    # -- helpers -------------------------------------------------------------------
    def extra_counts(self) -> dict[str, int]:
        """A refusal must leave the policy ledger AND the bundle store untouched (the base
        digest does not watch the policy tables)."""
        db = SessionLocal()
        try:
            out = {
                "policies": db.query(Policy).count(),
                "policy_changes": db.query(PolicyChange).count(),
                "policy_decisions": db.query(PolicyDecision).count(),
                "bundle_objects": len(self.cas.objects),
                "gateway_updates": len(self.control.updates),
            }
            try:
                from app.models.policy import PolicyEngine, PolicyEngineAttachment

                out["policy_engines"] = db.query(PolicyEngine).count()
                out["policy_engine_attachments"] = db.query(PolicyEngineAttachment).count()
            except ImportError:
                pass
            return out
        finally:
            db.close()

    def live_bundle(self, gateway_id: str) -> dict[str, Any]:
        pointer = json.loads(self.cas.objects[pointer_key(gateway_id)])
        return json.loads(self.cas.objects[pointer["key"]])

    def lambda_attached(self, gateway_id: str) -> bool:
        db = SessionLocal()
        try:
            return policy_gateway_state.gateway_interceptor_attached(db, gateway_id)
        finally:
            db.close()

    def engine_view(self) -> dict[str, Any]:
        res = self.admin.get(f"{ENGINES}/{self.engine_id}")
        assert res.status_code == 200, res.text
        return res.json()

    def caller_arn(self) -> str:
        db = SessionLocal()
        try:
            row = db.get(Agent, self.agent_id)
            return f"arn:aws-cn:iam::111122223333:role/{agent_iam.role_name_for(row.name, row.id)}"
        finally:
            db.close()

    def token(self, tsv: int) -> str:
        claims = _claims("user_member_chat")
        claims.update(ws=WS, agt=self.agent_id, aud=[self.agent_id], tsv=tsv)
        return self.seams["signer"].token(claims)

    def interceptor(self, gateway_id: str, *, fresh: bool = False) -> GatewayInterceptor:
        if fresh or gateway_id not in self.interceptors:
            self.interceptors[gateway_id] = GatewayInterceptor(
                self.seams, gateway_id, self.live_bundle(gateway_id))
        return self.interceptors[gateway_id]

    def gateway_call(self, world: GatewayInterceptor, tool: str) -> dict[str, Any]:
        attachment = world.bundle.get("attachment") or {}
        event = call_event(self.token(int(attachment.get("temporal_set_version") or 0)),
                           tool=tool, arn=self.caller_arn())
        event["mcp"]["gatewayRequest"]["body"]["id"] = f"rpc-{uuid.uuid4().hex[:12]}"
        return event

    def capture_lines(self, world: GatewayInterceptor) -> None:
        now_ms = int(time.time() * 1000)
        for line in world.audit:
            if "launchpad.policy.decision" not in line:
                continue
            message = json.dumps(line)
            if all(e["message"] != message for e in self.events):
                self.events.append({"timestamp": now_ms, "message": message})
                self.ids.add(line["launchpad.policy.decision"]["decision_id"])

    # -- edges -----------------------------------------------------------------------
    def edge_create_engine(self, edge) -> None:
        res = self.admin.post(ENGINES, json={"name": self.name, "description": "graph"})
        body = self.expect(res, edge)
        self.engine_id = body["id"]
        assert self.engine_id.startswith("pe_") and len(self.engine_id) == 15, body
        self.ids.add(self.engine_id)
        assert body.get("policy_count", 0) == 0 and body.get("attachments", []) == [], body

    def edge_member_create_engine(self, edge) -> None:
        res = self.bob.post(ENGINES, json={"name": f"{self.name}_m", "description": ""})
        self.expect(res, edge)

    def edge_create_engine_name_taken(self, edge) -> None:
        res = self.admin.post(ENGINES, json={"name": self.name, "description": "again"})
        self.expect(res, edge)

    def edge_publish_policy(self, edge) -> None:
        base = f"{ENGINES}/{self.engine_id}/policies"
        res = self.admin.post(base, json={"name": f"{self.name}_forbid", "description": "",
                                          "statement": FORBID_PAYOUT})
        body = self.expect(res, edge)
        policy = body.get("policy", body)
        self.policy_id = policy["id"]
        self.ids.add(self.policy_id)
        assert (policy["status"], policy["enforcement_mode"]) == ("draft", "log_only"), policy
        bundles_before = [k for k in self.cas.objects if k.startswith("policy/gateway/")]
        res = self.admin.post(f"{base}/{self.policy_id}/publish", json={})
        self.expect(res, edge, status=edge.expect["publish_status"])
        res = self.admin.put(f"{base}/{self.policy_id}/enforcement-mode",
                             json={"enforcement_mode": "active"})
        assert res.status_code == 200, res.text
        # one set per ENGINE, keyed by the engine id, `<engine>-<n>`, on every shard
        versions = {shard.sets.get(self.engine_id) for shard in self.seams["shards"]}
        assert len(versions) == 1 and None not in versions, (
            [dict(s.sets) for s in self.seams["shards"]])
        self.set_version = versions.pop()
        prefix, _, n = self.set_version.rpartition("-")
        assert prefix == "ps" and n.isdigit(), self.set_version
        assert all(WS not in shard.sets for shard in self.seams["shards"]), "set keyed by ws"
        # attached nowhere ⇒ no bundle written
        assert [k for k in self.cas.objects if k.startswith("policy/gateway/")] == bundles_before

    def edge_attach_a(self, edge) -> None:
        res = self.admin.put(_attach_url(GW_A), json={"engine_id": self.engine_id,
                                                      "mode": "ENFORCE"})
        self.expect(res, edge)
        bundle = self.live_bundle(GW_A)
        assert bundle["schema"] == SCHEMA_V3 and "workspaces" not in bundle, bundle.keys()
        assert bundle["gateway_id"] == GW_A
        att = bundle["attachment"]
        assert (att["engine_id"], att["mode"]) == (self.engine_id, "ENFORCE"), att
        versions = {shard.sets[self.engine_id] for shard in self.seams["shards"]}
        assert versions == {att["policy_set_version"]}
        self.set_version = att["policy_set_version"]
        assert att["active_count"] >= 1 and att["governed_actions"], att
        assert bundle["empty_set_decision"] == "deny" and "exempt_system_presets" not in bundle
        assert all("exempt" in e for e in bundle["role_map"].values()), bundle["role_map"]
        bundle_mod.parse_bundle(bundle, expect_gateway_id=GW_A)  # what the Lambda reads
        assert self.lambda_attached(GW_A) is True
        assert self.control.bundle_seen_at_update[GW_A] == [True], (
            "the Lambda was attached before the bundle existed")

    def edge_attach_bad_mode(self, edge) -> None:
        before = self.live_bundle(GW_A)
        res = self.admin.put(_attach_url(GW_A), json={"engine_id": self.engine_id,
                                                      "mode": "enforce"})
        self.expect(res, edge)
        assert self.live_bundle(GW_A) == before

    def edge_attach_b(self, edge) -> None:
        res = self.admin.put(_attach_url(GW_B), json={"engine_id": self.engine_id,
                                                      "mode": "LOG_ONLY"})
        self.expect(res, edge)
        a, b = self.live_bundle(GW_A), self.live_bundle(GW_B)
        assert b["gateway_id"] == GW_B and b["attachment"]["mode"] == "LOG_ONLY", b["attachment"]
        assert b["attachment"]["policy_set_version"] == a["attachment"]["policy_set_version"]
        assert _strip_volatile(a) == _strip_volatile(b), "bundles differ beyond gateway + mode"
        assert self.lambda_attached(GW_B) is True
        view = self.engine_view()
        assert {x["gateway_id"] for x in view["attachments"]} == {GW_A, GW_B}, view
        assert {x["mode"] for x in view["attachments"]} == {"ENFORCE", "LOG_ONLY"}, view

    def edge_delete_engine_attached(self, edge) -> None:
        self.expect(self.admin.delete(f"{ENGINES}/{self.engine_id}"), edge)

    def edge_decide_a_enforce(self, edge) -> None:
        world = self.interceptor(GW_A)
        world.set_rule(lambda body: ("allow", "deny"))
        error = error_of(world.run(self.gateway_call(world, CREATE_PAYOUT)))
        assert error is not None and error["code"] == -32002, error
        assert error["message"] == contracts.deny_message(contracts.DEFAULT_DENY_REASON)
        line = world.decisions()[-1]
        assert (line["decision"], line["mode"]) == ("deny", "ENFORCE"), line
        assert (line["gateway_id"], line["engine_id"]) == (GW_A, self.engine_id), line
        assert line["decision_id"] == error["data"]["decision_id"]
        # the engine was asked on the ENGINE's set
        decide = [p for path, p in world.requests if path == "/v1/decide"]
        assert decide and decide[-1]["ws"] == self.engine_id, decide[-1:]
        self.capture_lines(world)  # in memory only — the hub ledger is untouched

    def edge_decide_b_log_only(self, edge) -> None:
        world = self.interceptor(GW_B)
        world.set_rule(lambda body: ("allow", "deny"))
        out = world.run(self.gateway_call(world, CREATE_PAYOUT))
        assert "transformedGatewayRequest" in out["mcp"], error_of(out)
        body = forwarded(out)  # asserts the token never reaches a target
        assert body["params"]["name"] == CREATE_PAYOUT
        line = world.decisions()[-1]
        assert (line["decision"], line["mode"]) == ("would_deny", "LOG_ONLY"), line
        assert (line["gateway_id"], line["engine_id"]) == (GW_B, self.engine_id), line
        self.capture_lines(world)

    def edge_ingest(self, edge) -> None:
        assert self.events, "the interceptors decided nothing yet"
        db = SessionLocal()
        try:
            added = policy_audit.ingest_gateway_events(db, self.events)
            assert added == len(self.events), (added, len(self.events))
            for event in self.events:
                line = json.loads(event["message"])["launchpad.policy.decision"]
                row = db.get(PolicyDecision, line["decision_id"])
                assert row is not None and row.source == "gateway", line["decision_id"]
                assert row.gateway_id == line["gateway_id"] and row.gateway_id in (GW_A, GW_B)
                assert row.engine_id == self.engine_id and row.ingested_at is not None
        finally:
            db.close()
        by_gateway: dict[str, set[str]] = {GW_A: set(), GW_B: set()}
        for event in self.events:
            line = json.loads(event["message"])["launchpad.policy.decision"]
            by_gateway[line["gateway_id"]].add(line["decision_id"])
        for gid, expected in by_gateway.items():
            res = self.admin.get(f"{ENGINES}/{self.engine_id}/decisions",
                                 params={"gateway_id": gid})
            body = self.expect(res, edge)
            listed = {d["id"] for d in body["decisions"]}
            assert expected <= listed, (gid, expected - listed)
            assert all(d["gateway_id"] == gid for d in body["decisions"]), gid

    def edge_detach_a(self, edge) -> None:
        b_before = self.live_bundle(GW_B)
        rows_before = self.extra_counts()["policy_decisions"]
        self.expect(self.admin.delete(_attach_url(GW_A)), edge)
        a = self.live_bundle(GW_A)
        assert a["schema"] == SCHEMA_V3 and a["attachment"] is None, a.get("attachment")
        bundle_mod.parse_bundle(a, expect_gateway_id=GW_A)
        assert self.lambda_attached(GW_A) is False
        assert self.live_bundle(GW_B) == b_before
        assert self.extra_counts()["policy_decisions"] == rows_before
        view = self.engine_view()
        assert [x["gateway_id"] for x in view["attachments"]] == [GW_B], view

    def edge_unguarded_call(self, edge) -> None:
        world = self.interceptor(GW_A, fresh=True)  # over the rewritten bundle
        world.set_rule(lambda body: ("allow", "deny"))
        out = world.run(self.gateway_call(world, CREATE_PAYOUT))
        assert "transformedGatewayRequest" in out["mcp"], error_of(out)
        forwarded(out)
        assert world.decisions() == [] and world.requests == [], (world.decisions(),
                                                                  world.requests)


@pytest.fixture
def make_world(admin, ada, bob, seams):
    _ada_id, ada_client = ada
    bob_id, bob_client = bob
    revoked = admin.patch(f"/api/users/{bob_id}",
                          json={"permissions": {"policy.manage": False}})
    assert revoked.status_code == 200, revoked.text
    return lambda: PolicyPrincipalWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, seams)


@pytest.fixture
def world(make_world) -> PolicyPrincipalWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(PolicyPrincipalWorld)


@PENDING
@pytest.mark.parametrize("path", params_for(PolicyPrincipalWorld))
def test_policy_principal_path(world, path):
    world.run(path)


@PENDING
@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(PolicyPrincipalWorld, include_covered_pairs=True))
def test_soak_policy_principal_every_edge_pair(world, path):
    world.run(path)


@PENDING
@pytest.mark.parametrize("seed", walk_seeds())
def test_policy_principal_walk(make_world, seed):
    """Seeded random walk through every edge of the graph — refusal loops included — on a
    fresh world; a failure is shrunk on fresh worlds (``tests/graph/walks.py``)."""
    run_walk(make_world, PolicyPrincipalWorld, GRAPHS["policy_principal"], seed)


@PENDING
@pytest.mark.parametrize("path", neighbourhood.params_for(PolicyPrincipalWorld, GRAPHS))
def test_policy_principal_neighbourhood(world, path):
    world.run(path)


@PENDING
@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(PolicyPrincipalWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_policy_principal_neighbourhood_pairs(world, path):
    world.run(path)
