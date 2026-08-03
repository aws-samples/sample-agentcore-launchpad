"""Dataset v2 — scenario schema, editing, AWS sync, ground-truth runs.

Scenario items follow the devguide predefined schema: {scenario_id,
turns:[{input, expected_response?}], expected_trajectory?, assertions?}.
"""

from unittest.mock import MagicMock

from sqlalchemy import create_engine, inspect, text

from app.core.db import SessionLocal, _migrate
from app.evaluation import service as svc
from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.scenarios import ground_truth_metadata, normalize_scenarios
from tests.evaluation.test_runs_flow import make_agent, stub_environment

SCENARIO = {
    "scenario_id": "refund_flow",
    "turns": [
        {"input": "I want a refund", "expected_response": "refund policy explained"},
        {"input": "Order 123 please"},
    ],
    "expected_trajectory": ["lookup_order", "issue_refund"],
    "assertions": ["The agent confirms the refund"],
}

PERSONA = {
    "scenario_id": "frustrated-employee-leave",
    "scenario_description": "A frustrated employee needs leave booked quickly",
    "input": "I really need time off next week. Can you help?",
    "actor_profile": {
        "traits": {"tone": "frustrated but polite"},
        "context": "An employee whose childcare fell through",
        "goal": "Get a PTO request submitted and confirmed",
    },
    "max_turns": 8,
    "assertions": ["Agent submits a PTO request"],
}


# ─── normalization / ground truth (pure) ─────────────────────────────────────
def test_normalize_legacy_items():
    scenarios = normalize_scenarios([
        {"prompt": "2+2?", "expected": "4"},
        {"prompt": "hello"},
        {"prompt": "empty expected", "expected": ""},
    ])
    assert scenarios[0] == {
        "scenario_id": "item_1",
        "turns": [{"input": "2+2?", "expected_response": "4"}],
    }
    assert scenarios[1] == {"scenario_id": "item_2", "turns": [{"input": "hello"}]}
    assert "expected_response" not in scenarios[2]["turns"][0]


def test_normalize_passes_scenarios_through():
    assert normalize_scenarios([SCENARIO]) == [SCENARIO]


def test_ground_truth_metadata_only_nonempty_keys():
    scenarios = [
        SCENARIO,
        {"scenario_id": "plain", "turns": [{"input": "hi"}]},  # no ground truth
        {"scenario_id": "asserted", "turns": [{"input": "hi"}],
         "assertions": ["is polite"]},
    ]
    meta = ground_truth_metadata(scenarios, ["sid-1", "sid-2", "sid-3"])
    assert [m["sessionId"] for m in meta] == ["sid-1", "sid-3"]  # positional pairing
    full = meta[0]
    assert full["testScenarioId"] == "refund_flow"
    inline = full["groundTruth"]["inline"]
    assert inline["assertions"] == [{"text": "The agent confirms the refund"}]
    assert inline["expectedTrajectory"] == {"toolNames": ["lookup_order", "issue_refund"]}
    assert inline["turns"] == [{
        "input": {"prompt": "I want a refund"},
        "expectedResponse": {"text": "refund policy explained"},
    }]
    asserted = meta[1]["groundTruth"]["inline"]
    assert set(asserted.keys()) == {"assertions"}  # no empty keys


# ─── dataset CRUD with scenarios ─────────────────────────────────────────────
def test_create_scenario_dataset_and_output_fields(client):
    res = client.post("/api/eval/datasets", json={
        "name": "scen-ds", "description": "ground truth set", "items": [SCENARIO],
    })
    assert res.status_code == 201
    body = res.json()
    assert body["kind"] == "predefined"
    assert body["description"] == "ground truth set"
    assert body["has_ground_truth"] is True
    assert body["cloud"] is None


def test_scenario_item_validation(client):
    cases = [
        [{"turns": [{"input": "hi"}]}],  # missing scenario_id
        [{"scenario_id": "a", "turns": []}],  # empty turns
        [{"scenario_id": "a", "turns": [{"input": ""}]}],  # empty input
        [{"scenario_id": "a", "turns": [{"input": "x"}]},
         {"scenario_id": "a", "turns": [{"input": "y"}]}],  # duplicate id
    ]
    for items in cases:
        res = client.post("/api/eval/datasets", json={"name": "bad", "items": items})
        assert res.status_code == 422, items
        assert res.json()["code"] == "dataset.invalid_item"


