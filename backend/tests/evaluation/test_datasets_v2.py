"""Dataset v2 — scenario schema, editing, AWS sync, ground-truth runs.

Scenario items follow the devguide predefined schema: {scenario_id,
turns:[{input, expected_response?}], expected_trajectory?, assertions?}.
"""

import json

import pytest
from sqlalchemy import create_engine, inspect, text

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, _migrate
from app.evaluation import service as svc
from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.scenarios import ground_truth_metadata, normalize_scenarios
from tests.conftest import ws_ctx
from tests.evaluation.fakes import strip_ci
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal

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


# ─── cloud sync (S3 snapshot store, CA-6) ────────────────────────────────────
PREDEFINED = "AGENTCORE_EVALUATION_PREDEFINED_V1"
SIMULATED = "AGENTCORE_EVALUATION_SIMULATED_V1"


def fake_store(monkeypatch, bucket="bucket"):
    """An in-memory S3 behind ``datasets_local`` + the bucket on the default workspace."""
    from app.evaluation import datasets_local
    from app.models.ledger import Workspace
    from tests.evaluation.fakes import FakeS3

    s3 = FakeS3()
    with SessionLocal() as db:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.resources = {**(row.resources or {}), "artifacts_bucket": bucket}
        db.commit()
    monkeypatch.setattr(datasets_local, "s3_client", lambda _ws: s3)
    return s3


def seed_cloud(s3, *, cloud_id="cloudds-7", name="remote_scenarios", schema=PREDEFINED,
               examples=None, versions=(), status="ACTIVE", draft_status="MODIFIED",
               workspace_id=DEFAULT_WORKSPACE_ID, bucket="bucket", failure_reason=None):
    """A cloud-only snapshot store (no local row) with ``versions`` of the same examples."""
    examples = [SCENARIO] if examples is None else list(examples)
    body = lambda payload: json.dumps(payload).encode()  # noqa: E731
    prefix = f"eval-datasets/{cloud_id}/"
    s3.objects[(bucket, prefix + "draft.json")] = body({"examples": examples})
    listed = []
    for i, v in enumerate(versions):
        created = f"2026-09-0{i + 1}T00:00:00+00:00"
        s3.objects[(bucket, prefix + f"v{v}.json")] = body(
            {"examples": examples, "createdAt": created, "exampleCount": len(examples)})
        listed.append({"datasetVersion": str(v), "exampleCount": len(examples),
                       "createdAt": created})
    s3.objects[(bucket, prefix + "meta.json")] = body({
        "datasetId": cloud_id, "datasetName": name, "workspaceId": workspace_id,
        "schemaType": schema, "description": "", "status": status,
        "draftStatus": draft_status, "exampleCount": len(examples),
        "failureReason": failure_reason, "createdAt": "2026-09-01T00:00:00+00:00",
        "updatedAt": "2026-09-05T00:00:00+00:00", "versions": listed,
    })
    return s3


def _meta(s3, cloud_id, bucket="bucket"):
    return json.loads(s3.objects[(bucket, f"eval-datasets/{cloud_id}/meta.json")])


def test_sync_to_aws_success(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "sync me!", "items": [{"prompt": "2+2?", "expected": "4"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    cloud = res.json()["cloud"]
    meta = _meta(s3, cloud["dataset_id"])
    assert meta["schemaType"] == PREDEFINED
    assert meta["datasetName"] == "sync_me"  # sanitized
    draft = json.loads(s3.objects[("bucket", f"eval-datasets/{cloud['dataset_id']}/draft.json")])
    assert draft["examples"] == normalize_scenarios(ds["items"])
    assert cloud["status"] == "ACTIVE"
    assert cloud["synced_at"]
    assert cloud["failure_reason"] is None


def test_sync_simulated_dataset_uses_simulated_schema(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "personas", "items": [PERSONA],
    }).json()
    assert ds["kind"] == "simulated"
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    cloud_id = res.json()["cloud"]["dataset_id"]
    assert _meta(s3, cloud_id)["schemaType"] == SIMULATED
    draft = json.loads(s3.objects[("bucket", f"eval-datasets/{cloud_id}/draft.json")])
    assert draft["examples"] == [PERSONA]


def test_sync_to_aws_create_failed_persists_blob(client, monkeypatch):
    s3 = fake_store(monkeypatch)

    def boom(**kwargs):
        raise RuntimeError("bad examples")

    monkeypatch.setattr(s3, "put_object", boom)
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
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-9", name="remote_only", examples=[SCENARIO] * 7,
               draft_status=None)
    res = client.get("/api/eval/datasets/cloud")
    assert res.status_code == 200
    assert res.json()["datasets"] == [{
        "datasetId": "cloudds-9", "name": "remote_only", "status": "ACTIVE",
        "schemaType": PREDEFINED, "exampleCount": 7,
        "draftStatus": None, "updatedAt": "2026-09-05T08:00:00+08:00",  # N17-obs463
    }]


def test_cloud_delete_marks_local_copy(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "synced", "items": [{"prompt": "x"}],
    }).json()
    synced = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws").json()
    cloud_id = synced["cloud"]["dataset_id"]
    res = client.delete(f"/api/eval/datasets/cloud/{cloud_id}")
    assert res.status_code == 200
    assert not any(k.startswith(f"eval-datasets/{cloud_id}/") for (_b, k) in s3.objects)
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "deleted"


