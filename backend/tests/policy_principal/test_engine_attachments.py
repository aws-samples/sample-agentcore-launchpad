# ruff: noqa: F811 — pytest fixtures are imported explicitly from their modules
"""Contract amendment 11 (2026-10-28, pg-contracts): one policy ENGINE attached to one or many
gateways — the Global AgentCore Policy model.

Hermetic, against the hub's engine routes and services (pg-backend): the fake shards keyed by the
ENGINE id, the CAS fake S3 the bundle writer and the Lambda read, a two-gateway fake control
plane (``tests/graph/test_graph_policy_principal.TwoGatewayControl``), the interceptor Lambda
in-process over each gateway's REAL composed ``/3`` bundle. Every test is a STRICT xfail
"pg-backend pending" until ``app.services.policy_engines`` exists; then it must pass.

Pinned sentences (amendment 11): one set per ENGINE keyed by the engine id, `<engine>-<n>`;
one engine on two gateways ⇒ two bundle writes naming the same set, differing only in gateway +
mode; `ENFORCE` refuses a deny, `LOG_ONLY` records `would_deny` and passes, rows carry their own
`gateway_id`; detach ⇒ `attachment: null` + the Lambda detached ⇒ calls pass without a row; the
bundle is written BEFORE the Lambda is attached; the migration is idempotent ("no second engine,
no second attachment, no second journal row, and the bundle is not rewritten").
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.policy import PolicyChange, PolicyDecision
from app.policy import contracts
from app.policy.interceptor import bundle as bundle_mod
from app.policy.interceptor.bundle import pointer_key
from app.services import policy_audit, policy_gateway_state
from tests.graph.test_graph_policy_principal import (  # noqa: F401
    ENGINES,
    FORBID_PAYOUT,
    GW_A,
    GW_A_ARN,
    GW_B,
    GW_B_ARN,
    LANDED,
    PENDING,
    SCHEMA_V3,
    GatewayInterceptor,
    _attach_url,
    _strip_volatile,
    seams,
)
from tests.policy_principal.test_engine_sync import compiler, engines  # noqa: F401
from tests.policy_principal.test_gateway_publish import cas, gateway  # noqa: F401
from tests.policy_principal.test_interceptor import _claims, call_event, error_of, forwarded
from tests.policy_principal.test_mint_entrances import kms  # noqa: F401
from tests.test_policy_deploy_narrowing import _agent, engine, s3  # noqa: F401
from tests.test_visibility import admin, app  # noqa: F401 — fixtures by name

pytestmark = PENDING

WS = DEFAULT_WORKSPACE_ID
WS2 = "second-workspace"
PAYOUT = "hr-database___create_payout"
PERMIT_ALL = 'permit(principal, action == Launchpad::Action::"call_tool", resource);'


# ── helpers over the routes (the console's own surface) ─────────────────────────────────


def _engine(admin, name: str | None = None) -> str:
    res = admin.post(ENGINES, json={"name": name or f"t_{uuid.uuid4().hex[:8]}",
                                    "description": "attachments test"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _policy(admin, eid: str, statement: str, *, active: bool = True) -> str:
    base = f"{ENGINES}/{eid}/policies"
    res = admin.post(base, json={"name": f"p_{uuid.uuid4().hex[:8]}", "description": "",
                                 "statement": statement,
                                 "enforcement_mode": "active" if active else "log_only"})
    assert res.status_code == 201, res.text
    pid = res.json().get("policy", res.json())["id"]
    res = admin.post(f"{base}/{pid}/publish", json={})
    assert res.status_code == 200, res.text
    if active:
        res = admin.put(f"{base}/{pid}/enforcement-mode", json={"enforcement_mode": "active"})
        assert res.status_code == 200, res.text
    return pid


def _attach(admin, gid: str, eid: str, mode: str) -> None:
    res = admin.put(_attach_url(gid), json={"engine_id": eid, "mode": mode})
    assert res.status_code == 200, res.text


def _detach(admin, gid: str) -> None:
    res = admin.delete(_attach_url(gid))
    assert res.status_code == 200, res.text


def _live(cas, gid: str) -> dict[str, Any]:
    pointer = json.loads(cas.objects[pointer_key(gid)])
    return json.loads(cas.objects[pointer["key"]])


def _set_of(seams, eid: str) -> str:
    versions = {shard.sets.get(eid) for shard in seams["shards"]}
    assert len(versions) == 1 and None not in versions, [dict(s.sets) for s in seams["shards"]]
    return versions.pop()


def _lambda_attached(gid: str) -> bool:
    db = SessionLocal()
    try:
        return policy_gateway_state.gateway_interceptor_attached(db, gid)
    finally:
        db.close()


def _caller(agent_id: str) -> tuple[str, str]:
    """A ledger agent with Gateway tools (in the bundle's role_map) and its calling-role ARN."""
    from app.models.ledger import Agent
    from app.services import agent_iam

    _agent(agent_id=agent_id, name=f"att-{agent_id[-6:]}")
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        return agent_id, (f"arn:aws-cn:iam::111122223333:role/"
                          f"{agent_iam.role_name_for(row.name, row.id)}")
    finally:
        db.close()


def _call(seams, world: GatewayInterceptor, agent_id: str, arn: str, tool: str = PAYOUT) -> dict:
    attachment = world.bundle.get("attachment") or {}
    claims = _claims("user_member_chat")
    claims.update(ws=WS, agt=agent_id, aud=[agent_id],
                  tsv=int(attachment.get("temporal_set_version") or 0))
    event = call_event(seams["signer"].token(claims), tool=tool, arn=arn)
    event["mcp"]["gatewayRequest"]["body"]["id"] = f"rpc-{uuid.uuid4().hex[:12]}"
    return event


# ── one engine, two gateways ────────────────────────────────────────────────────────────


def test_one_engine_on_two_gateways_writes_two_bundles_naming_one_set(admin, seams):
    eid = _engine(admin)
    _policy(admin, eid, FORBID_PAYOUT)
    set_v1 = _set_of(seams, eid)
    prefix, _, n = set_v1.rpartition("-")
    assert prefix == "ps" and n.isdigit(), set_v1
    # the shards were installed under the ENGINE id (I5 `install.ws` carries the engine id)
    installs = [m for _shard, m in seams["seen"] if m.get("op") == "install"]
    assert installs and all(m["ws"] == eid for m in installs), installs[-1:]
    assert not [k for k in seams["cas"].objects if k.startswith("policy/gateway/")], (
        "attached nowhere ⇒ no bundle")

    _attach(admin, GW_A, eid, "ENFORCE")
    _attach(admin, GW_B, eid, "LOG_ONLY")
    set_v1 = _set_of(seams, eid)  # membership recompiles the union of attached gateway schemas
    a, b = _live(seams["cas"], GW_A), _live(seams["cas"], GW_B)
    for gid, bundle in ((GW_A, a), (GW_B, b)):
        assert bundle["schema"] == SCHEMA_V3 and bundle["gateway_id"] == gid
        assert "workspaces" not in bundle and "exempt_system_presets" not in bundle
        assert bundle["empty_set_decision"] == "deny"
        assert bundle["attachment"]["engine_id"] == eid
        assert bundle["attachment"]["policy_set_version"] == set_v1
        bundle_mod.parse_bundle(bundle, expect_gateway_id=gid)
    assert (a["attachment"]["mode"], b["attachment"]["mode"]) == ("ENFORCE", "LOG_ONLY")
    assert _strip_volatile(a) == _strip_volatile(b)

    # a second publish re-installs the ENGINE's set once and rewrites BOTH bundles
    _policy(admin, eid, PERMIT_ALL)
    set_v2 = _set_of(seams, eid)
    assert set_v2 != set_v1 and set_v2.rpartition("-")[0] == "ps"
    a2, b2 = _live(seams["cas"], GW_A), _live(seams["cas"], GW_B)
    assert a2["version"] == a["version"] + 1 and b2["version"] == b["version"] + 1
    assert a2["attachment"]["policy_set_version"] == set_v2
    assert b2["attachment"]["policy_set_version"] == set_v2
    assert (a2["attachment"]["mode"], b2["attachment"]["mode"]) == ("ENFORCE", "LOG_ONLY")


def test_the_bundle_is_written_before_the_lambda_is_attached(admin, seams):
    eid = _engine(admin)
    _policy(admin, eid, FORBID_PAYOUT)
    _attach(admin, GW_A, eid, "ENFORCE")
    assert _lambda_attached(GW_A) is True
    assert seams["control"].bundle_seen_at_update[GW_A] == [True]


def test_a_gateway_carries_one_attachment_and_changing_it_rewrites_the_bundle(admin, seams):
    e1, e2 = _engine(admin), _engine(admin)
    _policy(admin, e1, FORBID_PAYOUT)
    _policy(admin, e2, PERMIT_ALL)
    _attach(admin, GW_A, e1, "ENFORCE")
    v1 = _live(seams["cas"], GW_A)
    _attach(admin, GW_A, e1, "ENFORCE")  # idempotent: same engine, same mode
    assert _live(seams["cas"], GW_A)["version"] == v1["version"]
    _attach(admin, GW_A, e1, "LOG_ONLY")  # mode change rewrites
    v2 = _live(seams["cas"], GW_A)
    assert v2["version"] == v1["version"] + 1 and v2["attachment"]["mode"] == "LOG_ONLY"
    _attach(admin, GW_A, e2, "ENFORCE")  # engine change rewrites: ONE attachment per gateway
    v3 = _live(seams["cas"], GW_A)
    assert v3["attachment"]["engine_id"] == e2
    assert v3["attachment"]["policy_set_version"] == _set_of(seams, e2)
    res = admin.get(f"{ENGINES}/{e1}")
    assert res.status_code == 200 and res.json()["attachments"] == [], res.text
    res = admin.put(_attach_url(GW_A), json={"engine_id": e2, "mode": "enforce"})
    assert (res.status_code, res.json()["code"]) == (422, "gateway.policy_engine_mode_invalid")
    assert _live(seams["cas"], GW_A) == v3


# ── the modes decide per gateway ────────────────────────────────────────────────────────


def test_enforce_refuses_what_log_only_passes_and_rows_name_their_gateway(admin, seams):
    eid = _engine(admin)
    _policy(admin, eid, FORBID_PAYOUT)
    agent_id, arn = _caller(f"ag_att_{uuid.uuid4().hex[:6]}")
    _attach(admin, GW_A, eid, "ENFORCE")
    _attach(admin, GW_B, eid, "LOG_ONLY")
    a = GatewayInterceptor(seams, GW_A, _live(seams["cas"], GW_A))
    b = GatewayInterceptor(seams, GW_B, _live(seams["cas"], GW_B))
    for world in (a, b):
        world.set_rule(lambda body: ("allow", "deny"))

    error = error_of(a.run(_call(seams, a, agent_id, arn)))
    assert error is not None and error["code"] == -32002, error
    assert error["message"] == contracts.deny_message(contracts.DEFAULT_DENY_REASON)
    line_a = a.decisions()[-1]
    assert (line_a["decision"], line_a["mode"], line_a["gateway_id"], line_a["engine_id"]) == (
        "deny", "ENFORCE", GW_A, eid), line_a
    decide = [p for path, p in a.requests if path == "/v1/decide"]
    assert decide and decide[-1]["ws"] == eid, "the engine must be asked on the ENGINE's set"

    out = b.run(_call(seams, b, agent_id, arn))
    assert "transformedGatewayRequest" in out["mcp"], error_of(out)
    assert forwarded(out)["params"]["name"] == PAYOUT
    line_b = b.decisions()[-1]
    assert (line_b["decision"], line_b["mode"], line_b["gateway_id"], line_b["engine_id"]) == (
        "would_deny", "LOG_ONLY", GW_B, eid), line_b

    # the hub ingests both lines with their own gateway and lists them per gateway
    events = [{"timestamp": 0, "message": json.dumps(line)} for line in a.audit + b.audit
              if "launchpad.policy.decision" in line]
    db = SessionLocal()
    try:
        assert policy_audit.ingest_gateway_events(db, events) == 2
        row_a = db.get(PolicyDecision, line_a["decision_id"])
        row_b = db.get(PolicyDecision, line_b["decision_id"])
        assert (row_a.gateway_id, row_b.gateway_id) == (GW_A, GW_B)
        assert row_a.engine_id == row_b.engine_id == eid
        assert (row_a.mode, row_b.mode) == ("ENFORCE", "LOG_ONLY")
    finally:
        db.close()
    for gid, decision_id in ((GW_A, line_a["decision_id"]), (GW_B, line_b["decision_id"])):
        res = admin.get(f"{ENGINES}/{eid}/decisions", params={"gateway_id": gid})
        assert res.status_code == 200, res.text
        ids = {d["id"] for d in res.json()["decisions"]}
        assert decision_id in ids and all(d["gateway_id"] == gid for d in res.json()["decisions"])


# ── detach ⇒ unguarded ─────────────────────────────────────────────────────────────────


def test_detach_rewrites_a_null_attachment_detaches_the_lambda_and_passes_calls(admin, seams):
    eid = _engine(admin)
    _policy(admin, eid, FORBID_PAYOUT)
    agent_id, arn = _caller(f"ag_att_{uuid.uuid4().hex[:6]}")
    _attach(admin, GW_A, eid, "ENFORCE")
    _attach(admin, GW_B, eid, "LOG_ONLY")
    b_before = _live(seams["cas"], GW_B)
    res = admin.delete(f"{ENGINES}/{eid}")
    assert (res.status_code, res.json()["code"]) == (409, "policy_engine.attached"), res.text

    _detach(admin, GW_A)
    a = _live(seams["cas"], GW_A)
    assert a["schema"] == SCHEMA_V3 and a["attachment"] is None, a.get("attachment")
    bundle_mod.parse_bundle(a, expect_gateway_id=GW_A)
    assert _lambda_attached(GW_A) is False and _lambda_attached(GW_B) is True
    assert _live(seams["cas"], GW_B) == b_before, "the other gateway's bundle is untouched"
    world = GatewayInterceptor(seams, GW_A, a)
    world.set_rule(lambda body: ("allow", "deny"))
    out = world.run(_call(seams, world, agent_id, arn))
    assert "transformedGatewayRequest" in out["mcp"], error_of(out)
    forwarded(out)
    assert world.decisions() == [] and world.requests == [], "unguarded: no row, no engine call"

    # still attached to B ⇒ the engine still cannot be deleted; after B's detach it can
    res = admin.delete(f"{ENGINES}/{eid}")
    assert res.status_code == 409, res.text
    _detach(admin, GW_B)
    assert _live(seams["cas"], GW_B)["attachment"] is None
    res = admin.delete(f"{ENGINES}/{eid}", params={"force": "true"})
    assert res.status_code == 200, res.text
    db = SessionLocal()
    try:
        ops = [r.operation for r in db.query(PolicyChange).filter_by(workspace_id=WS).all()]
        assert ops.count("gateway.attach") >= 2 and ops.count("gateway.detach") == 2, ops
    finally:
        db.close()


# ── migration ─────────────────────────────────────────────────────────────────────────


def _migrate() -> None:
    from app.services import policy_engines

    for name in ("migrate_legacy", "migrate", "migrate_workspaces", "ensure_migrated"):
        fn = getattr(policy_engines, name, None)
        if callable(fn):
            db = SessionLocal()
            try:
                fn(db)
                db.commit()
                from app.services import policy_gateway_publish

                policy_gateway_publish.reconcile(db)
            finally:
                db.close()
            return
    pytest.fail("app.services.policy_engines has no migration entry point "
                "(expected migrate_legacy(db) — amendment 11 §11.6)")


def _add_column_if_missing(db, table: str, column: str, ddl: str) -> None:
    cols = {r[1] for r in db.execute(text(f"PRAGMA table_info({table})")).fetchall()}
    if column not in cols:
        db.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


def _seed_old_shape(workspace_id: str, gateway_id: str, mode: str, *, attached: bool) -> str:
    """Seed the fields an upgraded ledger carries, using ORM defaults for unrelated columns."""
    from app.models.ledger import Workspace
    from app.models.policy import Policy, PolicySettings, PolicyVersion
    from tests.graph.test_graph_policy_principal import LAMBDA
    from tests.policy_principal.test_gateway_publish import RES

    db = SessionLocal()
    try:
        ws = db.get(Workspace, workspace_id)
        resources = {**RES, "gateway_id": gateway_id,
                     "gateway_arn": GW_A_ARN if gateway_id == GW_A else GW_B_ARN}
        if ws is None:
            db.add(Workspace(id=workspace_id, name=workspace_id, account_id="111122223333",
                             region="cn-northwest-1", resources=resources))
        else:
            ws.resources = resources
        db.query(PolicySettings).filter_by(workspace_id=workspace_id).delete()
        db.add(PolicySettings(workspace_id=workspace_id, mode=mode,
                              gateway_interceptor=True, engine_publish=True))
        pid = f"pol_{uuid.uuid4().hex[:12]}"
        db.add(Policy(id=pid, workspace_id=workspace_id, name=f"legacy_{pid[-6:]}",
                      status="active", enforcement_mode="active", active_version=1))
        db.add(PolicyVersion(workspace_id=workspace_id, policy_id=pid, version=1,
                             statement=FORBID_PAYOUT, schema_digest="d"))
        db.commit()
        if attached:
            policy_gateway_state.record_attach(db, gateway_id, LAMBDA)
        else:
            policy_gateway_state.record_detach(db, gateway_id)
        return pid
    finally:
        db.close()


def _snapshot(workspace_id: str) -> dict[str, Any]:
    from app.models.policy import Policy, PolicyEngine, PolicyEngineAttachment

    db = SessionLocal()
    try:
        engines_ = db.query(PolicyEngine).filter_by(workspace_id=workspace_id).all()
        return {
            "engines": sorted((e.name, e.id) for e in engines_),
            "policy_engine_ids": sorted(p.engine_id or "" for p in
                                        db.query(Policy).filter_by(workspace_id=workspace_id)),
            "attachments": sorted((a.gateway_id, a.engine_id, a.mode)
                                  for a in db.query(PolicyEngineAttachment).all()),
            "migrated_rows": db.query(PolicyChange).filter_by(
                workspace_id=workspace_id, operation="policy_engine.migrated").count(),
        }
    finally:
        db.close()


def test_migration_creates_the_default_engine_and_attachment_once(admin, seams):
    pid = _seed_old_shape(WS, GW_A, "enforce", attached=True)
    _migrate()
    first = _snapshot(WS)
    assert [name for name, _id in first["engines"]] == ["default_policy_engine"], first
    eid = first["engines"][0][1]
    assert first["policy_engine_ids"] and set(first["policy_engine_ids"]) == {eid}, first
    assert first["attachments"] == [(GW_A, eid, "ENFORCE")], first
    assert first["migrated_rows"] == 1
    bundle_v1 = _live(seams["cas"], GW_A)
    assert bundle_v1["schema"] == SCHEMA_V3
    assert bundle_v1["attachment"]["engine_id"] == eid
    assert bundle_v1["attachment"]["mode"] == "ENFORCE"
    assert bundle_v1["attachment"]["policy_set_version"] == _set_of(seams, eid)
    res = admin.get(f"{ENGINES}/{eid}/policies")
    assert res.status_code == 200 and pid in {p["id"] for p in res.json()["policies"]}, res.text

    # idempotence: "no second engine, no second attachment, no second journal row, and the
    # bundle is not rewritten"
    _migrate()
    assert _snapshot(WS) == first
    assert _live(seams["cas"], GW_A)["version"] == bundle_v1["version"]


def test_migration_off_mode_means_no_attachment_and_a_detached_lambda(admin, seams):
    _seed_old_shape(WS, GW_B, "off", attached=True)
    _migrate()
    snap = _snapshot(WS)
    assert [name for name, _id in snap["engines"]] == ["default_policy_engine"], snap
    assert not [a for a in snap["attachments"] if a[0] == GW_B], snap
    assert _lambda_attached(GW_B) is False
    assert snap["migrated_rows"] == 1
    _migrate()
    assert _snapshot(WS) == snap


def test_migration_retry_preserves_an_attachment_created_after_off_migration(admin, seams):
    from app.services import policy_gateway_publish

    _seed_old_shape(WS, GW_A, "off", attached=True)
    _migrate()
    eid = _snapshot(WS)["engines"][0][1]
    _attach(admin, GW_A, eid, "ENFORCE")
    before = _live(seams["cas"], GW_A)
    with SessionLocal() as db:
        policy_gateway_publish.reconcile(db)
    assert _snapshot(WS)["attachments"] == [(GW_A, eid, "ENFORCE")]
    assert _lambda_attached(GW_A)
    assert _live(seams["cas"], GW_A) == before


def test_a_workspace_without_policies_gets_no_engine(seams):
    _migrate()
    db = SessionLocal()
    try:
        from app.models.policy import PolicyEngine

        assert db.query(PolicyEngine).filter_by(workspace_id=WS).count() == 0
        assert db.query(PolicyChange).filter_by(
            workspace_id=WS, operation="policy_engine.migrated").count() == 0
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["enforce", "log_only", "off"])
def test_failed_migration_preserves_legacy_bundle_and_next_reconcile_retries(
        admin, seams, monkeypatch, mode):
    from app.core.errors import AppError
    from app.services import policy_engines, policy_gateway_publish, policy_scope_migration
    from tests.policy_principal.conftest import load_fixture

    _seed_old_shape(WS, GW_A, mode, attached=True)
    legacy = load_fixture("gateway_bundle.json")
    block = legacy.pop("attachment")
    block.pop("engine_id")
    block.update(mode=mode, published=True, gateway_interceptor=True)
    legacy.update(schema="launchpad.policy.gateway/2", gateway_id=GW_A,
                  gateway_arn=GW_A_ARN, workspaces={WS: block})
    key = bundle_mod.bundle_key(GW_A, legacy["version"])
    seams["cas"].objects[key] = json.dumps(legacy).encode()
    seams["cas"].objects[pointer_key(GW_A)] = json.dumps(
        bundle_mod.pointer(GW_A, legacy["version"], legacy["issued_at"])).encode()
    before = dict(seams["cas"].objects)
    original = policy_scope_migration.bake_scope

    def fail(*_args):
        raise AppError("policy.s3_copy_failed", "injected migration failure", status_code=502)

    monkeypatch.setattr(policy_scope_migration, "bake_scope", fail)
    assert policy_engines.migrate_once() == []
    with SessionLocal() as db:
        policy_gateway_publish.reconcile(db)
    assert seams["cas"].objects == before, "failed migration must retain the old enforcement"
    monkeypatch.setattr(policy_scope_migration, "bake_scope", original)
    with SessionLocal() as db:
        policy_gateway_publish.reconcile(db)
    current = _live(seams["cas"], GW_A)
    assert current["schema"] == SCHEMA_V3
    if mode == "off":
        assert current["attachment"] is None
    else:
        assert current["attachment"]["mode"] == ("ENFORCE" if mode == "enforce" else "LOG_ONLY")
    after = dict(seams["cas"].objects)
    with SessionLocal() as db:
        policy_gateway_publish.reconcile(db)
    assert seams["cas"].objects == after


def test_reconcile_recovers_failed_detach_of_an_additional_gateway(admin, seams):
    from app.services import policy_gateway_publish

    eid = _engine(admin)
    _policy(admin, eid, PERMIT_ALL)
    _attach(admin, GW_B, eid, "ENFORCE")
    before = seams["cas"].objects[pointer_key(GW_B)]
    seams["cas"].fail_prefix = f"policy/gateway/{GW_B}/"
    response = admin.delete(_attach_url(GW_B))
    assert response.status_code == 409
    assert seams["cas"].objects[pointer_key(GW_B)] == before
    seams["cas"].fail_prefix = None
    with SessionLocal() as db:
        policy_gateway_publish.reconcile(db)
    assert _live(seams["cas"], GW_B)["attachment"] is None