def test_upload_scenario_jsonl(client):
    import json
    res = client.post("/api/eval/datasets/upload", json={
        "name": "scen-upload", "jsonl": json.dumps(SCENARIO) + "\n",
    })
    assert res.status_code == 201
    assert res.json()["kind"] == "predefined"


def test_put_edits_name_description_items(client):
    ds = client.post("/api/eval/datasets", json={
        "name": "editable", "items": [{"prompt": "old"}],
    }).json()
    res = client.put(f"/api/eval/datasets/{ds['id']}", json={
        "name": "edited", "description": "now described",
        "items": [{"prompt": "new", "expected": "42"}],
    })
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "edited"
    assert body["description"] == "now described"
    assert body["items"][0]["prompt"] == "new"
    assert body["has_ground_truth"] is True  # legacy expected counts


def test_put_kind_immutable(client):
    ds = client.post("/api/eval/datasets", json={
        "name": "legacy-locked", "items": [{"prompt": "old"}],
    }).json()
    res = client.put(f"/api/eval/datasets/{ds['id']}", json={"items": [SCENARIO]})
    assert res.status_code == 400
    assert res.json()["code"] == "dataset.kind_immutable"


# ─── AWS sync ────────────────────────────────────────────────────────────────
def stub_cloud(monkeypatch, *, get_status="ACTIVE", failure_reason=None):
    stub = MagicMock()
    stub.create_dataset.return_value = {
        "datasetId": "cloudds-1",
        "datasetArn": "arn:aws:bedrock-agentcore:us-west-2:1:dataset/cloudds-1",
    }
    detail = {"datasetId": "cloudds-1", "status": get_status,
              "datasetArn": "arn:aws:bedrock-agentcore:us-west-2:1:dataset/cloudds-1",
              "exampleCount": 1}
    if failure_reason:
        detail["failureReason"] = failure_reason
    stub.get_dataset.return_value = detail
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda: stub)
    return stub