def stub_cloud_run(monkeypatch, *, schema_type=PREDEFINED, status="ACTIVE", examples=None):
    """A cloud-only snapshot store for cloud-dataset-scoped runs and the detail endpoint."""
    if examples is None:
        base = PERSONA if schema_type == SIMULATED else SCENARIO
        examples = [{"exampleId": "ex-1", **base}]
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-7", name="remote_scenarios", schema=schema_type,
               examples=examples, status=status)
    return s3


def test_cloud_dataset_detail_reports_ground_truth(client, monkeypatch):
    stub_cloud_run(monkeypatch)
    res = client.get("/api/eval/datasets/cloud/cloudds-7")
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "remote_scenarios"
    assert body["runnable"] is True
    assert body["has_ground_truth"] is True


def test_cloud_dataset_detail_simulated_runnable_with_gt(client, monkeypatch):
    stub_cloud_run(monkeypatch, schema_type=SIMULATED)
    res = client.get("/api/eval/datasets/cloud/cloudds-7")
    assert res.status_code == 200
    body = res.json()
    assert body["runnable"] is True
    assert body["has_ground_truth"] is True  # persona assertions count
    assert body["schemaType"] == SIMULATED


def test_run_on_cloud_dataset(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-run-agent")
    db.close()
    stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7",
        "evaluators": ["Builtin.TrajectoryInOrderMatch", "Builtin.Correctness"],
        "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["dataset_id"] == "cloudds-7"
    assert body["dataset_name"] == "cloud:remote_scenarios"
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
    stub_cloud_run(monkeypatch, schema_type=SIMULATED)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7", "wait_seconds": 0,
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.actor_model_required"


def test_run_refuses_more_than_ten_evaluators_before_any_replay(client, monkeypatch):
    """StartBatchEvaluation accepts at most ten evaluators; the route refuses an
    over-long selection up front instead of replaying every scenario first."""
    from app.evaluation.evaluators_local import ALL_BUILTIN_EVALUATORS

    db = SessionLocal()
    agent = make_agent(db, name="cloud-too-many")
    db.close()
    stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7", "wait_seconds": 0,
        "evaluators": list(ALL_BUILTIN_EVALUATORS)[:11],
    })
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "run.too_many_evaluators" and body["detail"]["selected"] == 11


def test_run_on_simulated_cloud_dataset_with_actor_model(client, monkeypatch):
    """Persona scenarios ride to the worker with their actor profile; the worker drives the
    ActorSimulator (here a fake) against the pre-assigned session and the persona's
    assertions ride along as ground truth."""
    from app.services.strands_eval import jobs_batch

    db = SessionLocal()
    agent = make_agent(db, name="cloud-sim-ok-agent")
    db.close()
    env, calls = stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch, schema_type=SIMULATED)
    sim_calls: list[dict] = []

    def build_actor(profile, initial_query, *, model, max_turns=10):
        sim_calls.append({"profile": profile, "input": initial_query, "model": model,
                          "max_turns": max_turns})
        return object()

    def drive(actor, invoke):
        reply = invoke("turn-1", None)  # the actor's first message → the agent
        return reply["session_id"]

    monkeypatch.setattr(jobs_batch.simulator, "build_actor", build_actor)
    monkeypatch.setattr(jobs_batch.simulator, "drive", drive)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7",
        "actor_model_id": "global.moonshotai.kimi-k3",
        "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    assert sim_calls[0]["profile"]["goal"] == "Get a PTO request submitted and confirmed"
    assert sim_calls[0]["input"] == PERSONA["input"] and sim_calls[0]["max_turns"] == 8
    assert sim_calls[0]["model"] is env.judge  # the injected actor model, never a default
    assert calls["n"] == 1 and calls["sessions"] == run["session_ids"]
    (job,) = env.submitted
    assert job.spec["actor_model_id"] == "global.moonshotai.kimi-k3"
    session = job.spec["sessions"][0]
    assert session["actor_profile"]["goal"] == PERSONA["actor_profile"]["goal"]
    # persona assertions ride along as ground truth
    assert job.spec["ground_truth"][session["session_id"]]["assertions"] == [
        "Agent submits a PTO request"]
