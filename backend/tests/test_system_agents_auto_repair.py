"""N17-obs206 (user ruling 2026-10-22): installed system presets are repaired automatically.

Hermetic throughout: the deploy launch is a recording fake, the AWS client factory fails
loudly, the scheduler is driven step by step with a fake clock (no thread sleeps in the
suite — the one thread test uses a zero deadline and an Event). Proven over TWO workspaces:
the plan lists every installed, active preset whose last deploy did not fail; a candidate is
offered to `install_preset` only when its turn comes (N17-obs260: at most ONE auto job is
non-terminal, a restart mid-drain resumes at most one) and `_repair` alone decides — a behind
catalog pin or a newer release is claimed, a current one is the `changed=False` no-op; a
failed re-publish is never masked as current and never auto-retried (N17-obs259); the
journal listener debounces three writes into one sweep (max-wait 15 min), personal / audit-
only rows arm nothing, model-role writes do; the startup trigger honours the flag; the
bootstrap step-10 claim and the sweep's claim have exactly one winner.
"""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
import app.routers.system_agents as system_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import harness_local
from app.deployer import pipeline as pipeline_module
from app.main import create_app
from app.models.ledger import Agent, Deployment, Job, Workspace
from app.services import aws_clients, model_registry
from app.system_agents import auto_repair, service
from app.system_agents.auto_repair import Candidate
from app.system_agents.presets import ARCHITECT, EVAL_WORKER, PRESETS

FIXTURE_TEMPLATE = Path(__file__).parent / "fixtures" / "harness_runtime_min"
BUCKET = "launchpad-artifacts-test"
READY_RESOURCES = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
    "memory_arn": ("arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:memory/"
                   "launchpad_memory-x"),
    "memory_id": "launchpad_memory-x",
}
RUNTIME_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/arch-1"
LAB = "lab-use2"
DEF_ARCH = (DEFAULT_WORKSPACE_ID, ARCHITECT.key)
DEF_EVAL = (DEFAULT_WORKSPACE_ID, EVAL_WORKER.key)
LAB_ARCH = (LAB, ARCHITECT.key)
LAB_EVAL = (LAB, EVAL_WORKER.key)


