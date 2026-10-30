"""N-SCN-obs10 round-6 (r8-a): ``POST /api/eval/datasets/from-sessions`` as a job.

Hermetic — the observability reads are the in-memory fixtures of ``test_pipelines``. The
sync / async rule: ≤ ``FROM_SESSIONS_SYNC_MAX`` sessions → 201 as before; above → 202 +
``GET /api/jobs/{id}``; ``?sync=false`` forces the job, ``?sync=true`` forces 201 and is
refused above the cap. The job carries progress, the result (dataset id / name / item count,
added, skipped, timing) and, on failure, the AppError envelope; a restart sweeps it failed.
"""

import threading
import time

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import pipeline_routers
from app.models.ledger import Job
from tests.evaluation.test_pipelines import SID_A, SID_B, SID_EMPTY, fake_sessions  # noqa: F401

MANY = [f"sess-{i:08d}-jobs" for i in range(12)]  # > FROM_SESSIONS_SYNC_MAX


def _generic_sessions(monkeypatch):
    def get_session(session_id, range_key, db, workspace, force=False, logs=None):
        return {
            "session_id": session_id,
            "summary": {"agent": "hr-bot"},
            "transcript": {"available": True, "turns": [
                {"role": "user", "text": f"question {session_id}", "at": "t1"},
                {"role": "assistant", "text": f"answer {session_id}", "at": "t2"},
            ]},
        }
    monkeypatch.setattr(pipeline_routers.observability, "get_session", get_session)