def test_run_rejects_local_plus_cloud_dataset(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="cloud-xor-agent")
    ds = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="local", items=[{"prompt": "x"}])
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
def test_multi_turn_scenarios_replay_in_one_session(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="scenario-agent")
    run = EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, agent_name=agent.name, mode="evaluators",
        evaluators=["Builtin.Correctness"], status="queued")
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    env, calls = stub_environment(monkeypatch)
    items = [SCENARIO, {"prompt": "standalone question"}]
    workspace = ws_ctx(id=DEFAULT_WORKSPACE_ID)
    plan = svc.plan_run(
        run_id, workspace=workspace, agent_arn=agent.arn, agent_id=agent.id, protocol="http",
        service_name="svc.DEFAULT", log_group="/lg", log_groups=["aws/spans", "/lg"],
        items=items, evaluators=["Builtin.Correctness"], mode="evaluators", wait_seconds=0,
        existing_session_ids=None, time_range=None, insights=None, actor_model_id=None,
        runtime_user_id=None,
    )
    svc.execute_run(run_id, workspace=workspace, plan=plan)
    assert calls["prompts"] == ["I want a refund", "Order 123 please", "standalone question"]
    assert calls["sessions"][0] == calls["sessions"][1]  # same scenario → same session
    assert calls["sessions"][2] != calls["sessions"][0]  # next scenario → new session
    refund_sid = calls["sessions"][0]
    (job,) = env.submitted
    truth = job.spec["ground_truth"]
    assert truth[refund_sid]["scenario_id"] == "refund_flow"
    assert truth[refund_sid]["expected_trajectory"] == ["lookup_order", "issue_refund"]
    assert truth[refund_sid]["assertions"] == ["The agent confirms the refund"]
    assert truth[calls["sessions"][2]]["expected_response"] is None  # the plain prompt
    db = SessionLocal()
    assert db.get(EvalRun, run_id).status == "completed"
    db.close()
def test_trajectory_evaluator_needs_ground_truth(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="traj-agent")
    plain = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="no-gt", items=[{"prompt": "x"}])
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
    ds = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="with-gt", kind="predefined", items=[SCENARIO])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()
    env, _ = stub_environment(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds_id,
        "evaluators": ["Builtin.TrajectoryInOrderMatch"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["status"] == "completed", run.get("error")
    (job,) = env.submitted
    sid = job.spec["sessions"][0]["session_id"]
    assert job.spec["ground_truth"][sid]["expected_trajectory"] == ["lookup_order", "issue_refund"]
    # the fixture session called hr_lookup only → the deterministic matcher scores 0
    assert strip_ci(run["scores"]) == [{
        "evaluatorId": "Builtin.TrajectoryInOrderMatch", "score": 0.0,
                              "count": 1, "n": 1, "unit": "session"}]
def _stub_judge(monkeypatch=None):
    """One ground-truth judge row ({expected_response} placeholder) in the default
    workspace — the CN evaluator is the ``eval_evaluators`` row (E1-1)."""
    from app.evaluation.models import EvalEvaluator

    with SessionLocal() as db:
        db.add(EvalEvaluator(
            id=GT_JUDGE_ID, workspace_id=DEFAULT_WORKSPACE_ID, name="ref_grounding",
            level="TRACE", definition="judge", model_id="global.zai.glm-5.3",
            instructions=("Context: {context}\nAnswer: {assistant_turn}\n"
                          "Reference: {expected_response}\nScore it."),
            rating_scale=[],
        ))
        db.commit()


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


# ─── custom-judge ground-truth preflight ─────────────────────────────────────
GT_JUDGE_ID = "ref_grounding-abc123"


def _stub_judge(monkeypatch=None):
    """One ground-truth judge row ({expected_response} placeholder) in the default
    workspace — the CN evaluator is the ``eval_evaluators`` row (E1-1)."""
    from app.evaluation.models import EvalEvaluator

    with SessionLocal() as db:
        db.add(EvalEvaluator(
            id=GT_JUDGE_ID, workspace_id=DEFAULT_WORKSPACE_ID, name="ref_grounding",
            level="TRACE", definition="judge", model_id="global.zai.glm-5.3",
            instructions=("Context: {context}\nAnswer: {assistant_turn}\n"
                          "Reference: {expected_response}\nScore it."),
            rating_scale=[],
        ))
        db.commit()


def test_judge_ground_truth_gate_rejects_scopes_without_it(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db, name="judge-gate-agent")
    plain = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID, name="no-gt-judge", items=[{"prompt": "x"}])
    db.add(plain)
    db.commit()
    plain_id = plain.id
    db.close()
    _stub_judge(monkeypatch)

    # dataset scope, but no scenario carries expected_response
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": plain_id,
        "evaluators": [GT_JUDGE_ID],
    })
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "run.judge_needs_ground_truth"
    assert "expected_response" in body["message"]

    # window scope — ground truth is impossible there
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24,
        "evaluators": [GT_JUDGE_ID],
    })
    assert res.status_code == 422
    assert res.json()["code"] == "run.judge_needs_ground_truth"