# ---------------------------------------------------------------------------
# fixtures (the test_system_agents.py hermetic trio + a fake scheduler slot)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixture_template(monkeypatch):
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", FIXTURE_TEMPLATE)


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(system_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(auto_repair, "_default_launch",
                        lambda jid: pytest.fail(f"real deploy launch attempted for {jid}"))
    yield launched


@pytest.fixture(autouse=True)
def no_aws_clients(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(f"AWS client requested during a hermetic test: {args} {kwargs}")

    monkeypatch.setattr(aws_clients, "client", boom)
    monkeypatch.setattr(aws_clients, "get_session", boom)


@pytest.fixture(autouse=True)
def fresh_scheduler_slot():
    """Every test starts with no scheduler installed and ends with none (create_app installs
    a real one — its thread never starts because nothing schedules under the suite flag)."""
    previous = auto_repair.set_scheduler(None)
    model_registry.clear_catalog_change_listeners()
    yield
    current = auto_repair.set_scheduler(previous)
    if current is not None:
        current.stop(timeout=1)
    model_registry.clear_catalog_change_listeners()


class FakeWorker:
    """What `start_deploy_async` hands back: a joinable worker. `join` settles the job in
    the ledger (the worker "finished") and records when it was joined; `on_join` lets a
    test simulate the process dying mid-job."""

    def __init__(self, job_id, log, *, alive_after_join=False, settle=True, on_join=None):
        self.job_id, self.log = job_id, log
        self.alive_after_join, self.settle, self.on_join = alive_after_join, settle, on_join

    def join(self, timeout=None):
        self.log.append(("join", self.job_id, timeout))
        if self.on_join is not None:
            self.on_join(self.job_id)
        if not self.settle:
            return
        db = SessionLocal()
        try:
            job = db.get(Job, self.job_id)
            job.status = "succeeded"
            dep = db.query(Deployment).filter(Deployment.job_id == self.job_id).one()
            dep.status = "succeeded"
            dep.catalog_version = model_registry.stamp(db)  # what the generate stage pins
            agent = db.get(Agent, job.payload["agent_id"])
            agent.status = "active"
            db.commit()
        finally:
            db.close()

    def is_alive(self):
        return self.alive_after_join


class FakeLauncher:
    def __init__(self, **worker_kwargs):
        self.log: list[tuple] = []
        self.worker_kwargs = worker_kwargs

    def __call__(self, job_id: str):
        self.log.append(("launch", job_id))
        return FakeWorker(job_id, self.log, **self.worker_kwargs)

    @property
    def launched(self) -> list[str]:
        return [e[1] for e in self.log if e[0] == "launch"]


class FakeClock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def make_scheduler(launcher=None, clock=None, **kw) -> auto_repair.AutoRepairScheduler:
    sched = auto_repair.AutoRepairScheduler(
        launch=launcher or FakeLauncher(), clock=clock or FakeClock(), start_thread=False, **kw)
    auto_repair.set_scheduler(sched)
    return sched


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------


def _mark_ready(workspace_id: str = DEFAULT_WORKSPACE_ID, resources: dict | None = None,
                *, create: bool = False) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        if row is None and create:
            # (account, region) is unique: the second workspace is another Region
            row = Workspace(id=workspace_id, name=workspace_id, account_id="111122223333",
                            region="cn-north-1")
            db.add(row)
        row.bootstrap_status = "ready"
        row.resources = dict(resources or READY_RESOURCES)
        db.commit()
    finally:
        db.close()


def _install(client: TestClient, key: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> str:
    headers = {"X-Workspace": workspace_id}
    res = client.post(f"/api/system-agents/{key}/install", json={}, headers=headers)
    assert res.status_code == 202, res.text
    return res.json()["agent"]["id"]


def _settle(agent_id: str, status: str = "active", *, pin: int | None = None,
            job_failed: bool | None = None) -> None:
    """The stubbed job "finished": agent `status`, job terminal, the newest deployment
    pinned at catalog version `pin` (what the generate stage writes). `status="active"` with
    `job_failed=True` is the N10v3-bug7 shape: the re-publish failed, the live version
    survived."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status = status
        if status == "active":
            agent.arn = RUNTIME_ARN
            agent.resource_id = "arch-1"
        dep = (db.query(Deployment).filter(Deployment.agent_id == agent_id)
               .order_by(Deployment.started_at.desc(), Deployment.id.desc()).first())
        failed = job_failed if job_failed is not None else status != "active"
        dep.status = "failed" if failed else "succeeded"
        if pin is not None:
            dep.catalog_version = pin
        job = db.get(Job, dep.job_id)
        job.status = dep.status
        if failed:
            job.error = agent.error = "boom: package stage failed"
        db.commit()
    finally:
        db.close()


def _stamp() -> int:
    db = SessionLocal()
    try:
        return model_registry.stamp(db)
    finally:
        db.close()


def _agent(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        return db.get(Job, job_id)
    finally:
        db.close()


def _job_count() -> int:
    db = SessionLocal()
    try:
        return db.query(Job).count()
    finally:
        db.close()


def _nonterminal_jobs() -> int:
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.status.in_(["queued", "running"])).count()
    finally:
        db.close()


def _plan() -> tuple[dict[tuple[str, str], Candidate], dict[tuple[str, str], auto_repair.Outcome]]:
    db = SessionLocal()
    try:
        candidates, skipped = auto_repair.plan(db)
        return {c.key: c for c in candidates}, {(o.workspace_id, o.preset_key): o for o in skipped}
    finally:
        db.close()


def _claim(candidate: Candidate) -> auto_repair.Outcome:
    db = SessionLocal()
    try:
        return auto_repair.claim(db, candidate)
    finally:
        db.close()


def _plan_and_claim_all() -> dict[tuple[str, str], auto_repair.Outcome]:
    """Test-only: the plan's skips plus one claim per candidate, keyed by (workspace,
    preset). The product claims lazily (one per drain step, `AutoRepairScheduler._drain`)."""
    candidates, skipped = _plan()
    return {**skipped, **{key: _claim(c) for key, c in candidates.items()}}


def _status(client: TestClient, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict[str, dict]:
    res = client.get("/api/system-agents", headers={"X-Workspace": workspace_id})
    assert res.status_code == 200, res.text
    return {p["key"]: p for p in res.json()["presets"]}


def _two_workspaces_three_installed(client: TestClient) -> dict[str, str]:
    """default: architect BEHIND + eval-worker CURRENT; lab: architect BEHIND."""
    _mark_ready()
    _mark_ready(LAB, create=True)
    current = _stamp()
    ids = {
        "arch-default": _install(client, ARCHITECT.key),
        "eval-default": _install(client, EVAL_WORKER.key),
        "arch-lab": _install(client, ARCHITECT.key, LAB),
    }
    _settle(ids["arch-default"], pin=current - 1)
    _settle(ids["eval-default"], pin=current)
    _settle(ids["arch-lab"], pin=current - 1)
    return ids


# ---------------------------------------------------------------------------
# plan + claim: `_repair` is the single staleness funnel
# ---------------------------------------------------------------------------


def test_plan_lists_every_installed_active_preset_and_claim_repairs_only_the_behind_ones(client):
    ids = _two_workspaces_three_installed(client)
    before = _job_count()
    current = _stamp()

    candidates, skipped = _plan()
    assert set(candidates) == {DEF_ARCH, DEF_EVAL, LAB_ARCH}  # the plan does NOT judge staleness
    assert skipped[LAB_EVAL].reason == auto_repair.R_NOT_INSTALLED
    assert _job_count() == before  # and it claims nothing

    outcomes = _plan_and_claim_all()
    assert {k for k, o in outcomes.items() if o.action == auto_repair.LAUNCHED} == {
        DEF_ARCH, LAB_ARCH}
    current_row = outcomes[DEF_EVAL]
    assert current_row.reason == auto_repair.R_CURRENT
    assert current_row.agent_id == ids["eval-default"]
    assert _job_count() == before + 2  # the current preset was OFFERED, `_repair` said no-op
    for key in ("arch-default", "arch-lab"):
        agent = _agent(ids[key])
        assert agent.status == "deploying"
        launched = outcomes[(agent.workspace_id, ARCHITECT.key)]
        assert launched.agent_id == agent.id and launched.job_id
        job = _job(launched.job_id)
        assert job.status == "queued" and job.payload["mode"] == "update"
        assert job.payload["repair_trigger"] == "auto"
        assert job.payload["repair_reason"] == (
            f"auto: catalog snapshot behind v{current - 1} < v{current}")
        assert job.payload["note"] == job.payload["repair_reason"]
        assert "preset_bundle" in job.payload  # the release pin still rides along
        assert launched.reason == job.payload["repair_reason"]  # `_repair`'s own verdict
    assert _agent(ids["eval-default"]).status == "active"
    # the status read says 自动修复中 through the job summary (ledger only)
    st = _status(client)
    assert st[ARCHITECT.key]["status"] == "deploying"
    assert st[ARCHITECT.key]["last_job"]["trigger"] == "auto"
    assert st[ARCHITECT.key]["last_job"]["repair_reason"].startswith("auto: catalog")
    assert st[EVAL_WORKER.key]["last_job"]["trigger"] is None
    # a second plan pre-filters the deploying rows: nothing more is claimed
    candidates, skipped = _plan()
    assert set(candidates) == {DEF_EVAL}
    assert skipped[DEF_ARCH].reason == auto_repair.R_DEPLOYING
    assert skipped[DEF_ARCH].job_id == outcomes[DEF_ARCH].job_id
    assert _plan_and_claim_all()[DEF_EVAL].reason == auto_repair.R_CURRENT
    assert _job_count() == before + 2


def test_claim_repairs_a_preset_whose_stored_spec_is_on_an_older_release(client):
    """The stored spec pins an older skill release than this build ships — the re-derived
    spec differs, `_repair` re-publishes with the reason `release update`."""
    _mark_ready()
    agent_id = _install(client, ARCHITECT.key)
    _settle(agent_id, pin=_stamp())
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        spec = dict(agent.spec)
        spec["skills"] = [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/0.0.1-abcdefabcdef/"]
        agent.spec = spec
        db.commit()
    finally:
        db.close()
    st = _status(client)[ARCHITECT.key]
    assert st["update_available"] is True and st["snapshot_behind"] is False

    launched = _plan_and_claim_all()[DEF_ARCH]
    assert launched.action == auto_repair.LAUNCHED
    assert launched.reason == "auto: release update"  # `_repair`: the re-derived spec differs
    job = _job(launched.job_id)
    assert job.payload["repair_reason"] == "auto: release update"
    assert job.payload["repair_trigger"] == "auto"


def test_a_current_preset_is_offered_and_left_alone(client):
    """The sweep does not decide staleness: an active preset pinned at the current stamp
    with this build's spec goes through `install_preset` and comes back `changed=False`."""
    _mark_ready()
    agent_id = _install(client, ARCHITECT.key)
    _settle(agent_id, pin=_stamp())
    before = _job_count()
    for _ in range(2):
        outcome = _plan_and_claim_all()[DEF_ARCH]
        assert outcome.action == auto_repair.SKIPPED and outcome.reason == auto_repair.R_CURRENT
        assert outcome.agent_id == agent_id and outcome.job_id is None
    assert _job_count() == before and _agent(agent_id).status == "active"
    # a NEWER pin (another process published) is not behind either
    _settle(agent_id, pin=_stamp() + 1)
    assert _plan_and_claim_all()[DEF_ARCH].reason == auto_repair.R_CURRENT
    assert _job_count() == before


def test_plan_prefilters_deploying_failed_uninstalling_and_claim_reports_not_ready(client):
    _mark_ready()
    _mark_ready(LAB, create=True)
    current = _stamp()
    deploying = _install(client, ARCHITECT.key)  # the stubbed job leaves it deploying
    failed = _install(client, EVAL_WORKER.key)
    _settle(failed, "failed", pin=current - 1)
    lab_arch = _install(client, ARCHITECT.key, LAB)
    _settle(lab_arch, pin=current - 1)
    # the lab workspace stops being ready after the install (bootstrap re-run, bucket gone)
    db = SessionLocal()
    try:
        row = db.get(Workspace, LAB)
        row.resources = {k: v for k, v in row.resources.items() if k != "artifacts_bucket"}
        db.commit()
    finally:
        db.close()
    before = _job_count()

    candidates, skipped = _plan()
    assert set(candidates) == {LAB_ARCH}
    assert skipped[DEF_ARCH].reason == auto_repair.R_DEPLOYING
    assert skipped[DEF_EVAL].reason == auto_repair.R_FAILED
    # not ready: the single funnel's own 409 (`_require_ready`), reported as refused
    not_ready = _claim(candidates[LAB_ARCH])
    assert not_ready.reason == auto_repair.R_REFUSED
    assert not_ready.code == "system_agent.workspace_not_ready"
    assert _job_count() == before
    assert _agent(deploying).status == "deploying" and _agent(failed).status == "failed"

    # uninstalling: the row belongs to its teardown
    _mark_ready(LAB)
    res = client.delete(f"/api/system-agents/{ARCHITECT.key}", headers={"X-Workspace": LAB})
    assert res.status_code == 202
    _, skipped = _plan()
    assert skipped[LAB_ARCH].reason == auto_repair.R_UNINSTALLING


def test_claim_is_fail_soft_and_re_prefilters_the_row(client, monkeypatch):
    """A refusal (409 bundle_invalid) or a crash on one candidate is its own `skipped` row;
    a candidate whose row moved since the plan is re-judged at claim time."""
    ids = _two_workspaces_three_installed(client)
    candidates, _ = _plan()
    real = service.install_preset

    def flaky(db, row, preset, edit, **kw):
        if row.id == LAB:
            raise AppError(service.E_BUNDLE_INVALID, "cannot publish", status_code=409)
        return real(db, row, preset, edit, **kw)

    monkeypatch.setattr(service, "install_preset", flaky)
    refused = _claim(candidates[LAB_ARCH])
    assert refused.action == auto_repair.SKIPPED and refused.reason == auto_repair.R_REFUSED
    assert refused.code == service.E_BUNDLE_INVALID and "cannot publish" in refused.message
    assert _agent(ids["arch-lab"]).status == "active"  # untouched
    assert _claim(candidates[DEF_ARCH]).action == auto_repair.LAUNCHED

    monkeypatch.setattr(service, "install_preset",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _settle(ids["arch-lab"], pin=_stamp() - 1)
    errored = _claim(candidates[LAB_ARCH])
    assert errored.reason == auto_repair.R_ERROR and "boom" in errored.message

    # the default architect's claim above left it deploying: the stale candidate is re-judged
    monkeypatch.setattr(service, "install_preset", real)
    moved = _claim(candidates[DEF_ARCH])
    assert moved.reason == auto_repair.R_DEPLOYING and _job_count() == 4


def test_every_catalogue_preset_is_planned(client):
    """The plan iterates the catalogue, not a hard-coded pair."""
    _mark_ready()
    candidates, skipped = _plan()
    assert not candidates and {k for (_, k) in skipped} == set(PRESETS)
    assert all(o.reason == auto_repair.R_NOT_INSTALLED for o in skipped.values())


# ---------------------------------------------------------------------------
# N17-obs259: a failed re-publish is neither masked as current nor auto-retried
# ---------------------------------------------------------------------------


def test_a_failed_auto_repair_stays_visible_and_is_not_retried(client):
    """rev-r24 repro: the repair job fails after `_record_catalog` pinned the NEW stamp on
    its deployment and the live version survived (agent `active`, N10v3-bug7). Before the
    fix the next sweep read that pin → `skipped: current` and the status read said
    `snapshot_behind: false` while the Runtime served the OLD snapshot."""
    _mark_ready()
    agent_id = _install(client, ARCHITECT.key)
    current = _stamp()
    _settle(agent_id, pin=current - 1)
    launched = _plan_and_claim_all()[DEF_ARCH]
    assert launched.action == auto_repair.LAUNCHED
    # the job fails after the generate stage pinned the current stamp; the live version stays
    _settle(agent_id, "active", pin=current, job_failed=True)
    st = _status(client)[ARCHITECT.key]
    assert st["status"] == "active" and st["last_deploy_failed"] is True
    assert st["last_job"]["status"] == "failed" and st["last_job"]["trigger"] == "auto"
    # the SERVING snapshot is still the old one: the verdict reads the newest non-failed pin
    assert st["catalog_version"]["installed"] == current - 1
    assert st["snapshot_behind"] is True
    db = SessionLocal()
    try:
        assert service.catalog_behind(db, db.get(Agent, agent_id)) == (current - 1, current)
        assert [w["detail"]["agent_id"] for w in service.catalog_snapshot_warnings(db)] == [
            agent_id]
    finally:
        db.close()
    # the sweep never retries a failed re-publish: pre-filtered, with the failure attached
    before = _job_count()
    candidates, skipped = _plan()
    assert DEF_ARCH not in candidates
    assert skipped[DEF_ARCH].reason == auto_repair.R_FAILED
    assert skipped[DEF_ARCH].job_id == launched.job_id
    assert "package stage failed" in skipped[DEF_ARCH].message
    assert _job_count() == before
    # the administrator's own 修复快照 (bodiless install) still re-publishes it
    res = client.post(f"/api/system-agents/{ARCHITECT.key}/install", json={})
    assert res.status_code == 202 and res.json()["changed"] is True
    assert _job(res.json()["job_id"]).payload.get("repair_trigger") is None


def test_a_repair_that_failed_before_generate_leaves_the_serving_pin_in_place(client):
    """A failure before the generate stage pins nothing on the failed deployment; the
    serving pin is still the previous success's, so the preset stays `behind` (before the
    fix `catalog_behind` read the None pin → never behind again)."""
    _mark_ready()
    agent_id = _install(client, ARCHITECT.key)
    current = _stamp()
    _settle(agent_id, pin=current - 1)
    _plan_and_claim_all()
    _settle(agent_id, "active", job_failed=True)  # no pin: failed before generate
    st = _status(client)[ARCHITECT.key]
    assert st["catalog_version"]["installed"] == current - 1 and st["snapshot_behind"] is True
    assert _plan()[1][DEF_ARCH].reason == auto_repair.R_FAILED


def test_a_failed_first_install_has_no_serving_pin(client):
    _mark_ready()
    agent_id = _install(client, ARCHITECT.key)
    _settle(agent_id, "failed", pin=_stamp())
    st = _status(client)[ARCHITECT.key]
    assert st["status"] == "failed"
    assert st["catalog_version"]["installed"] is None and st["snapshot_behind"] is False
    assert _plan()[1][DEF_ARCH].reason == auto_repair.R_FAILED


# ---------------------------------------------------------------------------
# the serial claim-and-launch queue (N17-obs260: lazy claims)
# ---------------------------------------------------------------------------


def test_queue_claims_lazily_and_launches_one_job_at_a_time(client):
    ids = _two_workspaces_three_installed(client)
    launcher = FakeLauncher()
    sched = make_scheduler(launcher)
    sched.schedule(0, "test")
    before = _job_count()

    outcomes = sched.run_due()

    jobs = auto_repair.launched_job_ids(outcomes)
    assert len(jobs) == 2 and _job_count() == before + 2
    # claim → launch → join (cap 30 min) → claim → launch → join: never two live at once
    assert launcher.log == [
        ("launch", jobs[0]), ("join", jobs[0], auto_repair.JOB_CAP_S),
        ("launch", jobs[1]), ("join", jobs[1], auto_repair.JOB_CAP_S),
    ]
    assert list(sched.launches) == jobs and sched.sweeps == 1
    assert sched.pending()["queued"] == [] and sched.pending()["running"] is None
    # the fake workers settled their jobs: both presets are active again on the new pin
    assert {_agent(ids[k]).status for k in ("arch-default", "arch-lab")} == {"active"}
    assert _nonterminal_jobs() == 0
    by_key = {(o.workspace_id, o.preset_key): o for o in outcomes}
    assert by_key[DEF_EVAL].reason == auto_repair.R_CURRENT
    assert by_key[LAB_EVAL].reason == auto_repair.R_NOT_INSTALLED
    assert sched.history[-1]["launched"] == jobs
    # the repaired presets are current now: a second sweep claims nothing
    sched.schedule(0, "again")
    again = sched.run_due()
    assert auto_repair.launched_job_ids(again) == [] and _job_count() == before + 2
    assert {o.reason for o in again} == {auto_repair.R_CURRENT, auto_repair.R_NOT_INSTALLED}


def test_at_most_one_auto_job_is_nonterminal_and_a_restart_resumes_only_it(client, monkeypatch):
    """rev-r24 repro (N17-obs260): with eager claims, a host update mid-drain found N queued
    jobs and `resume_pending_jobs` started them all at once. Lazy claims: the second
    candidate is not claimed until the first job finished — the process dies during the
    first job, the restart resumes exactly that job, the next startup sweep repairs the rest."""
    ids = _two_workspaces_three_installed(client)
    before = _job_count()
    sched = make_scheduler()

    def die_mid_job(job_id):
        assert _nonterminal_jobs() == 1  # exactly the job being run
        sched.stop(timeout=0)  # the process is going down

    launcher = FakeLauncher(settle=False, on_join=die_mid_job)
    sched._launch = launcher
    sched.schedule(0, "startup")
    outcomes = sched.run_due()
    first = auto_repair.launched_job_ids(outcomes)
    assert len(first) == 1 and _job_count() == before + 1
    assert _nonterminal_jobs() == 1  # the second candidate was never claimed
    assert set(sched.pending()["queued"]) == {DEF_EVAL, LAB_ARCH}  # unclaimed, still queued
    deploying = [k for k in ("arch-default", "arch-lab") if _agent(ids[k]).status == "deploying"]
    assert len(deploying) == 1

    # the restart: `resume_pending_jobs` resumes the one job, not N
    resumed: list[str] = []
    monkeypatch.setattr(pipeline_module, "start_deploy_async",
                        lambda jid: resumed.append(jid) or threading.Thread())
    assert pipeline_module.resume_pending_jobs() == first
    assert resumed == first
    # the resumed job finishes; the next startup sweep plans the remaining candidate
    FakeWorker(first[0], []).join()
    fresh = make_scheduler(FakeLauncher())
    fresh.schedule(0, "startup")
    second = auto_repair.launched_job_ids(fresh.run_due())
    assert len(second) == 1 and second != first and _job_count() == before + 2
    assert {_agent(ids[k]).status for k in ("arch-default", "arch-lab")} == {"active"}


def test_queue_moves_on_after_the_job_cap(client, caplog):
    _two_workspaces_three_installed(client)
    launcher = FakeLauncher(alive_after_join=True)  # the worker outlived the cap
    sched = make_scheduler(launcher, job_cap_s=7)
    sched.schedule(0, "test")
    with caplog.at_level("WARNING", logger="launchpad.system_agents.auto_repair"):
        outcomes = sched.run_due()
    jobs = auto_repair.launched_job_ids(outcomes)
    assert [e for e in launcher.log if e[0] == "join"] == [("join", j, 7) for j in jobs]
    assert launcher.launched == jobs  # the second was still claimed and launched
    assert sum("no longer waited for" in r.message for r in caplog.records) == 2


def test_queue_survives_a_launcher_failure(client):
    _two_workspaces_three_installed(client)
    calls: list[str] = []

    def launch(job_id):
        calls.append(job_id)
        if len(calls) == 1:
            raise RuntimeError("thread start failed")
        return SimpleNamespace(join=lambda timeout=None: None, is_alive=lambda: False)

    sched = make_scheduler(launch)
    sched.schedule(0, "test")
    outcomes = sched.run_due()
    assert len(calls) == 2  # both candidates were claimed and their launch attempted
    errored = [o for o in outcomes if o.reason == auto_repair.R_ERROR]
    assert len(errored) == 1 and errored[0].message == "launch failed"
    assert sched.pending()["queued"] == []


def test_enqueue_deduplicates_and_run_due_without_a_deadline_only_drains(client):
    _two_workspaces_three_installed(client)
    launcher = FakeLauncher()
    sched = make_scheduler(launcher)
    candidates, _ = _plan()
    sched.enqueue(list(candidates.values()) * 2)  # a repeated enqueue is one claim
    assert sched.pending()["queued"] == list(candidates)
    outcomes = sched.run_due()  # no sweep was due, the queue was drained
    assert sched.sweeps == 0 and len(launcher.launched) == 2
    assert len(outcomes) == 3 and _nonterminal_jobs() == 0
    assert sched.run_due() is None  # nothing due, nothing queued


# ---------------------------------------------------------------------------
# triggers: startup flag, debounced journal listener
# ---------------------------------------------------------------------------


def _settings(flag: bool, **kw):
    return SimpleNamespace(system_presets_auto_repair=flag,
                           system_presets_auto_repair_start_delay_s=kw.get("start", 60),
                           system_presets_auto_repair_debounce_s=kw.get("debounce", 180))


def test_startup_schedule_honours_the_flag():
    clock = FakeClock(500.0)
    sched = make_scheduler(clock=clock)
    assert auto_repair.schedule_startup(_settings(False)) is None
    assert sched.pending()["due_in_s"] is None
    assert auto_repair.schedule_startup(_settings(True)) == 560.0
    assert sched.pending() == {"due_in_s": 60.0, "reasons": ["startup"], "queued": [],
                               "running": None, "thread_alive": False}
    # not due yet → nothing runs; due → one sweep
    assert sched.run_due(now=559.9) is None
    assert sched.run_due(now=560.0) is not None and sched.sweeps == 1
    assert sched.pending()["due_in_s"] is None


def test_startup_trigger_is_a_no_op_without_a_scheduler():
    assert auto_repair.scheduler() is None
    assert auto_repair.schedule_startup(_settings(True)) is None


def test_debounce_coalesces_three_journal_writes_into_one_sweep(client, monkeypatch):
    """Three catalog-shaping journal rows inside the settle window move ONE deadline (to
    180 s after the LAST write); the sweep runs once, after it."""
    monkeypatch.setattr(auto_repair, "get_settings", lambda: _settings(True, debounce=180))
    clock = FakeClock(1000.0)
    sched = make_scheduler(clock=clock)
    model_registry.add_catalog_change_listener(auto_repair.on_catalog_change)
    db = SessionLocal()
    try:
        # the SAME writes the Model Providers page issues; none of them is awaited here
        model_registry._journal(db, "provider.create", "admin", "p1", None, {"name": "a"},
                                scope="workspace")
        clock.t = 1010.0
        model_registry._journal(db, "model.create", "admin", "m1", None, {"model_id": "x"},
                                scope="workspace")
        clock.t = 1020.0
        model_registry._journal(db, "provider.delete", "admin", "p1", {"name": "a"}, None,
                                scope="workspace")
        db.commit()
    finally:
        db.close()
    pending = sched.pending()
    assert pending["due_in_s"] == 180.0  # 1020 + 180 − 1020: the LAST write set it
    assert pending["reasons"] == ["catalog change: provider.create",
                                  "catalog change: model.create",
                                  "catalog change: provider.delete"]
    assert sched.run_due(now=1199.0) is None
    outcomes = sched.run_due(now=1200.0)
    assert outcomes is not None and sched.sweeps == 1
    assert sched.history[-1]["reasons"] == pending["reasons"]
    assert sched.run_due(now=1500.0) is None  # nothing re-armed


def test_a_never_pausing_burst_is_still_swept_after_the_max_wait():
    clock = FakeClock(0.0)
    sched = make_scheduler(clock=clock, plan_fn=lambda: ([], []))
    deadline = None
    for i in range(20):  # a write every 100 s for ~33 min, never a 180 s pause
        clock.t = i * 100.0
        deadline = sched.schedule(180, f"write {i}")
        assert deadline <= auto_repair.DEBOUNCE_MAX_WAIT_S
    assert deadline == auto_repair.DEBOUNCE_MAX_WAIT_S  # 15 min after the FIRST write
    assert sched.run_due(now=899.0) is None
    assert sched.run_due(now=900.0) == [] and sched.sweeps == 1
    # the cap re-arms from the next burst's first write
    clock.t = 5000.0
    assert sched.schedule(180, "later") == 5180.0


def test_personal_scope_and_audit_only_rows_schedule_nothing_but_model_role_writes_do(
    monkeypatch,
):
    monkeypatch.setattr(auto_repair, "get_settings", lambda: _settings(True))
    sched = make_scheduler()
    model_registry.add_catalog_change_listener(auto_repair.on_catalog_change)
    db = SessionLocal()
    try:
        model_registry._journal(db, "provider.create", "alice", "p9", None, {"name": "mine"},
                                scope="personal")
        model_registry._journal(db, "provider.probe", "admin", "p1", None, {"state": "ok"})
        model_registry._journal(db, "provider.key", "admin", "p1", None, {"last4": "1234"})
        model_registry._journal(db, "provider.credential", "admin", "p1", None, {"v": 2})
        db.commit()
        assert sched.pending()["due_in_s"] is None
        # rev-r24 #3: a model-role write is audit-only for `stamp()` yet an installed preset
        # renders the role's default (N17-obs202) — it arms a sweep through its own predicate
        stamp_before = model_registry.stamp(db)
        model_registry._journal(db, "model_role.set", "admin", None, None, {"judge": "m"})
        db.commit()
        assert model_registry.stamp(db) == stamp_before  # `stamp()` untouched
        assert sched.pending()["reasons"] == ["catalog change: model_role.set"]
        # a scope flip to the shared catalog IS a catalog change
        model_registry._journal(db, "provider.update", "admin", "p9", {"scope": "personal"},
                                {"scope": "workspace"}, scope="workspace")
        db.commit()
    finally:
        db.close()
    assert sched.pending()["reasons"] == ["catalog change: model_role.set",
                                          "catalog change: provider.update"]
    assert model_registry.arms_auto_repair("model_role.reset", None) is True
    assert model_registry.arms_auto_repair("model_role.set", "personal") is False
    assert model_registry.moves_catalog_version("model_role.set", None) is False


def test_journal_listener_honours_the_flag_and_is_fail_soft(monkeypatch):
    monkeypatch.setattr(auto_repair, "get_settings", lambda: _settings(False))
    sched = make_scheduler()
    model_registry.add_catalog_change_listener(auto_repair.on_catalog_change)

    def broken(row):
        raise RuntimeError("listener bug")

    model_registry.add_catalog_change_listener(broken)
    db = SessionLocal()
    try:
        row = model_registry._journal(db, "provider.create", "admin", "p1", None, {"n": 1},
                                      scope="workspace")
        db.commit()
        assert row.id  # the write itself succeeded despite the broken listener
    finally:
        db.close()
    assert sched.pending()["due_in_s"] is None  # flag off → nothing armed


def test_create_app_installs_the_scheduler_and_the_listener_but_arms_nothing_under_the_flag():
    """The suite runs with `LAUNCHPAD_SYSTEM_PRESETS_AUTO_REPAIR=0`: the scheduler and the
    registry listener exist but neither trigger arms a deadline, so no thread ever starts
    behind a test."""
    create_app(resume_jobs=True)
    sched = auto_repair.scheduler()
    assert isinstance(sched, auto_repair.AutoRepairScheduler)
    assert auto_repair.on_catalog_change in model_registry._catalog_change_listeners
    assert sched.pending() == {"due_in_s": None, "reasons": [], "queued": [],
                               "running": None, "thread_alive": False}
    assert get_settings().system_presets_auto_repair is False


def test_the_thread_runs_a_due_sweep_and_stops():
    """The one real-thread test: a zero deadline, a plan fake that signals an Event, no
    sleep longer than the Event wait."""
    ran = threading.Event()

    def fake_plan():
        ran.set()
        return [], []

    sched = auto_repair.AutoRepairScheduler(plan_fn=fake_plan, launch=FakeLauncher(),
                                            start_thread=True)
    try:
        sched.schedule(0, "test")
        assert ran.wait(5), "the scheduler thread never ran the due sweep"
        assert sched.pending()["thread_alive"] is True
    finally:
        sched.stop(timeout=5)
    assert sched.pending()["thread_alive"] is False and sched.sweeps == 1


# ---------------------------------------------------------------------------
# bootstrap step 10 × the sweep: one winner
# ---------------------------------------------------------------------------


def test_bootstrap_step_10_and_the_sweep_claim_one_job(client):
    """`ensure_eval_worker` (bootstrap step 10) and the sweep both call `install_preset` on
    the default workspace's eval worker; the durable claim in `_repair` admits exactly one
    job in either order — the loser adopts the winner's."""
    _mark_ready()
    agent_id = _install(client, EVAL_WORKER.key)
    _settle(agent_id, pin=_stamp() - 1)
    before = _job_count()
    candidate = _plan()[0][DEF_EVAL]

    # order 1: bootstrap claims first (its job runs synchronously in the CLI process)
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        boot = service.install_preset(db, row, EVAL_WORKER, None)
        db.commit()
        assert boot.changed and boot.job is not None
        boot_job = boot.job.id
    finally:
        db.close()
    skipped = _claim(candidate)  # the candidate from BEFORE bootstrap's claim
    assert skipped.action == auto_repair.SKIPPED and skipped.reason == auto_repair.R_DEPLOYING
    assert skipped.job_id == boot_job and _job_count() == before + 1
    assert _job(boot_job).payload.get("repair_trigger") is None  # bootstrap's job, not auto

    # order 2: the sweep claims first; bootstrap then adopts the sweep's in-flight job
    _settle(agent_id, pin=_stamp() - 1)
    sweep_job = _claim(_plan()[0][DEF_EVAL]).job_id
    assert sweep_job and _job_count() == before + 2
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        boot = service.install_preset(db, row, EVAL_WORKER, None)
        db.commit()
        assert boot.changed is False and boot.job.id == sweep_job
    finally:
        db.close()
    assert _job_count() == before + 2
    assert _job(sweep_job).payload["repair_trigger"] == "auto"


# ---------------------------------------------------------------------------
# N17-obs268 (deploy-ops-34): every sweep logs one summary + one line per skipped preset
# ---------------------------------------------------------------------------

LOGGER = "launchpad.system_agents.auto_repair"


def _sweep_lines(caplog) -> tuple[list[str], list[str]]:
    msgs = [r for r in caplog.records if r.name == LOGGER and r.levelname == "INFO"]
    summary = [r.getMessage() for r in msgs if r.getMessage().startswith("auto-repair sweep:")]
    skips = [r.getMessage() for r in msgs if r.getMessage().startswith("auto-repair skip:")]
    return summary, skips


def test_every_sweep_logs_one_summary_and_one_line_per_skipped_preset(client, caplog):
    """Live (deploy-ops-34): the sweep's skips (current / failed / in flight) lived only in
    the in-process `history`, so the log could not say why a preset was not repaired."""
    _two_workspaces_three_installed(client)
    sched = make_scheduler(FakeLauncher())
    sched.schedule(0, "test")
    with caplog.at_level("INFO", logger=LOGGER):
        outcomes = sched.run_due()
    summary, skips = _sweep_lines(caplog)
    jobs = auto_repair.launched_job_ids(outcomes)
    not_installed = 2 * len(PRESETS) - 3  # two workspaces, three installed presets
    assert summary == [
        f"auto-repair sweep: planned=3 launched={len(jobs)} skipped={{current: 1, failed: 0, "
        f"in_flight: 0, refused: 0, not_installed: {not_installed}}}"]
    assert len(jobs) == 2
    assert f"auto-repair skip: workspace={DEFAULT_WORKSPACE_ID} preset={EVAL_WORKER.key} " \
           "reason=current" in skips
    assert f"auto-repair skip: workspace={LAB} preset={EVAL_WORKER.key} " \
           "reason=not_installed" in skips
    assert len(skips) == 1 + not_installed  # one per skipped preset, nothing for launches
    caplog.clear()
    with caplog.at_level("INFO", logger=LOGGER):
        assert sched.run_due() is None  # no sweep due: nothing logged
    assert _sweep_lines(caplog) == ([], [])


def test_the_skip_lines_carry_the_job_code_and_message(caplog):
    """Every reason, through fake plan / claim functions: a failed row names its job, an
    in-flight claim the winner's job, a refusal its code; a reason outside the five summary
    keys is appended to the summary only when it occurred."""
    skip = auto_repair._skip
    planned_skips = [
        skip("ws1", "arch", "a1", auto_repair.R_FAILED, job_id="j-failed",
             message="deploy failed: boom"),
        skip("ws1", "eval", "a2", auto_repair.R_DEPLOYING, job_id="j-running"),
        skip("ws2", "arch", None, auto_repair.R_NOT_INSTALLED),
    ]
    candidates = [Candidate("ws2", "eval", "a3"), Candidate("ws3", "arch", "a4")]
    claimed = {
        ("ws2", "eval"): skip("ws2", "eval", "a3", auto_repair.R_IN_FLIGHT, job_id="j-card"),
        ("ws3", "arch"): skip("ws3", "arch", "a4", auto_repair.R_REFUSED,
                              code="system_agent.workspace_not_ready", message="not ready"),
    }
    sched = auto_repair.AutoRepairScheduler(
        plan_fn=lambda: (candidates, planned_skips), claim_fn=lambda c: claimed[c.key],
        launch=lambda jid: pytest.fail("nothing launches"), clock=FakeClock(),
        start_thread=False)
    sched.schedule(0, "test")
    with caplog.at_level("INFO", logger=LOGGER):
        sched.run_due()
    summary, skips = _sweep_lines(caplog)
    assert summary == ["auto-repair sweep: planned=2 launched=0 skipped={current: 0, failed: 1, "
                       "in_flight: 1, refused: 1, not_installed: 1, deploying: 1}"]
    assert skips == [
        "auto-repair skip: workspace=ws1 preset=arch reason=failed job=j-failed "
        "message=deploy failed: boom",
        "auto-repair skip: workspace=ws1 preset=eval reason=deploying job=j-running",
        "auto-repair skip: workspace=ws2 preset=arch reason=not_installed",
        "auto-repair skip: workspace=ws2 preset=eval reason=in_flight job=j-card",
        "auto-repair skip: workspace=ws3 preset=arch reason=refused "
        "code=system_agent.workspace_not_ready message=not ready",
    ]


def test_a_broken_plan_still_logs_an_empty_summary(caplog):
    def broken():
        raise RuntimeError("ledger gone")

    sched = auto_repair.AutoRepairScheduler(
        plan_fn=broken, claim_fn=lambda c: pytest.fail("no candidates"),
        launch=lambda jid: pytest.fail("nothing launches"), clock=FakeClock(),
        start_thread=False)
    sched.schedule(0, "test")
    with caplog.at_level("INFO", logger=LOGGER):
        assert sched.run_due() == []
    summary, skips = _sweep_lines(caplog)
    assert summary == ["auto-repair sweep: planned=0 launched=0 skipped={current: 0, failed: 0, "
                       "in_flight: 0, refused: 0, not_installed: 0}"]
    assert skips == []
