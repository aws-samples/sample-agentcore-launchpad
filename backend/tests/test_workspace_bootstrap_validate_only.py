# ruff: noqa: F811  — pytest fixtures are imported by name from test_workspace_bootstrap
"""The validate-only bootstrap mode (r17-ws, N17-obs117): `POST /api/workspaces/{id}/bootstrap`
with `{"validate_only": true}` (or `{"stages": ["validate-access"]}`) runs the `validate-access`
stage alone — nothing provisioned, the workspace row handed back in the status it had, the
job's terminal status the verdict. The release smoke `scripts/e2e_workspace_region_smoke.py`
drives this mode against a real host; everything here is hermetic (the stateful fake AWS of
`test_workspace_bootstrap.py`).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.models.ledger import Job, Workspace
from app.services import workspace_bootstrap as wb
from tests.test_workspace_bootstrap import (  # noqa: F401 — fixtures by name
    REGION,
    WS_ID,
    FakeAws,
    _client_error,
    _fast,
    _job_row,
    _register,
    _row,
    _stub_skill_lab_stage,
    aws,
    default_records,
    global_bedrock_key,
    no_thread,
)

VALIDATE_ONLY = ["validate-access"]


def _queue(stages: list[str] | None = None, workspace_id: str = WS_ID) -> str:
    db = SessionLocal()
    try:
        job = wb.create_bootstrap_job(db, db.get(Workspace, workspace_id), stages=stages)
        return job.id
    finally:
        db.close()


def _run(job_id: str) -> Job:
    wb.execute_bootstrap_job(job_id, timeouts=_fast())
    return _job_row(job_id)


def _statuses(job: Job) -> dict[str, str]:
    return {entry["name"]: entry["status"] for entry in wb.job_stages(job)}


def _deny_ecr(aws: FakeAws) -> None:
    def denied(**_kw):
        raise ClientError(
            {"Error": {"Code": "AccessDeniedException",
                       "Message": "User is not authorized to perform ecr:DescribeRepositories"}},
            "DescribeRepositories",
        )

    aws.ecr.describe_repositories = denied  # type: ignore[method-assign]


# ── the service ───────────────────────────────────────────────────────────────


def test_a_validate_only_run_executes_one_stage_creates_nothing_and_restores_the_status(aws):
    _register()
    job = _run(_queue(VALIDATE_ONLY))

    assert job.status == "succeeded" and job.error is None
    assert wb.job_mode(job) == wb.MODE_VALIDATE_ONLY
    assert _statuses(job) == {"validate-access": "succeeded"}
    assert wb.job_stages(job)[0]["detail"] == f"{REGION} usable · 5 services probed"
    # nothing provisioned, nothing recorded
    assert aws.iam.created == [] and aws.s3.created == [] and aws.ecr.created == []
    assert aws.codebuild.created == [] and aws.control.created_gateways == []
    assert _row().resources == {}
    # a verdict, not a provisioning step: the row is `registered` again, not `bootstrapping`
    assert _row().bootstrap_status == "registered"


def test_a_failed_validate_only_run_names_the_denial_and_does_not_mark_the_workspace_failed(aws):
    """The N17-obs112 shape: ECR refuses the probe. The job carries the stage's own sentence
    (service, operation, region, code) and the row stays `registered` — nothing was attempted,
    so there is nothing to resume."""
    _register()
    _deny_ecr(aws)
    job = _run(_queue(VALIDATE_ONLY))

    assert job.status == "failed"
    assert _statuses(job) == {"validate-access": "failed"}
    detail = wb.job_stages(job)[0]["detail"]
    assert f"ecr DescribeRepositories failed in {REGION}: AccessDeniedException" in detail
    assert "the hub's credentials must be able to provision this workspace" in detail
    assert job.error == detail
    assert _row().bootstrap_status == "registered"
    assert _row().resources == {}


def test_a_validate_only_run_on_a_failed_workspace_hands_back_failed(aws):
    """A workspace whose full run failed keeps its `failed` status (and its resume) through an
    access check in either direction."""
    _register(bootstrap_status="failed")
    assert _run(_queue(VALIDATE_ONLY)).status == "succeeded"
    assert _row().bootstrap_status == "failed"
    _deny_ecr(aws)
    assert _run(_queue(VALIDATE_ONLY)).status == "failed"
    assert _row().bootstrap_status == "failed"


def test_a_full_run_never_carries_a_validate_only_stage_forward(aws):
    _register()
    assert _run(_queue(VALIDATE_ONLY)).status == "succeeded"

    full = _job_row(_queue())
    assert wb.job_mode(full) == wb.MODE_FULL
    assert set(_statuses(full).values()) == {"pending"}, _statuses(full)
    assert [e["name"] for e in wb.job_stages(full)] == wb.STAGE_ORDER


def test_a_full_run_resumes_from_the_previous_full_run_across_a_validate_only_run(aws):
    """The carry-forward reads the newest FULL job, not the newest job."""
    _register()

    def foreign_bucket(**_kw):
        raise _client_error("BucketAlreadyExists", "CreateBucket")

    # an instance attribute shadows the fake's method; deleting it heals the seam (a
    # monkeypatch.undo() here would also undo the `aws` fixture's client funnel)
    aws.s3.create_bucket = foreign_bucket  # type: ignore[method-assign]
    first = _run(_queue())
    assert first.status == "failed" and _statuses(first)["storage"] == "failed"
    assert _row().bootstrap_status == "failed"
    assert _statuses(first)["iam"] == "succeeded"

    del aws.s3.create_bucket
    assert _run(_queue(VALIDATE_ONLY)).status == "succeeded"
    assert _row().bootstrap_status == "failed"

    resumed = _job_row(_queue())
    statuses = _statuses(resumed)
    assert statuses["validate-access"] == statuses["iam"] == "succeeded"
    assert statuses["model-credential"] == "succeeded"
    assert statuses["storage"] == "pending"
    assert wb.job_stages(resumed)[1]["detail"] == "already provisioned by an earlier run"


def test_any_other_subset_is_refused_before_the_claim(aws):
    _register()
    for stages in (["iam"], ["validate-access", "iam"], [], ["finalize"]):
        with pytest.raises(wb.UnsupportedStages):
            _queue(stages)
        assert _row().bootstrap_status == "registered", stages
    db = SessionLocal()
    try:
        assert db.query(Job).count() == 0
    finally:
        db.close()


def test_latest_job_filters_by_mode(aws):
    _register()
    full_id = _queue()
    _deny_ecr(aws)
    assert _run(full_id).status == "failed"  # `failed` keeps the slot claimable for the probe
    del aws.ecr.describe_repositories
    probe_id = _queue(VALIDATE_ONLY)
    assert _run(probe_id).status == "succeeded"
    db = SessionLocal()
    try:
        # two jobs within one clock tick sort by their random ids: date the first one back
        first = db.get(Job, full_id)
        first.created_at = first.created_at - timedelta(seconds=5)
        db.commit()
        assert wb.latest_job(db, WS_ID).id == probe_id
        assert wb.latest_job(db, WS_ID, mode=wb.MODE_VALIDATE_ONLY).id == probe_id
        assert wb.latest_job(db, WS_ID, mode=wb.MODE_FULL).id == full_id
    finally:
        db.close()


def test_a_pre_r17_ws_job_without_a_mode_is_a_full_run(aws):
    """Jobs queued before the mode existed carry no `mode` and the full stage list."""
    _register()
    db = SessionLocal()
    try:
        job = Job(workspace_id=WS_ID, type=wb.JOB_TYPE,
                  payload={"workspace_id": WS_ID, "stages": wb._new_stages()})
        db.add(job)
        db.commit()
        job_id = job.id
    finally:
        db.close()
    assert wb.job_mode(_job_row(job_id)) == wb.MODE_FULL
    done = _run(job_id)
    assert done.status == "succeeded" and _row().bootstrap_status == "ready"


# ── the route ─────────────────────────────────────────────────────────────────


def test_the_route_accepts_validate_only_and_reports_the_mode(client, no_thread):
    _register()
    response = client.post(f"/api/workspaces/{WS_ID}/bootstrap", json={"validate_only": True})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "validate-only"
    assert [s["name"] for s in body["stages"]] == VALIDATE_ONLY
    assert no_thread == [body["job_id"]]
    status = client.get(f"/api/workspaces/{WS_ID}/bootstrap").json()
    assert status["job"]["id"] == body["job_id"] and status["job"]["mode"] == "validate-only"


def test_the_route_accepts_the_spelled_out_stage_list(client, no_thread):
    _register()
    response = client.post(f"/api/workspaces/{WS_ID}/bootstrap", json={"stages": VALIDATE_ONLY})
    assert response.status_code == 202, response.text
    assert response.json()["mode"] == "validate-only"


def test_the_route_without_a_body_is_the_full_run(client, no_thread):
    _register()
    body = client.post(f"/api/workspaces/{WS_ID}/bootstrap").json()
    assert body["mode"] == "full"
    assert [s["name"] for s in body["stages"]] == wb.STAGE_ORDER
    _register("acct-two", region="us-west-2")
    body = client.post("/api/workspaces/acct-two/bootstrap", json={}).json()
    assert body["mode"] == "full"


def test_the_route_refuses_any_other_subset_with_422(client, no_thread):
    _register()
    for payload in ({"stages": ["iam"]}, {"stages": ["validate-access", "iam"]},
                    {"stages": []}):
        response = client.post(f"/api/workspaces/{WS_ID}/bootstrap", json=payload)
        assert response.status_code == 422, (payload, response.text)
        assert response.json()["code"] == "workspace.bootstrap_stages_unsupported"
        assert response.json()["detail"]["supported"] == VALIDATE_ONLY
    assert no_thread == []
    assert _row().bootstrap_status == "registered"


def test_a_validate_only_run_through_the_route_is_pollable_and_leaves_the_row_registered(
    client, aws, no_thread
):
    _register()
    job_id = client.post(f"/api/workspaces/{WS_ID}/bootstrap",
                         json={"validate_only": True}).json()["job_id"]
    assert _row().bootstrap_status == "bootstrapping"  # claimed while the job runs
    wb.execute_bootstrap_job(job_id, timeouts=_fast())

    polled = client.get(f"/api/jobs/{job_id}", headers={"X-Workspace": WS_ID})
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] == "succeeded"
    assert polled.json()["payload"]["mode"] == "validate-only"
    assert [s["status"] for s in polled.json()["payload"]["stages"]] == ["succeeded"]
    assert _row().bootstrap_status == "registered"
    # and a full run may follow at once (the probe did not claim the slot for good)
    assert client.post(f"/api/workspaces/{WS_ID}/bootstrap").status_code == 202


# ── the startup sweep (rev-r17 nit: nothing stays `bootstrapping` with no one to finish it) ──


def _leave_running(job_id: str, *, stage: str = "validate-access") -> None:
    """What a process death leaves: the job `running`, the stage `running`, the row
    `bootstrapping` (the claim already happened in create_bootstrap_job)."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        job.status = "running"
        payload = dict(job.payload or {})
        stages = [dict(s) for s in payload["stages"]]
        for entry in stages:
            if entry["name"] == stage:
                entry["status"] = "running"
        payload["stages"] = stages
        job.payload = payload
        db.commit()
    finally:
        db.close()


