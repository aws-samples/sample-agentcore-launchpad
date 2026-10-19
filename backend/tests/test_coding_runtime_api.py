"""Coding runtime install job (app/coding/install.py) and the `/api/coding/runtime`
routes: single-flight, recorded keys, failure + startup sweep, and member vs admin."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.coding import infra, install, status
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Job, Workspace
from app.services import users as users_service
from tests.conftest import client_error, set_default_resources

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/launchpad_coding_runtime-x"
INSTALLED = {
    "artifacts_bucket": "launchpad-artifacts-test",
    infra.KEY_ROLE_ARN: "arn:aws:iam::111122223333:role/launchpad-coding-runtime",
    infra.KEY_IMAGE_TAG: "coding-runtime-0123456789ab",
    infra.KEY_IMAGE_DIGEST: "sha256:abc",
    infra.KEY_RUNTIME_ID: "launchpad_coding_runtime-x",
    infra.KEY_RUNTIME_ARN: ARN,
    infra.KEY_MODELS: list(get_settings().coding_models),
}


@pytest.fixture(autouse=True)
def _fresh_status_cache():
    status._CACHE.clear()
    yield
    status._CACHE.clear()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


@pytest.fixture
def member(app, admin):
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/register", json={
        "username": "ada", "email": "ada@acme-corp.com", "password": password,
    }).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, "ada")
        user.status = users_service.STATUS_ACTIVE
        user.role = "member"
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
    finally:
        db.close()
    assert client.post("/api/auth/login", json={"username": "ada", "password": password}
                       ).status_code == 200
    return client


@pytest.fixture
def started(monkeypatch):
    """Job ids the routes handed to a worker thread (none actually starts)."""
    ids: list[str] = []
    monkeypatch.setattr(install, "start_async", lambda job_id: ids.append(job_id))
    return ids


class StatusControl:
    def __init__(self, status_value: str | None = "READY"):
        self.status_value = status_value
        self.gets = 0

    def get_agent_runtime(self, agentRuntimeId):
        self.gets += 1
        if self.status_value is None:
            raise client_error("ResourceNotFoundException")
        if self.status_value == "BOOM":
            raise client_error("AccessDeniedException")
        return {"agentRuntimeId": agentRuntimeId, "status": self.status_value}


@pytest.fixture
def aws(monkeypatch):
    stub = StatusControl()
    monkeypatch.setattr(status, "control_client", lambda _ctx: stub)
    return stub


def _jobs() -> list[Job]:
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.type.in_(install.JOB_TYPES)).all()
    finally:
        db.close()


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        return db.get(Job, job_id)
    finally:
        db.close()


def _queue(job_type=install.INSTALL_JOB_TYPE):
    db = SessionLocal()
    try:
        return install.request_job(db, DEFAULT_WORKSPACE_ID, job_type, requested_by="u1",
                                   requested_by_name="operator")
    finally:
        db.close()


def _resources() -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Workspace, DEFAULT_WORKSPACE_ID).resources or {})
    finally:
        db.close()


# ─── the job ─────────────────────────────────────────────────────────────────


def test_request_job_is_single_flight_per_workspace():
    job, started = _queue()
    assert started and job.status == "queued"
    again, started_again = _queue()
    assert (again.id, started_again) == (job.id, False), "same request → the live job"
    with pytest.raises(AppError) as err:
        _queue(install.UNINSTALL_JOB_TYPE)
    assert err.value.code == "coding.runtime_busy" and err.value.status_code == 409
    assert len(_jobs()) == 1
    assert "install requested by operator" in _job(job.id).log


def test_install_job_records_the_keys_and_succeeds(monkeypatch):
    keys = {**INSTALLED}
    keys.pop("artifacts_bucket")

    def fake_install(ctx, workspace_id, log):
        assert workspace_id == DEFAULT_WORKSPACE_ID
        for stage in ("role", "image", "runtime", "wait"):
            log(stage, f"{stage} ok")
        return keys

    monkeypatch.setattr(infra, "ensure_coding_runtime", fake_install)
    job, _ = _queue()
    install.execute_job(job.id)
    done = _job(job.id)
    assert done.status == "succeeded" and done.error is None
    stages = [line for line in done.log.splitlines()]
    assert [s for s in ('"stage": "role"', '"stage": "image"', '"stage": "runtime"',
                        '"stage": "wait"', '"stage": "done"') if not any(s in ln for ln in stages)
            ] == []
    resources = _resources()
    for key, value in keys.items():
        assert resources[key] == value
    assert resources["artifacts_bucket"], "existing keys are kept"
    # a second worker for the same job is inert (queued → running CAS)
    install.execute_job(job.id)
    assert _job(job.id).status == "succeeded"


def test_install_job_failure_is_recorded(monkeypatch):
    def broken(ctx, workspace_id, log):
        log("role", "creating role")
        raise RuntimeError("AccessDenied: iam:CreateRole")

    monkeypatch.setattr(infra, "ensure_coding_runtime", broken)
    job, _ = _queue()
    install.execute_job(job.id)
    failed = _job(job.id)
    assert failed.status == "failed"
    assert "AccessDenied" in failed.error
    assert '"stage": "failed"' in failed.log
    assert infra.KEY_RUNTIME_ARN not in _resources()
    # nothing live any more: a new request queues a fresh job
    retry, started = _queue()
    assert started and retry.id != job.id


def test_uninstall_job_clears_the_keys(monkeypatch):
    set_default_resources(INSTALLED)
    monkeypatch.setattr(infra, "uninstall_coding_runtime",
                        lambda ctx, wid, log: {k: None for k in infra.RESOURCE_KEYS})
    job, _ = _queue(install.UNINSTALL_JOB_TYPE)
    install.execute_job(job.id)
    assert _job(job.id).status == "succeeded"
    resources = _resources()
    assert all(resources.get(k) is None for k in infra.RESOURCE_KEYS)
    assert resources["artifacts_bucket"]


def test_startup_sweep_fails_interrupted_jobs_only():
    db = SessionLocal()
    try:
        db.add_all([
            Job(id="j-run", workspace_id="default", type=install.INSTALL_JOB_TYPE,
                status="running"),
            Job(id="j-q", workspace_id="default", type=install.UNINSTALL_JOB_TYPE, status="queued"),
            Job(id="j-ok", workspace_id="default", type=install.INSTALL_JOB_TYPE,
                status="succeeded"),
            Job(id="j-deploy", workspace_id="default", type="deploy_agent", status="running"),
        ])
        db.commit()
    finally:
        db.close()
    assert sorted(install.sweep_interrupted_jobs()) == ["j-q", "j-run"]
    assert _job("j-run").status == "failed" and _job("j-run").error == install.INTERRUPTED
    assert _job("j-q").status == "failed"
    assert _job("j-ok").status == "succeeded"
    assert _job("j-deploy").status == "running", "other job types are not ours"


def test_running_projects_is_zero_without_the_projects_ledger():
    db = SessionLocal()
    try:
        assert install.running_projects(db, DEFAULT_WORKSPACE_ID) == 0
    finally:
        db.close()


# ─── routes ──────────────────────────────────────────────────────────────────


def test_status_not_installed_for_everyone(admin, member, aws):
    for client in (admin, member):
        body = client.get("/api/coding/runtime").json()
        assert body["installed"] is False
        assert body["status"] == "not_installed"
        assert body["runtime_arn"] is None and body["image_tag"] is None and body["job"] is None
        assert body["models"] == get_settings().coding_models
        assert body["efforts"] == ["low", "medium", "high", "xhigh"]
        assert body["default_model"] == "global.openai.gpt-6-sol"
        assert body["default_effort"] == "medium"
    assert aws.gets == 0, "nothing installed: no AWS read"


def test_status_ready_reads_aws_once_per_ttl_and_hides_operator_detail(admin, member, aws):
    set_default_resources(INSTALLED)
    job, _ = _queue()
    install._finish(SessionLocal(), job.id, "succeeded")
    a = admin.get("/api/coding/runtime").json()
    m = member.get("/api/coding/runtime").json()
    assert a["status"] == m["status"] == "ready"
    assert a["installed"] is m["installed"] is True
    assert a["image_tag"] == m["image_tag"] == "coding-runtime-0123456789ab"
    assert a["runtime_arn"] == ARN and m["runtime_arn"] is None
    assert a["job"]["log_tail"] and "log_tail" not in m["job"]
    assert a["job"]["id"] == m["job"]["id"] == job.id
    assert aws.gets == 1, "cached for 10 s"
    assert "[queued] install requested by operator" in a["job"]["log_tail"][0]


@pytest.mark.parametrize("aws_status, state, installed", [
    ("CREATING", "installing", True),
    ("UPDATING", "installing", True),
    ("UPDATE_FAILED", "failed", True),
    ("DELETING", "uninstalling", True),
    (None, "not_installed", False),
])
def test_status_follows_the_runtime(admin, aws, aws_status, state, installed):
    set_default_resources(INSTALLED)
    aws.status_value = aws_status
    body = admin.get("/api/coding/runtime").json()
    assert (body["status"], body["installed"]) == (state, installed)


def test_status_degrades_when_aws_cannot_be_read(admin, aws):
    set_default_resources(INSTALLED)
    aws.status_value = "BOOM"
    body = admin.get("/api/coding/runtime").json()
    assert body["status"] == "ready" and "AccessDenied" in body["status_error"]


def test_status_shows_live_and_failed_jobs(admin, member, aws):
    job, _ = _queue()
    assert admin.get("/api/coding/runtime").json()["status"] == "installing"
    install._finish(SessionLocal(), job.id, "failed", "CodeBuild FAILED: BUILD: exit 1")
    a = admin.get("/api/coding/runtime").json()
    m = member.get("/api/coding/runtime").json()
    assert a["status"] == m["status"] == "failed"
    assert a["job"]["error"].startswith("CodeBuild") and m["job"]["error"] is None


def test_install_and_uninstall_are_admin_only(member, started):
    assert member.post("/api/coding/runtime/install").status_code == 403
    assert member.delete("/api/coding/runtime").status_code == 403
    assert started == [] and _jobs() == []


def test_admin_install_answers_202_with_the_job_and_is_idempotent(admin, started, aws):
    first = admin.post("/api/coding/runtime/install")
    assert first.status_code == 202
    body = first.json()
    assert set(body) == {"job_id", "status"} and body["status"] == "queued"
    again = admin.post("/api/coding/runtime/install")
    assert again.status_code == 202 and again.json()["job_id"] == body["job_id"]
    assert started == [body["job_id"]], "only the first request starts a worker"
    busy = admin.delete("/api/coding/runtime")
    assert busy.status_code == 409 and busy.json()["code"] == "coding.runtime_busy"


def test_admin_uninstall_answers_202_and_is_refused_while_projects_run(admin, started,
                                                                         monkeypatch):
    monkeypatch.setattr(install, "running_projects", lambda db, wid: 2)
    refused = admin.delete("/api/coding/runtime")
    assert refused.status_code == 409
    assert refused.json()["code"] == "coding.runtime_in_use"
    monkeypatch.setattr(install, "running_projects", lambda db, wid: 0)
    ok = admin.delete("/api/coding/runtime")
    assert ok.status_code == 202
    assert _job(ok.json()["job_id"]).type == install.UNINSTALL_JOB_TYPE
    assert started == [ok.json()["job_id"]]