def _wait(client, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {job}")


def test_more_than_the_cap_runs_as_a_job_with_progress_and_result(client, monkeypatch):
    _generic_sessions(monkeypatch)
    res = client.post("/api/eval/datasets/from-sessions", json={
        "session_ids": MANY, "name": "big", "range": "24h"})
    assert res.status_code == 202, res.text
    accepted = res.json()
    assert accepted["status"] == "queued" and accepted["sessions"] == 12
    assert accepted["poll"] == f"/api/jobs/{accepted['job_id']}"

    job = _wait(client, accepted["job_id"])
    assert job["type"] == pipeline_routers.FROM_SESSIONS_JOB_TYPE
    assert job["status"] == "succeeded", job
    assert job["payload"]["progress"] == {"read": 12, "total": 12}
    result = job["payload"]["result"]
    assert result["added"] == 12 and result["skipped"] == []
    assert result["dataset"]["name"] == "big" and result["dataset"]["item_count"] == 12
    assert result["dataset"]["kind"] == "predefined"
    assert result["timing"]["sessions"] == 12 and result["timing"]["workers"] == 4
    assert "principal" in job["payload"]  # the visibility rule the worker applied
    stages = {e["stage"] for e in job["events"]}
    assert stages == {pipeline_routers.FROM_SESSIONS_STAGE}
    assert job["events"][0]["msg"].startswith("queued: 12 session(s)")
    assert job["events"][-1]["msg"].startswith(f"dataset {result['dataset']['id']}")
    # the dataset really exists, in the request's session order
    rows = client.get("/api/eval/datasets").json()["datasets"]
    ds = next(d for d in rows if d["id"] == result["dataset"]["id"])
    assert [it["metadata"]["session_id"] for it in ds["items"]] == MANY


def test_sync_false_forces_the_job_for_a_small_input(client, fake_sessions):  # noqa: F811
    res = client.post("/api/eval/datasets/from-sessions?sync=false", json={
        "session_ids": [SID_A, SID_EMPTY, "sess-missing-0009"], "name": "small-job"})
    assert res.status_code == 202, res.text
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "succeeded"
    result = job["payload"]["result"]
    assert result["added"] == 1
    assert {s["session_id"]: s["reason"] for s in result["skipped"]} == {
        SID_EMPTY: "no_transcript", "sess-missing-0009": "not_found"}
    # N17-obs161: `_read_details` reads `workers` sessions at a time (ThreadPoolExecutor), so the
    # ORDER of the gets is not a contract — the set is; the dataset keeps the request order.
    assert sorted(fake_sessions["get"]) == sorted([SID_A, SID_EMPTY, "sess-missing-0009"])


def test_sync_true_above_the_cap_is_refused_and_the_default_small_input_stays_201(
    client, fake_sessions,  # noqa: F811
):
    res = client.post("/api/eval/datasets/from-sessions?sync=true", json={
        "session_ids": MANY, "name": "too-big"})
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "from_sessions.sync_too_large"
    assert body["detail"] == {"sessions": 12, "max": pipeline_routers.FROM_SESSIONS_SYNC_MAX}
    # nothing was filed
    db = SessionLocal()
    assert db.query(Job).filter(Job.type == pipeline_routers.FROM_SESSIONS_JOB_TYPE).count() == 0
    db.close()

    res = client.post("/api/eval/datasets/from-sessions", json={
        "session_ids": [SID_A, SID_B], "name": "still-sync"})
    assert res.status_code == 201 and res.json()["added"] == 2  # the r7-G shape, unchanged


def test_job_appends_to_an_existing_dataset_and_reports_the_target_kind(
    client, fake_sessions,  # noqa: F811
):
    from tests.evaluation.test_pipelines import _dataset

    ds = _dataset([{"prompt": "Reset my password"}], "legacy")
    res = client.post("/api/eval/datasets/from-sessions?sync=false", json={
        "session_ids": [SID_A, SID_B], "dataset_id": ds})
    assert res.status_code == 202
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["payload"]["kind"] == "legacy"
    assert job["payload"]["result"]["dataset"] == {
        "id": ds, "name": "seed", "kind": "legacy", "item_count": 2}
    assert job["payload"]["result"]["added"] == 1  # SID_B's question was already there


def test_job_failure_carries_the_error_envelope(client, fake_sessions):  # noqa: F811
    res = client.post("/api/eval/datasets/from-sessions?sync=false", json={
        "session_ids": [SID_EMPTY], "name": "empty"})
    assert res.status_code == 202
    job = _wait(client, res.json()["job_id"])
    assert job["status"] == "failed"
    assert job["payload"]["error"]["code"] == "dataset.nothing_extracted"
    assert job["payload"]["error"]["detail"]["skipped"] == [
        {"session_id": SID_EMPTY, "reason": "no_transcript"}]
    assert "none of the selected sessions" in job["error"]
    assert job["events"][-1]["level"] == "error"
    assert "result" not in job["payload"]


def test_simulated_target_is_refused_before_any_job_is_filed(client, fake_sessions):  # noqa: F811
    from tests.evaluation.test_pipelines import _dataset

    ds = _dataset([{"scenario_id": "s1", "input": "hi",
                    "actor_profile": {"context": "c", "goal": "g"}}], "simulated")
    res = client.post("/api/eval/datasets/from-sessions?sync=false",
                      json={"session_ids": [SID_A], "dataset_id": ds})
    assert res.status_code == 400 and res.json()["code"] == "dataset.kind_unsupported"


def test_job_is_workspace_scoped_like_every_job(client, fake_sessions):  # noqa: F811
    res = client.post("/api/eval/datasets/from-sessions?sync=false", json={
        "session_ids": [SID_A], "name": "scoped"})
    job_id = res.json()["job_id"]
    db = SessionLocal()
    assert db.get(Job, job_id).workspace_id == DEFAULT_WORKSPACE_ID
    db.close()
    _wait(client, job_id)


def test_restart_sweep_fails_an_interrupted_job_instead_of_rerunning_it(client):
    db = SessionLocal()
    job = Job(workspace_id=DEFAULT_WORKSPACE_ID, type=pipeline_routers.FROM_SESSIONS_JOB_TYPE,
              status="running", payload={"session_ids": [SID_A], "name": "cut"})
    done = Job(workspace_id=DEFAULT_WORKSPACE_ID, type=pipeline_routers.FROM_SESSIONS_JOB_TYPE,
               status="succeeded", payload={})
    other = Job(workspace_id=DEFAULT_WORKSPACE_ID, type="deploy_agent", status="running",
                payload={})
    db.add_all([job, done, other])
    db.commit()
    ids = (job.id, done.id, other.id)
    db.close()

    assert pipeline_routers.sweep_interrupted_from_sessions_jobs() == [ids[0]]

    swept = client.get(f"/api/jobs/{ids[0]}").json()
    assert swept["status"] == "failed"
    assert swept["payload"]["error"]["code"] == "from_sessions.interrupted"
    assert "restart" in swept["error"] and swept["events"][-1]["level"] == "error"
    assert client.get(f"/api/jobs/{ids[1]}").json()["status"] == "succeeded"
    assert client.get(f"/api/jobs/{ids[2]}").json()["status"] == "running"  # not ours
    assert pipeline_routers.sweep_interrupted_from_sessions_jobs() == []  # idempotent


def _status(client, job_id: str) -> str:
    return client.get(f"/api/jobs/{job_id}").json()["status"]


def test_concurrent_jobs_are_capped_by_the_gate(client, monkeypatch):
    """r8-a2 review note B: a member filing N jobs must not run N × FROM_SESSIONS_WORKERS
    Logs Insights reads at once — the gate (Settings.from_sessions_job_concurrency, default
    2) lets two run and keeps the third ``queued`` until a slot frees; all three finish."""
    _generic_sessions(monkeypatch)
    monkeypatch.setattr(pipeline_routers, "_job_gate", threading.BoundedSemaphore(2))
    release = threading.Event()
    entered: list[str] = []
    real_read = pipeline_routers._read_items

    def gated_read(db, scope, private, session_ids, *args, **kwargs):
        entered.append(session_ids[0])
        assert release.wait(10), "test harness never released the reads"
        return real_read(db, scope, private, session_ids, *args, **kwargs)

    monkeypatch.setattr(pipeline_routers, "_read_items", gated_read)
    job_ids = []
    for n in range(3):
        res = client.post("/api/eval/datasets/from-sessions?sync=false", json={
            "session_ids": [f"sess-gate-{n}-{i:06d}" for i in range(2)],
            "name": f"gated-{n}", "range": "24h"})
        assert res.status_code == 202, res.text
        job_ids.append(res.json()["job_id"])

    deadline = time.monotonic() + 5
    while len(entered) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.3)  # the third thread had every chance to start — and must not have
    assert len(entered) == 2, entered
    statuses = sorted(_status(client, j) for j in job_ids)
    assert statuses == ["queued", "running", "running"], statuses
    waiting = next(j for j in job_ids if _status(client, j) == "queued")
    events = client.get(f"/api/jobs/{waiting}").json().get("events") or []
    assert any("waiting for a slot" in str(e) for e in events), events

    release.set()
    for j in job_ids:
        assert _wait(client, j)["status"] == "succeeded"
    assert len(entered) == 3
    # the gate is whole again: nothing leaked a slot
    gate = pipeline_routers._job_gate
    assert gate is not None and gate.acquire(blocking=False) and gate.acquire(blocking=False)
    gate.release()
    gate.release()