def test_judge_ground_truth_gate_accepts_dataset_with_expected_response(
    client, monkeypatch
):
    db = SessionLocal()
    agent = make_agent(db, name="judge-ok-agent")
    # a {expected_response} judge is applied to EVERY turn: every turn carries one here
    referenced = json.loads(json.dumps(SCENARIO))
    referenced["turns"][1]["expected_response"] = "order 123 refunded"
    ds = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID, name="gt-judge", kind="predefined",
        items=[referenced])
    db.add(ds)
    db.commit()
    ds_id = ds.id
    db.close()
    stub_environment(monkeypatch)
    _stub_judge(monkeypatch)

    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds_id,
        "evaluators": [GT_JUDGE_ID], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text


def test_judge_ground_truth_gate_skips_builtins_and_refuses_unknown_judges(
    client, monkeypatch
):
    """SE-047 B: an unknown custom evaluator has UNKNOWN reference needs; the gate refuses
    before any run row (the agent would otherwise be invoked for every scenario first)."""
    db = SessionLocal()
    agent = make_agent(db, name="judge-open-agent")
    plain = EvalDataset(
        workspace_id=DEFAULT_WORKSPACE_ID, name="no-gt-open", items=[{"prompt": "x"}])
    db.add(plain)
    db.commit()
    plain_id = plain.id
    db.close()
    stub_environment(monkeypatch)

    # builtin-only selection needs no row lookup
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": plain_id,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 201, res.text

    # an id that is neither a builtin nor a row of this workspace: refused, no run row
    from app.evaluation.models import EvalRun

    with SessionLocal() as db:
        before = db.query(EvalRun).count()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": plain_id,
        "evaluators": ["ref_grounding-missing"], "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "run.evaluator_not_found"
    assert "ref_grounding-missing" in res.json()["message"]
    with SessionLocal() as db:
        assert db.query(EvalRun).count() == before


@pytest.mark.siliconflow
def test_run_refuses_an_actor_model_whose_provider_is_not_configured(client, monkeypatch):
    """N-R6-obs1: the actor simulator runs on the worker with the provider's key — a
    SiliconFlow actor without the key is a 422 `model.provider_unconfigured` at start."""
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    db = SessionLocal()
    agent = make_agent(db, name="cloud-sim-unconfigured-agent")
    db.close()
    stub_environment(monkeypatch)
    stub_cloud_run(monkeypatch, schema_type=SIMULATED)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "cloud_dataset_id": "cloudds-7",
        "actor_model_id": "siliconflow.zai-org/GLM-5.3",
        "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "model.provider_unconfigured"
    assert res.json()["detail"] == {"actor_model_id": "siliconflow.zai-org/GLM-5.3"}
    assert "launchpad-siliconflow-api-key" in res.json()["message"]
