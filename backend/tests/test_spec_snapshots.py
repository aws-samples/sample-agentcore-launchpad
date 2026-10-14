"""Ledger spec snapshots: one per publish, the as-deployed stamp, diff, rollback and its
refusals, the startup baseline, and the console-v3 table migration.

Ported from the console-v3 branch (`tests/test_spec_snapshots.py` there) without the
prod-tier lock, plus the checks main adds (never-deployed / stale-KB refusals, the
as-deployed spec refresh, the creator convention, visibility).
"""

import pytest
import sqlalchemy as sa

import app.routers.agents as agents_router
from app.core import db as db_module
from app.core.db import (
    DEFAULT_WORKSPACE_ID,
    Base,
    SessionLocal,
    assert_no_schema_drift,
)
from app.deployer.pipeline import (
    STAGE_ORDER,
    StageResult,
    _finish,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Agent, Deployment, Job, SpecSnapshot, Workspace
from app.services import snapshots as snap_service
from app.services.creators import Creator
from tests import test_visibility as tv
from tests.test_visibility import _isolate

SPEC = {"name": "snap-agent", "method": "harness", "system_prompt": "Be brief."}

# the auth-enabled console + members of the workspace visibility suite
app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    return launched


def _deployed(agent_id: str, version: str) -> None:
    """Finish the agent's latest deployment successfully at `version` (the real
    `_finish`, so the snapshot is stamped the way a live deploy stamps it)."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.resource_id = "harness-xyz"
        agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/xyz"
        agent.version = version
        db.commit()
        dep = (
            db.query(Deployment)
            .filter(Deployment.agent_id == agent_id)
            .order_by(Deployment.started_at.desc())
            .first()
        )
        _finish(db, dep.job_id, dep.id, agent_id, None)
    finally:
        db.close()


def _create(client, spec=SPEC, version: str = "1") -> str:
    res = client.post("/api/agents", json=spec)
    assert res.status_code == 202, res.text
    agent_id = res.json()["agent"]["id"]
    _deployed(agent_id, version)
    return agent_id


def _redeploy(client, agent_id, spec, version: str | None = "2"):
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=spec)
    assert res.status_code == 202, res.text
    if version is not None:
        _deployed(agent_id, version)
    return res


def _snap_rows(agent_id: str) -> list[SpecSnapshot]:
    db = SessionLocal()
    try:
        return (
            db.query(SpecSnapshot)
            .filter(SpecSnapshot.agent_id == agent_id)
            .order_by(SpecSnapshot.seq)
            .all()
        )
    finally:
        db.close()


def _edit_snapshot(agent_id: str, seq: int, **fields) -> None:
    db = SessionLocal()
    try:
        snap = snap_service.get_snapshot(db, agent_id, seq)
        for key, value in fields.items():
            setattr(snap, key, value)
        db.commit()
    finally:
        db.close()


# ─── recording ────────────────────────────────────────────────────────────────


def test_snapshot_written_on_create_and_every_redeploy(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."}, version="2")
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose.", "max_iterations": 20},
              version="3")
    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert [r["seq"] for r in rows] == [3, 2, 1]
    assert [r["aws_version"] for r in rows] == ["3", "2", "1"]
    assert all(r["deployed"] and r["state"] == "deployed" for r in rows)
    assert "spec" not in rows[0]
    assert all(r["deployment_id"] for r in rows)
    # auth disabled: the local operator, `(None, name)` — the creators convention
    assert {(r["created_by"], r["created_by_name"]) for r in rows} == {(None, "admin")}
    first = client.get(f"/api/agents/{agent_id}/snapshots/1").json()
    assert first["spec"]["system_prompt"] == "Be brief."
    missing = client.get(f"/api/agents/{agent_id}/snapshots/9")
    assert missing.status_code == 404 and missing.json()["code"] == "snapshot.not_found"
    unknown = client.get("/api/agents/nope/snapshots")
    assert unknown.status_code == 404 and unknown.json()["code"] == "agent.not_found"


def test_the_list_never_loads_the_spec_column(client):
    agent_id = _create(client)
    db = SessionLocal()
    try:
        (snap,) = snap_service.list_snapshots(db, agent_id)
        assert "spec" in sa.inspect(snap).unloaded
        assert "spec" not in snap_service.snapshot_out(snap)
        assert "spec" in sa.inspect(snap).unloaded  # serializing did not load it either
    finally:
        db.close()


def test_the_job_payload_carries_the_actor_and_note(client):
    agent_id = _create(client)
    res = _redeploy(client, agent_id, {**SPEC, "system_prompt": "x"}, version=None)
    db = SessionLocal()
    try:
        payload = db.get(Job, res.json()["job_id"]).payload
    finally:
        db.close()
    assert payload["actor_name"] == "admin" and payload["note"] is None
    assert payload["mode"] == "update"


def test_a_snapshot_shares_a_commit_false_callers_transaction():
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="tx-agent", method="harness",
                      status="deploying", spec={"name": "tx-agent"})
        db.add(agent)
        db.flush()
        create_deployment(db, agent, commit=False)
        db.rollback()  # the caller's transaction fails: nothing of it survives
    finally:
        db.close()
    db = SessionLocal()
    try:
        assert db.query(SpecSnapshot).count() == 0
        assert db.query(Agent).count() == 0
    finally:
        db.close()


def _pipeline_agent(method: str, spec: dict) -> tuple[str, str]:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=f"{method}-agent", method=method,
                      status="deploying", spec=spec, created_by="u-1", created_by_name="ada")
        db.add(agent)
        db.commit()
        _, job = create_deployment(db, agent)
        return agent.id, job.id
    finally:
        db.close()


def _rewriting_deploy(ctx, agent):
    """A deploy stage that rewrites the spec mid-job (as the architect's harness stage
    pins skills to immutable copies) and lands a version, both in its own session."""
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)
        row.spec = {**row.spec, "skills": ["s3://bucket/system-skills/copy-1/"]}
        row.version = "7"
        db.commit()
    finally:
        db.close()
    return StageResult(detail="deployed")


def test_success_stamps_the_version_and_the_spec_as_deployed():
    register_method("fake_snap_ok", {s: (lambda ctx, a: StageResult(detail="ok"))
                                     for s in STAGE_ORDER} | {"deploy": _rewriting_deploy})
    agent_id, job_id = _pipeline_agent("fake_snap_ok", {"name": "x", "skills": ["skills/a/"]})
    (before,) = _snap_rows(agent_id)
    assert before.spec["skills"] == ["skills/a/"] and before.aws_version is None
    # creator falls back to the agent's when the path names no actor
    assert (before.created_by, before.created_by_name) == ("u-1", "ada")

    execute_deploy_job(job_id)

    (after,) = _snap_rows(agent_id)
    assert after.aws_version == "7"
    assert after.spec["skills"] == ["s3://bucket/system-skills/copy-1/"]


def test_a_failed_deploy_keeps_its_snapshot_unstamped():
    def boom(ctx, agent):
        raise RuntimeError("bad role arn")

    register_method("fake_snap_fail", {"deploy": boom})
    agent_id, job_id = _pipeline_agent("fake_snap_fail", {"name": "x", "system_prompt": "p"})
    execute_deploy_job(job_id)
    (snap,) = _snap_rows(agent_id)
    assert snap.aws_version is None and snap.spec["system_prompt"] == "p"
    db = SessionLocal()
    try:
        status = db.get(Deployment, snap.deployment_id).status
        out = snap_service.snapshot_out(snap, deployment_status=status)
    finally:
        db.close()
    assert out["deployed"] is False and out["state"] == "failed"


def test_a_resumed_job_records_no_second_snapshot():
    register_method("fake_snap_resume", {s: (lambda ctx, a: StageResult(detail="ok"))
                                         for s in STAGE_ORDER} | {"deploy": _rewriting_deploy})
    agent_id, job_id = _pipeline_agent("fake_snap_resume", {"name": "x"})
    # a process died mid-job: generate succeeded, the job is still `running`
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = "running"
        dep = db.get(Deployment, job.payload["deployment_id"])
        dep.stages = [{**s, "status": "succeeded"} if s["name"] == "generate" else s
                      for s in dep.stages]
        db.commit()
    finally:
        db.close()

    execute_deploy_job(job_id)  # the startup resume path

    rows = _snap_rows(agent_id)
    assert len(rows) == 1 and rows[0].aws_version == "7"


# ─── diff ─────────────────────────────────────────────────────────────────────


def test_diff_is_field_level_and_grouped(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {
        **SPEC,
        "system_prompt": "Be verbose.",
        "model_id": "global.other-model",
        "skills": ["s1"],
        "memory": {"short_term": False, "long_term": False},
    })
    diff = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 1, "to_seq": 2}
    ).json()
    by_path = {c["path"]: c for c in diff["changes"]}
    assert by_path["system_prompt"]["before"] == "Be brief."
    assert by_path["system_prompt"]["after"] == "Be verbose."
    assert by_path["system_prompt"]["group"] == "prompt"
    assert by_path["model_id"]["group"] == "model"
    assert by_path["skills"]["added"] == ["s1"] and by_path["skills"]["removed"] == []
    assert by_path["memory.short_term"]["kind"] == "changed"
    assert by_path["memory.short_term"]["group"] == "memory"
    assert "name" not in by_path  # unchanged fields are omitted
    assert [c["group"] for c in diff["changes"]][:2] == ["prompt", "model"]  # display order
    assert diff["from"]["seq"] == 1 and diff["to"]["seq"] == 2
    assert diff["to"]["state"] == "deployed" and "spec" not in diff["to"]
    same = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 2, "to_seq": 2}
    ).json()
    assert same["changes"] == []


def test_an_omitted_side_is_the_current_spec(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    to_current = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 1}
    ).json()
    assert to_current["to"] is None and to_current["from"]["seq"] == 1
    assert [(c["path"], c["before"], c["after"]) for c in to_current["changes"]] == [
        ("system_prompt", "Be brief.", "Be verbose.")
    ]
    # the rollback confirm: current → target
    from_current = client.get(
        f"/api/agents/{agent_id}/snapshots/diff", params={"to_seq": 1}
    ).json()
    assert from_current["from"] is None
    assert from_current["changes"][0]["before"] == "Be verbose."
    both = client.get(f"/api/agents/{agent_id}/snapshots/diff")
    assert both.status_code == 422 and both.json()["code"] == "snapshot.diff_side_required"
    missing = client.get(f"/api/agents/{agent_id}/snapshots/diff", params={"from_seq": 9})
    assert missing.status_code == 404 and missing.json()["code"] == "snapshot.not_found"


def test_a_large_atomic_body_is_replaced_by_its_length():
    long_prompt = "x" * (snap_service.BODY_LIMIT + 10)
    bundle = {"main.py": "print(1)\n" * 1000}
    changes = snap_service.diff_specs(
        {"system_prompt": "short", "code_bundle": bundle},
        {"system_prompt": long_prompt, "code_bundle": {**bundle, "extra.py": ""}},
    )
    by_path = {c["path"]: c for c in changes}
    assert set(by_path) == {"system_prompt", "code_bundle"}  # atomic: not recursed into
    prompt = by_path["system_prompt"]
    assert prompt["before"] == "short" and prompt["after"] == {"length": len(long_prompt)}
    assert prompt["elided"] is True
    code = by_path["code_bundle"]
    assert set(code["before"]) == {"length"} and set(code["after"]) == {"length"}
    small = snap_service.diff_specs({"system_prompt": "a"}, {"system_prompt": "b"})
    assert "elided" not in small[0]


def test_diff_specs_marks_added_and_removed_fields():
    changes = snap_service.diff_specs({"a": 1, "tools": [1, 2]}, {"b": 2, "tools": [2, 3]})
    kinds = {c["path"]: c["kind"] for c in changes}
    assert kinds == {"a": "removed", "b": "added", "tools": "changed"}
    tools = next(c for c in changes if c["path"] == "tools")
    assert tools["added"] == [3] and tools["removed"] == [1] and tools["group"] == "tools"
    assert snap_service.changed_groups(changes) == ["tools", "other"]


# ─── rollback ─────────────────────────────────────────────────────────────────


def test_rollback_redeploys_the_old_spec_as_a_new_snapshot(client, no_real_deploy):
    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 202, res.text
    body = res.json()
    assert set(body) == {"agent", "job_id", "deployment_id"}  # the redeploy shape
    assert body["agent"]["status"] == "deploying"
    assert body["agent"]["spec"]["system_prompt"] == "Be brief."
    assert no_real_deploy[-1] == body["job_id"]
    db = SessionLocal()
    try:
        payload = db.get(Job, body["job_id"]).payload
    finally:
        db.close()
    assert payload["mode"] == "update"  # never an AWS-side revert
    assert payload["note"] == "rollback to #1" and payload["actor_name"] == "admin"
    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert [r["seq"] for r in rows] == [3, 2, 1]
    assert rows[0]["note"] == "rollback to #1" and rows[0]["created_by_name"] == "admin"
    assert rows[0]["state"] == "deploying" and rows[0]["deployed"] is False
    assert client.get(f"/api/agents/{agent_id}/snapshots/3").json()["spec"]["system_prompt"] \
        == "Be brief."


def test_rollback_refusals(client):
    agent_id = _create(client)
    unknown = client.post(f"/api/agents/{agent_id}/snapshots/9/rollback")
    assert unknown.status_code == 404 and unknown.json()["code"] == "snapshot.not_found"
    assert client.post("/api/agents/nope/snapshots/1/rollback").status_code == 404
    # a publish that never deployed is no rollback target
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."}, version=None)
    db = SessionLocal()
    try:
        dep = db.query(Deployment).filter(Deployment.agent_id == agent_id) \
            .order_by(Deployment.started_at.desc()).first()
        _finish(db, dep.job_id, dep.id, agent_id, "DeployError: boom")
        db.get(Agent, agent_id).status = "active"
        db.commit()
    finally:
        db.close()
    never = client.post(f"/api/agents/{agent_id}/snapshots/2/rollback")
    assert never.status_code == 409 and never.json()["code"] == "snapshot.never_deployed"
    # in-flight deploy
    db = SessionLocal()
    db.get(Agent, agent_id).status = "deploying"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 409 and res.json()["code"] == "agent.deploy_in_progress"
    # system-managed preset
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.system_key = "architect"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 403 and res.json()["code"] == "agent.system_managed"
    assert res.json()["detail"]["action"] == "rollback"
    # discovered runtimes are external
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.system_key = None
    agent.method = "discovered_runtime"
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 400 and res.json()["code"] == "agent.redeploy_external"
    # nothing above started a deploy: still two snapshots
    assert len(_snap_rows(agent_id)) == 2


def test_rollback_refused_when_a_converted_agent_would_change_baked_fields(client):
    converted = {
        **SPEC, "name": "conv-agent", "method": "zip_runtime",
        "code_bundle": {"main.py": "print(1)\n"},
        "source_harness": {"agent_id": "src", "harness_arn": "arn:aws:x:1:1:harness/h"},
    }
    agent_id = _create(client, converted)
    # an older snapshot whose baked prompt differs from the live spec
    db = SessionLocal()
    old = snap_service.get_snapshot(db, agent_id, 1)
    old.spec = {**old.spec, "system_prompt": "An older baked prompt."}
    db.commit()
    db.close()
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 400 and res.json()["code"] == "agent.converted_locked"
    assert res.json()["detail"]["fields"] == ["system_prompt"]


def test_rollback_of_a_snapshot_that_no_longer_validates_is_409(client):
    agent_id = _create(client)
    _edit_snapshot(agent_id, 1, spec={"name": "x"})
    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 409 and res.json()["code"] == "snapshot.spec_invalid"
    assert res.json()["detail"]["errors"] >= 1


class _KbClient:
    """bedrock-agent stub: GetKnowledgeBase for the ids that still exist."""

    class exceptions:  # noqa: N801 — the boto3 attribute shape
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, existing: set[str]):
        self.existing = existing
        self.calls: list[str] = []

    def get_knowledge_base(self, knowledgeBaseId):
        self.calls.append(knowledgeBaseId)
        if knowledgeBaseId not in self.existing:
            raise self.exceptions.ResourceNotFoundException(knowledgeBaseId)
        return {"knowledgeBase": {"knowledgeBaseId": knowledgeBaseId}}


def test_rollback_refuses_a_knowledge_base_that_is_gone(client, monkeypatch):
    import app.services.agentcore.client as client_mod

    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    def kb(kb_id: str) -> dict:
        return {"kb_id": kb_id, "name": kb_id, "description": ""}

    _edit_snapshot(agent_id, 1, spec={**SPEC, "knowledge_bases": [kb("KBGONE"), kb("KBLIVE")]})
    stub = _KbClient({"KBLIVE"})
    monkeypatch.setattr(client_mod, "agent_client", lambda _ws: stub)

    res = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "snapshot.stale_reference"
    assert res.json()["detail"]["kb_ids"] == ["KBGONE"]
    assert sorted(stub.calls) == ["KBGONE", "KBLIVE"]  # only the re-added ids are read
    assert len(_snap_rows(agent_id)) == 2

    # once the KB exists again the rollback goes through
    stub.existing.add("KBGONE")
    assert client.post(f"/api/agents/{agent_id}/snapshots/1/rollback").status_code == 202


def test_a_rollback_that_re_adds_no_kb_reads_nothing(client, monkeypatch):
    import app.services.agentcore.client as client_mod

    def boom(_ws):
        raise AssertionError("no KB read expected")

    monkeypatch.setattr(client_mod, "agent_client", boom)
    agent_id = _create(client)
    _redeploy(client, agent_id, {**SPEC, "system_prompt": "Be verbose."})
    assert client.post(f"/api/agents/{agent_id}/snapshots/1/rollback").status_code == 202


def test_snapshots_do_not_cross_workspaces(client):
    agent_id = _create(client)
    db = SessionLocal()
    db.add(Workspace(id="other", name="Other", account_id="222233334444", region="us-east-1",
                     bootstrap_status="ready"))
    db.commit()
    db.close()
    for method, url in (
        ("GET", f"/api/agents/{agent_id}/snapshots"),
        ("GET", f"/api/agents/{agent_id}/snapshots/1"),
        ("GET", f"/api/agents/{agent_id}/snapshots/diff?from_seq=1"),
        ("POST", f"/api/agents/{agent_id}/snapshots/1/rollback"),
    ):
        res = client.request(method, url, headers={"X-Workspace": "other"})
        assert res.status_code == 404 and res.json()["code"] == "agent.not_found", (method, url)


# ─── members ──────────────────────────────────────────────────────────────────


def _member_agent(created_by: str, name: str = "bob-agent") -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness",
                      status="active", resource_id="h-1", arn="arn:h", version="1",
                      spec={**SPEC, "name": name}, created_by=created_by,
                      created_by_name="bob")
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()
    assert snap_service.backfill_baselines() == 1
    return agent_id


def test_a_hidden_agents_snapshots_are_404(app, admin, ada, bob):
    bob_id, bob_client = bob
    _, ada_client = ada
    agent_id = _member_agent(bob_id)
    urls = (
        ("GET", f"/api/agents/{agent_id}/snapshots"),
        ("GET", f"/api/agents/{agent_id}/snapshots/1"),
        ("GET", f"/api/agents/{agent_id}/snapshots/diff?from_seq=1"),
        ("POST", f"/api/agents/{agent_id}/snapshots/1/rollback"),
    )
    assert ada_client.get(urls[0][1]).status_code == 200  # shared: visible
    _isolate()
    for method, url in urls:
        res = ada_client.request(method, url)
        assert res.status_code == 404 and res.json()["code"] == "agent.not_found", (method, url)
    assert bob_client.get(urls[0][1]).status_code == 200  # the creator still sees it
    assert admin.get(urls[0][1]).status_code == 200


def test_rollback_needs_the_deploy_permission(app, admin, bob, no_real_deploy):
    bob_id, bob_client = bob
    agent_id = _member_agent(bob_id)
    admin.patch(f"/api/users/{bob_id}", json={"permissions": {"agents.deploy": False}})
    denied = bob_client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert denied.status_code == 403 and denied.json()["code"] == "auth.permission_required"
    # reads stay open
    assert bob_client.get(f"/api/agents/{agent_id}/snapshots").status_code == 200
    admin.patch(f"/api/users/{bob_id}", json={"permissions": {"agents.deploy": True}})
    res = bob_client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert res.status_code == 202, res.text
    rows = bob_client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert (rows[0]["created_by"], rows[0]["created_by_name"]) == (bob_id, "bob")


# ─── baseline backfill ────────────────────────────────────────────────────────


def _row(**fields) -> str:
    db = SessionLocal()
    try:
        fields.setdefault("workspace_id", DEFAULT_WORKSPACE_ID)
        fields.setdefault("method", "harness")
        fields.setdefault("status", "active")
        fields.setdefault("resource_id", "rid")
        fields.setdefault("spec", {"name": fields["name"], "system_prompt": "live"})
        agent = Agent(**fields)
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def test_the_baseline_backfill_runs_once_and_skips_what_it_should():
    live = _row(name="live", version="4", created_by="u-ada", created_by_name="ada")
    legacy = _row(name="legacy", version=None)
    skipped = [
        _row(name="found", method="discovered_runtime"),
        _row(name="broken", status="failed"),
        _row(name="busy", status="deploying"),
        _row(name="gone", status="deleted"),
        _row(name="no-resource", resource_id=None),
    ]
    already = _row(name="already", version="2")
    db = SessionLocal()
    try:
        snap_service.record_snapshot(db, db.get(Agent, already), note="earlier")
        db.commit()
    finally:
        db.close()

    assert snap_service.backfill_baselines() == 2
    assert snap_service.backfill_baselines() == 0  # idempotent

    (snap,) = _snap_rows(live)
    assert (snap.seq, snap.note, snap.aws_version) == (1, "baseline", "4")
    assert snap.spec == {"name": "live", "system_prompt": "live"}
    assert (snap.created_by, snap.created_by_name) == ("u-ada", "ada")
    assert snap.deployment_id is None and snap.workspace_id == DEFAULT_WORKSPACE_ID
    (unversioned,) = _snap_rows(legacy)
    assert unversioned.aws_version is None  # honest: no known version, no rollback target
    for agent_id in skipped:
        assert _snap_rows(agent_id) == []
    assert [s.note for s in _snap_rows(already)] == ["earlier"]


def test_the_baseline_backfill_is_safe_on_an_empty_ledger():
    assert snap_service.backfill_baselines() == 0


def test_a_backfill_that_loses_a_race_does_not_raise(monkeypatch):
    """Two processes starting at once both see "no snapshot" and race the unique
    (agent_id, seq) index: the loser rolls back whole and the boot goes on."""
    agent_id = _row(name="raced", version="1")
    _row(name="raced-2", version="1")
    real = snap_service.record_snapshot
    calls: list[str] = []

    def racing(db, agent, **kw):
        calls.append(agent.name)
        if len(calls) == 2:
            raise sa.exc.IntegrityError("INSERT INTO spec_snapshots", {}, Exception("dup"))
        return real(db, agent, **kw)

    monkeypatch.setattr(snap_service, "record_snapshot", racing)
    assert snap_service.backfill_baselines() == 0
    assert _snap_rows(agent_id) == []  # the first insert went with the rollback
    monkeypatch.setattr(snap_service, "record_snapshot", real)
    assert snap_service.backfill_baselines() == 2  # the next startup finishes the job


def test_startup_runs_the_baseline_backfill():
    from app import main

    assert main.backfill_snapshot_baselines is snap_service.backfill_baselines


# ─── canary-promote creator helper ───────────────────────────────────────────


def test_record_snapshot_prefers_the_actor_over_the_agent_creator():
    agent_id = _row(name="actor-agent", created_by="u-ada", created_by_name="ada")
    db = SessionLocal()
    try:
        snap = snap_service.record_snapshot(
            db, db.get(Agent, agent_id), actor=Creator(None, "operator"), note="n"
        )
        db.commit()
        assert (snap.seq, snap.created_by, snap.created_by_name) == (1, None, "operator")
        second = snap_service.record_snapshot(db, db.get(Agent, agent_id))
        db.commit()
        assert (second.seq, second.created_by_name) == (2, "ada")
    finally:
        db.close()


# ─── a console-v3-era ledger ──────────────────────────────────────────────────


def test_a_branch_era_snapshot_table_is_migrated_to_the_creator_convention(tmp_path):
    """console-v3 created `spec_snapshots` without `created_by_name`, with a username
    (or a label such as "canary") in `created_by`: it moves to the name, and the id
    becomes the matching `users.id`. Rows are kept."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'v3.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE spec_snapshots DROP COLUMN created_by_name"))
        conn.execute(sa.text(
            "INSERT INTO users (id, username, username_key, email, password_hash, role, "
            "status, created_by, login_count, created_at, updated_at) VALUES "
            "('u-alice', 'alice', 'alice', 'alice@acme-corp.com', 'x', 'member', 'active', "
            "'self', 0, '2026-10-01', '2026-10-01')"
        ))
        conn.execute(sa.text(
            "INSERT INTO agents (id, workspace_id, name, method, status, spec, owner, "
            "created_at, updated_at) VALUES ('a1', 'default', 'v3-agent', 'harness', "
            "'active', '{}', 'river', '2026-10-01', '2026-10-01')"
        ))
        for sid, seq, who in (("s1", 1, "alice"), ("s2", 2, "canary"), ("s3", 3, None)):
            conn.execute(
                sa.text(
                    "INSERT INTO spec_snapshots (id, workspace_id, agent_id, seq, spec, "
                    "aws_version, created_by, note, created_at) VALUES "
                    "(:id, 'default', 'a1', :seq, '{}', '1', :who, NULL, '2026-10-01')"
                ),
                {"id": sid, "seq": seq, "who": who},
            )
    db_module._migrate(engine)
    db_module._migrate(engine)  # a second run is a no-op
    assert_no_schema_drift(engine)
    with engine.begin() as conn:
        rows = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                sa.text("SELECT id, created_by, created_by_name FROM spec_snapshots")
            )
        }
    assert rows == {
        "s1": ("u-alice", "alice"),
        "s2": (None, "canary"),
        "s3": (None, None),
    }


def test_spec_snapshots_is_a_workspace_scoped_table():
    assert "spec_snapshots" in db_module.WORKSPACE_SCOPED_TABLES