def test_the_sweep_fails_an_interrupted_probe_and_hands_the_row_back(aws, monkeypatch):
    from app.deployer.pipeline import resume_pending_jobs

    started: list[str] = []
    monkeypatch.setattr(wb, "start_bootstrap_async", lambda job_id: started.append(job_id))
    _register(bootstrap_status="failed")
    job_id = _queue(VALIDATE_ONLY)
    _leave_running(job_id)
    assert _row().bootstrap_status == "bootstrapping"

    resumed = resume_pending_jobs()

    assert job_id not in resumed and started == []  # a probe is never re-run by the sweep
    job = _job_row(job_id)
    assert job.status == "failed" and job.error == wb.INTERRUPTED_PROBE_ERROR
    assert _statuses(job) == {"validate-access": "failed"}
    assert wb.job_stages(job)[0]["detail"] == wb.INTERRUPTED_PROBE_ERROR
    assert wb.INTERRUPTED_PROBE_ERROR in job.log
    assert _row().bootstrap_status == "failed"  # the status it had, not a new `failed`
    # and the slot is free again: a full run may be queued at once
    assert wb.create_bootstrap_job(SessionLocal(), _row()).id


def test_the_sweep_leaves_a_full_job_for_the_resume(aws, monkeypatch):
    from app.deployer.pipeline import resume_pending_jobs

    started: list[str] = []
    monkeypatch.setattr(wb, "start_bootstrap_async", lambda job_id: started.append(job_id))
    _register()
    job_id = _queue()
    _leave_running(job_id, stage="storage")

    resumed = resume_pending_jobs()

    assert started == [job_id] and job_id in resumed
    assert _job_row(job_id).status == "running"  # untouched: the resume adopts it
    assert _row().bootstrap_status == "bootstrapping"


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("none", "registered"),
        ("full_failed", "failed"),
        ("full_succeeded", "ready"),
        ("probe_done", "failed"),
    ],
)
def test_the_sweep_restores_a_row_stuck_bootstrapping_with_no_live_job(aws, setup, expected):
    """The process died between the job's terminal commit and the row's status write (or the
    job row is gone): the row is restored from its newest job."""
    from app.deployer.pipeline import resume_pending_jobs

    _register(bootstrap_status="failed" if setup == "probe_done" else "registered")
    if setup == "full_failed":
        _deny_ecr(aws)
        _run(_queue())
    elif setup == "full_succeeded":
        _run(_queue())
    elif setup == "probe_done":
        _run(_queue(VALIDATE_ONLY))
    db = SessionLocal()
    try:
        db.get(Workspace, WS_ID).bootstrap_status = "bootstrapping"
        db.commit()
    finally:
        db.close()

    resume_pending_jobs()

    assert _row().bootstrap_status == expected


def test_the_sweep_is_a_no_op_on_a_healthy_ledger(aws):
    _register()
    assert _run(_queue()).status == "succeeded"
    db = SessionLocal()
    try:
        assert wb.sweep_interrupted(db) == {"probes_failed": [], "full_resumable": [],
                                            "rows_restored": []}
    finally:
        db.close()
    assert _row().bootstrap_status == "ready"