def test_sync_to_aws_success(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "sync me!", "items": [{"prompt": "2+2?", "expected": "4"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    kwargs = stub.create_dataset.call_args.kwargs
    assert kwargs["schemaType"] == "AGENTCORE_EVALUATION_PREDEFINED_V1"
    assert kwargs["datasetName"] == "sync_me"  # sanitized
    assert kwargs["source"]["inlineExamples"]["examples"] == normalize_scenarios(
        ds["items"]
    )
    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == "cloudds-1"
    assert cloud["status"] == "ACTIVE"
    assert cloud["synced_at"]
    assert cloud["failure_reason"] is None


def test_sync_simulated_dataset_uses_simulated_schema(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "personas", "items": [PERSONA],
    }).json()
    assert ds["kind"] == "simulated"
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    kwargs = stub.create_dataset.call_args.kwargs
    assert kwargs["schemaType"] == "AGENTCORE_EVALUATION_SIMULATED_V1"
    assert kwargs["source"]["inlineExamples"]["examples"] == [PERSONA]


def test_sync_to_aws_create_failed_persists_blob(client, monkeypatch):
    stub_cloud(monkeypatch, get_status="CREATE_FAILED", failure_reason="bad examples")
    ds = client.post("/api/eval/datasets", json={
        "name": "failing", "items": [{"prompt": "x"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.sync_failed"
    assert "bad examples" in res.json()["message"]
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "CREATE_FAILED"
    assert "bad examples" in row["cloud"]["failure_reason"]


def test_cloud_list_passthrough(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    stub.list_datasets.return_value = {"datasets": [{
        "datasetId": "cloudds-9", "datasetName": "remote_only", "status": "ACTIVE",
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1", "exampleCount": 7,
        "updatedAt": "2026-07-10 00:00:00",
    }]}
    res = client.get("/api/eval/datasets/cloud")
    assert res.status_code == 200
    assert res.json()["datasets"] == [{
        "datasetId": "cloudds-9", "name": "remote_only", "status": "ACTIVE",
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1", "exampleCount": 7,
        "updatedAt": "2026-07-10 00:00:00",
    }]


def test_cloud_delete_marks_local_copy(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "synced", "items": [{"prompt": "x"}],
    }).json()
    client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    res = client.delete("/api/eval/datasets/cloud/cloudds-1")
    assert res.status_code == 200
    stub.delete_dataset.assert_called_once_with(datasetId="cloudds-1")
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "deleted"


def stub_cloud_run(monkeypatch, *, schema_type="AGENTCORE_EVALUATION_PREDEFINED_V1",
                   status="ACTIVE", examples=None):
    """Control-plane stub for cloud-dataset-scoped runs and the detail endpoint."""
    stub = MagicMock()
    stub.get_dataset.return_value = {
        "datasetId": "cloudds-7", "datasetName": "remote_scenarios",
        "status": status, "schemaType": schema_type, "exampleCount": 1,
    }
    if examples is None:
        base = PERSONA if schema_type == "AGENTCORE_EVALUATION_SIMULATED_V1" else SCENARIO
        examples = [{"exampleId": "ex-1", **base}]
    stub.list_dataset_examples.return_value = {"examples": examples}
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda: stub)
    return stub


def test_cloud_dataset_detail_reports_ground_truth(client, monkeypatch):
    stub_cloud_run(monkeypatch)
    res = client.get("/api/eval/datasets/cloud/cloudds-7")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "remote_scenarios"
    assert body["runnable"] is True
    assert body["has_ground_truth"] is True


def test_cloud_dataset_detail_simulated_runnable_with_gt(client, monkeypatch):
    stub_cloud_run(monkeypatch, schema_type="AGENTCORE_EVALUATION_SIMULATED_V1")
    res = client.get("/api/eval/datasets/cloud/cloudds-7")
    assert res.status_code == 200
    body = res.json()
    assert body["runnable"] is True
    assert body["has_ground_truth"] is True  # persona assertions count
    assert body["schemaType"] == "AGENTCORE_EVALUATION_SIMULATED_V1"


def test_run_on_cloud_dataset(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-run-agent")
    db.close()
    stub_environment(monkeypatch)
    stub = stub_cloud_run(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7",
        "evaluators": ["Builtin.TrajectoryInOrderMatch", "Builtin.Correctness"],
        "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["dataset_id"] == "cloudds-7"
    assert body["dataset_name"] == "cloud:remote_scenarios"
    stub.list_dataset_examples.assert_called_once_with(datasetId="cloudds-7")
    import time

    for _ in range(50):
        run = client.get(f"/api/eval/runs/{body['id']}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")


def test_run_on_simulated_dataset_requires_actor_model(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-sim-agent")
    db.close()
    stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch, schema_type="AGENTCORE_EVALUATION_SIMULATED_V1")

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7", "wait_seconds": 0,
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.actor_model_required"


def test_run_on_simulated_cloud_dataset_with_actor_model(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-sim-ok-agent")
    db.close()
    data, _ = stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch, schema_type="AGENTCORE_EVALUATION_SIMULATED_V1")

    sim_calls: list[dict] = []

    def fake_sim(data_client, *, agent_arn, method, scenario, actor_model_id,
                 protocol="http"):
        sim_calls.append({"scenario": scenario, "model": actor_model_id,
                          "method": method})
        return f"sim-sess-{len(sim_calls):03d}" + "x" * 30

    monkeypatch.setattr(svc.simulation, "run_simulated_scenario", fake_sim)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7",
        "actor_model_id": "global.anthropic.claude-haiku-4-5-20251001-v1:0",
        "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    run_id = res.json()["id"]

    import time
    for _ in range(50):
        run = client.get(f"/api/eval/runs/{run_id}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")
    assert sim_calls[0]["model"] == "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    assert sim_calls[0]["scenario"]["scenario_id"] == "frustrated-employee-leave"
    assert run["session_ids"] == ["sim-sess-001" + "x" * 30]
    # persona assertions ride along as ground truth
    meta = data.start_batch_evaluation.call_args.kwargs["evaluationMetadata"]
    assert meta["sessionMetadata"][0]["groundTruth"]["inline"]["assertions"] == [
        {"text": "Agent submits a PTO request"}
    ]


def test_run_rejects_local_plus_cloud_dataset(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-xor-agent")
    ds = EvalDataset(name="local", items=[{"prompt": "x"}])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()
    stub_cloud_run(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds_id, "cloud_dataset_id": "cloudds-7",
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.scope_required"


# ─── ground-truth runs ───────────────────────────────────────────────────────
def test_multi_turn_scenarios_replay_in_one_session(monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="scenario-agent")
    run = EvalRun(agent_id=agent.id, agent_name=agent.name, mode="evaluators",
                  evaluators=["Builtin.Correctness"], status="queued")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    data, _ = stub_environment(monkeypatch)
    calls: list[tuple[str, str]] = []

    def invoke(client, arn, prompt, session_id=None, actor_id="default"):
        sid = session_id or f"gen-{len(calls):02d}" + "x" * 30
        calls.append((sid, prompt))
        return {"text": "ok", "session_id": sid}

    monkeypatch.setattr(svc.rt, "invoke_runtime_text", invoke)

    items = [SCENARIO, {"prompt": "standalone question"}]
    svc.execute_run(
        run_id, agent_arn=agent.arn, method="zip_runtime", service_name="svc.DEFAULT",
        log_group="/lg",
        items=items, evaluators=["Builtin.Correctness"], mode="evaluators",
        wait_seconds=0,
    )
    assert [p for _, p in calls] == [
        "I want a refund", "Order 123 please", "standalone question",
    ]
    assert calls[0][0] == calls[1][0]  # same scenario → same session
    assert calls[2][0] != calls[0][0]  # next scenario → new session

    kwargs = data.start_batch_evaluation.call_args.kwargs
    meta = kwargs["evaluationMetadata"]["sessionMetadata"]
    assert len(meta) == 1  # only the scenario with ground truth
    assert meta[0]["sessionId"] == calls[0][0]
    assert meta[0]["testScenarioId"] == "refund_flow"

    db = SessionLocal()
    assert db.get(EvalRun, run_id).status == "completed"
    db.close()


def test_trajectory_evaluator_needs_ground_truth(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="traj-agent")
    plain = EvalDataset(name="no-gt", items=[{"prompt": "x"}])
    db.add(plain)
    db.commit()
    plain_id = plain.id
    db.close()

    # window scope — no ground truth possible
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": ["Builtin.TrajectoryInOrderMatch"],
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.trajectory_needs_ground_truth"

    # dataset scope but no expected_trajectory
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": plain_id,
        "evaluators": ["Builtin.TrajectoryInOrderMatch"],
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.trajectory_needs_ground_truth"


def test_trajectory_evaluator_accepted_with_ground_truth(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="traj-ok-agent")
    ds = EvalDataset(name="with-gt", kind="predefined", items=[SCENARIO])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()
    stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds_id,
        "evaluators": ["Builtin.TrajectoryInOrderMatch", "Builtin.Correctness"],
        "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    import time

    for _ in range(50):
        run = client.get(f"/api/eval/runs/{res.json()['id']}").json()
        if run["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert run["status"] == "completed", run.get("error")


# ─── migration guard ─────────────────────────────────────────────────────────
def test_migrate_adds_dataset_columns_idempotently(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE eval_datasets (id VARCHAR(16) PRIMARY KEY, "
            "name VARCHAR(64), kind VARCHAR(16), locale VARCHAR(8), "
            "items JSON, created_at DATETIME)"
        ))
    _migrate(eng)
    cols = {c["name"] for c in inspect(eng).get_columns("eval_datasets")}
    assert {"description", "cloud"} <= cols
    _migrate(eng)  # second pass over the upgraded schema — must be a no-op
    cols_again = {c["name"] for c in inspect(eng).get_columns("eval_datasets")}
    assert cols == cols_again
