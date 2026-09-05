"""Cloud-dataset runs can pin a published dataset version.

``RunCreate.dataset_version`` (a version number such as "2", never "DRAFT")
makes ``_cloud_dataset_items`` read that version from both GetDataset and
ListDatasetExamples so the replayed scenarios and ground truth are exactly the
snapshot's. Without it the DRAFT is read and no ``datasetVersion`` kwarg is
sent. The version is validated against ListDatasetVersions before any run row
exists and is persisted on ``EvalRun.dataset_version``.
"""

from unittest.mock import MagicMock

from app.core.db import SessionLocal
from app.evaluation.models import EvalRun
from tests.evaluation.test_datasets_v2 import SCENARIO
from tests.evaluation.test_run_scopes import wait_terminal
from tests.evaluation.test_runs_flow import make_agent, stub_environment

CLOUD_ID = "cloudds-9"
EXAMPLE = {"exampleId": "ex-1", **SCENARIO}


def _stub_cloud(monkeypatch, *, versions=("3", "2", "1")):
    stub = MagicMock()
    stub.get_dataset.return_value = {
        "datasetId": CLOUD_ID, "datasetName": "pinned", "status": "ACTIVE",
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1", "exampleCount": 1,
    }
    stub.list_dataset_examples.return_value = {"examples": [EXAMPLE]}
    stub.list_dataset_versions.return_value = {
        "versions": [
            {"datasetVersion": v, "exampleCount": 1, "createdAt": f"2026-09-0{i + 1}T00:00:00Z"}
            for i, v in enumerate(reversed(versions))
        ]
    }
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    return stub


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
    stub_environment(monkeypatch)
    stub = _stub_cloud(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "2",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    created = res.json()
    assert created["dataset_version"] == "2"
    assert created["dataset_name"] == "cloud:pinned"  # version never encoded here
    assert created["dataset_id"] == CLOUD_ID

    stub.get_dataset.assert_called_once_with(datasetId=CLOUD_ID, datasetVersion="2")
    stub.list_dataset_examples.assert_called_once_with(datasetId=CLOUD_ID, datasetVersion="2")

    run = wait_terminal(client, created["id"])
    assert run["dataset_version"] == "2"
    db = SessionLocal()
    try:
        assert db.get(EvalRun, created["id"]).dataset_version == "2"
    finally:
        db.close()


def test_pinned_version_paginates_with_token_only(monkeypatch):
    from app.evaluation import agentcore_eval as ac

    stub = MagicMock()
    stub.list_dataset_examples.side_effect = [
        {"examples": [EXAMPLE], "nextToken": "tok"},
        {"examples": [EXAMPLE]},
    ]
    out = ac.list_dataset_examples(stub, dataset_id=CLOUD_ID, version="2")
    assert len(out) == 2
    first, second = stub.list_dataset_examples.call_args_list
    assert first.kwargs == {"datasetId": CLOUD_ID, "datasetVersion": "2"}
    assert second.kwargs == {"datasetId": CLOUD_ID, "nextToken": "tok"}


def test_without_version_reads_draft_and_sends_no_version_kwarg(client, monkeypatch):
    agent = _agent("draft-agent")
    stub_environment(monkeypatch)
    stub = _stub_cloud(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    assert res.json()["dataset_version"] is None
    assert "datasetVersion" not in stub.get_dataset.call_args.kwargs
    assert "datasetVersion" not in stub.list_dataset_examples.call_args.kwargs
    stub.list_dataset_versions.assert_not_called()  # nothing to validate
    wait_terminal(client, res.json()["id"])


def test_unknown_version_is_422_and_creates_no_run(client, monkeypatch):
    agent = _agent("unknown-agent")
    stub_environment(monkeypatch)
    stub = _stub_cloud(monkeypatch, versions=("3", "2", "1"))

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": CLOUD_ID, "dataset_version": "9",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "run.dataset_version_unknown"
    stub.list_dataset_examples.assert_not_called()
    stub.get_dataset.assert_not_called()
    assert _run_count(agent.id) == 0


def test_version_with_local_dataset_or_window_is_scope_error(client, monkeypatch):
    agent = _agent("scope-agent")
    stub_environment(monkeypatch)
    stub = _stub_cloud(monkeypatch)
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
    stub.get_dataset.assert_not_called()
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
