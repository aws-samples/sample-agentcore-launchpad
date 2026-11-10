"""Ledger spec snapshots (upstream #277, the CN reads-only subset): one per publish, the
as-deployed stamp, diff, the startup baseline, visibility.

Ported from upstream `tests/test_spec_snapshots.py` without the rollback route (not
ported in CN — a redeploy publishes any spec) and the console-v3 ledger migration (no
CN ledger ever ran that branch).
"""

import pytest
import sqlalchemy as sa

import app.routers.agents as agents_router
from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
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
HARNESS_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:harness/xyz"

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
        agent.arn = HARNESS_ARN
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
    # ledger times are UTC; SQLite drops the zone, the wire must not (+08:00, N17-obs463)
    assert all(r["created_at"].endswith("+08:00") for r in rows)
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

    execute_deploy_job(job_id, resume=True)  # the startup resume path

    rows = _snap_rows(agent_id)
    assert len(rows) == 1 and rows[0].aws_version == "7"


# ─── diff ─────────────────────────────────────────────────────────────────────


def test_diff_is_field_level_and_grouped(client):
    agent_id = _create(client)
    _redeploy(client, agent_id, {
        **SPEC,
        "system_prompt": "Be verbose.",
        "model_id": "global.moonshotai.kimi-k3",
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


# ─── workspaces + members ─────────────────────────────────────────────────────


def test_snapshots_do_not_cross_workspaces(client):
    agent_id = _create(client)
    db = SessionLocal()
    db.add(Workspace(id="other", name="Other", account_id="222233334444",
                     region="cn-north-1", bootstrap_status="ready"))
    db.commit()
    db.close()
    for url in (
        f"/api/agents/{agent_id}/snapshots",
        f"/api/agents/{agent_id}/snapshots/1",
        f"/api/agents/{agent_id}/snapshots/diff?from_seq=1",
    ):
        res = client.get(url, headers={"X-Workspace": "other"})
        assert res.status_code == 404 and res.json()["code"] == "agent.not_found", url


def test_a_deleted_agents_history_stays_readable(client):
    agent_id = _create(client)
    db = SessionLocal()
    db.get(Agent, agent_id).status = "deleted"
    db.commit()
    db.close()
    assert client.get(f"/api/agents/{agent_id}").status_code == 404  # the agent itself: gone
    rows = client.get(f"/api/agents/{agent_id}/snapshots").json()["snapshots"]
    assert [r["seq"] for r in rows] == [1]  # its version ledger: still there


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
        f"/api/agents/{agent_id}/snapshots",
        f"/api/agents/{agent_id}/snapshots/1",
        f"/api/agents/{agent_id}/snapshots/diff?from_seq=1",
    )
    assert ada_client.get(urls[0]).status_code == 200  # shared: visible
    _isolate()
    for url in urls:
        res = ada_client.get(url)
        assert res.status_code == 404 and res.json()["code"] == "agent.not_found", url
    assert bob_client.get(urls[0]).status_code == 200  # the creator still sees it
    assert admin.get(urls[0]).status_code == 200


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
    assert unversioned.aws_version is None  # honest: no known version
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


def test_spec_snapshots_is_a_workspace_scoped_table():
    assert "spec_snapshots" in db_module.WORKSPACE_SCOPED_TABLES
