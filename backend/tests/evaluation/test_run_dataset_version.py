"""Cloud-dataset runs can pin a published dataset version.

``RunCreate.dataset_version`` (a version number such as "2", never "DRAFT")
makes ``_cloud_dataset_items`` read that version from both GetDataset and
ListDatasetExamples so the replayed scenarios and ground truth are exactly the
snapshot's. Without it the DRAFT is read and no ``datasetVersion`` kwarg is
sent. The version is validated against ListDatasetVersions before any run row
exists and is persisted on ``EvalRun.dataset_version``.
"""

import json

from app.core.db import SessionLocal
from app.evaluation.models import EvalRun
from tests.evaluation.test_datasets_v2 import SCENARIO, fake_store, seed_cloud
from tests.evaluation.test_run_scopes import wait_terminal
from tests.evaluation.test_runs_flow import make_agent, stub_environment

CLOUD_ID = "cloudds-9"
EXAMPLE = {"exampleId": "ex-1", **SCENARIO}


def _stub_cloud(monkeypatch, *, versions=("3", "2", "1")):
    """A cloud snapshot store ``cloudds-9`` named ``pinned`` with published ``versions``;
    the published snapshots carry a marker turn so a pinned run provably replayed them."""
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id=CLOUD_ID, name="pinned", examples=[EXAMPLE],
               versions=tuple(sorted(versions, key=int)))
    for v in versions:
        snapshot = {**SCENARIO, "turns": [{"input": f"version {v} prompt"}]}
        s3.objects[("bucket", f"eval-datasets/{CLOUD_ID}/v{v}.json")] = json.dumps(
            {"examples": [snapshot], "createdAt": "2026-09-01T00:00:00+00:00",
             "exampleCount": 1}).encode()
    return s3


def _agent(name):
    db = SessionLocal()
    agent = make_agent(db, name=name)
    db.close()
    return agent


def _run_count(agent_id):
    db = SessionLocal()
    try:
        return db.query(EvalRun).filter(EvalRun.agent_id == agent_id).count()
    finally:
        db.close()


def test_pinned_version_reads_that_version_and_is_persisted(client, monkeypatch):
    agent = _agent("pin-agent")
    _data, prompts = stub_environment(monkeypatch)
    _stub_cloud(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "2",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["dataset_version"] == "2"
    assert created["dataset_name"] == "cloud:pinned"  # version never encoded here
    assert created["dataset_id"] == CLOUD_ID

    run = wait_terminal(client, created["id"])
    assert run["dataset_version"] == "2"
    assert run["status"] == "completed", run.get("error")
    assert prompts["prompts"] == ["version 2 prompt"]  # the snapshot, not the draft
    db = SessionLocal()
    try:
        assert db.get(EvalRun, created["id"]).dataset_version == "2"
    finally:
        db.close()


def test_without_version_reads_the_draft(client, monkeypatch):
    agent = _agent("draft-agent")
    _data, prompts = stub_environment(monkeypatch)
    _stub_cloud(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["dataset_version"] is None
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert prompts["prompts"] == ["I want a refund", "Order 123 please"]  # the draft


def test_unknown_version_is_422_and_creates_no_run(client, monkeypatch):
    agent = _agent("unknown-agent")
    _data, prompts = stub_environment(monkeypatch)
    _stub_cloud(monkeypatch, versions=("3", "2", "1"))

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "9",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "run.dataset_version_unknown"
    assert prompts["prompts"] == []  # nothing replayed
    assert _run_count(agent.id) == 0


def test_version_with_local_dataset_or_window_is_scope_error(client, monkeypatch):
    agent = _agent("scope-agent")
    _data, prompts = stub_environment(monkeypatch)
    _stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "local", "items": [{"prompt": "2+2?", "expected": "4"}],
    }).json()

    for scope in ({"dataset_id": ds["id"]}, {"lookback_hours": 24}):
        res = client.post("/api/eval/runs", json={
            "agent_id": agent.id, "dataset_version": "2",
            "evaluators": ["Builtin.Correctness"], "wait_seconds": 0, **scope,
        })
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "run.dataset_version_scope"
    # the literal DRAFT is not a version — omit the field for the draft
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "DRAFT",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.dataset_version_scope"
    assert prompts["prompts"] == []
    assert _run_count(agent.id) == 0


def test_runs_list_reports_null_version_for_old_rows(client, monkeypatch):
    agent = _agent("list-agent")
    stub_environment(monkeypatch)
    _stub_cloud(monkeypatch)
    # a pre-feature row: created directly without the column set
    db = SessionLocal()
    old = EvalRun(
        workspace_id=agent.workspace_id, agent_id=agent.id, agent_name=agent.name,
        dataset_id=CLOUD_ID, dataset_name="cloud:pinned", status="completed",
    )
    db.add(old)
    db.commit()
    old_id = old.id
    db.close()

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "3",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    new_id = res.json()["id"]
    wait_terminal(client, new_id)

    listed = {r["id"]: r for r in client.get("/api/eval/runs").json()["runs"]}
    assert listed[old_id]["dataset_version"] is None
    assert listed[new_id]["dataset_version"] == "3"
    assert client.get(f"/api/eval/runs/{old_id}").json()["dataset_version"] is None
