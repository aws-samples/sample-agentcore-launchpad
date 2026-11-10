# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E15 — Skill Lab: task set → eval / train job → cancel / resume → publish / delete across
members × a missing model credential × a backend restart, as a model (round 16 channel A,
Hypothesis ``RuleBasedStateMachine``). Hermetic: the vendored CLIs are the stub scripts of
``test_skill_lab_jobs`` / ``test_skill_lab_train`` (real subprocesses, real job queue,
real on-disk artifacts under a temp ``JOBS_DIR``), the Registry seam is recorded.

Invariants and the contract clause each checks:

* **Ownership gate** — users-and-ownership.md §8: Skill Lab jobs — "``POST …/jobs/{id}/
  cancel``, ``…/resume``, ``…/publish`` (mutates the Registry record), ``DELETE …/jobs/{id}``"
  are creator-or-admin, "anyone else answers 403 ``skill_lab.forbidden`` {job_id, action}
  before any side effect"; every read stays open; a NULL creator is an administrator's.
* **Missing model credential → 503 at submit, no row** — CLAUDE.md (N13-obs20, E15 NC-13):
  "a hub without ``AWS_BEARER_TOKEN_BEDROCK`` refuses every Skill Lab job … at submit
  (``aws_clients.require_global_bedrock_bearer``)"; ``runner.require_model_credential``:
  "probed at SUBMIT, before any row … 503 ``skill_lab.model_credential_missing`` instead of a
  201 followed by ``failed``".
* **Lifecycle** — architecture.md §Skill Lab: "resume-from-checkpoint for interrupted runs"
  (training only; "an evaluation is cheap enough to re-submit"); "publish (minor version
  bump via the record-update path; the record settles into DRAFT — surfaced with an
  optional re-approve)" for a succeeded training job only; a job is deleted only once it
  is finished; a restart marks every non-terminal job ``interrupted`` honestly rather than
  resuming it (``jobs.sweep_interrupted_jobs``, testing.md §1.3 restart sweeps).
* **No orphan artifacts** — every job row has its directory (the inputs snapshot + log)
  and every directory under ``JOBS_DIR`` belongs to a row; a deleted job's directory is
  gone (``runner.remove_job_dir``).
"""

from __future__ import annotations

import os
import sys
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.skill_lab import artifacts, runner
from app.skill_lab import jobs as jobs_svc
from app.skill_lab import tasksets as taskset_svc
from app.skill_lab.models import SkillLabJob
from tests.conftest import set_default_resources
from tests.model.worlds import reset_skill_lab_world
from tests.property.ledger_invariants import scan_ledger
from tests.test_skill_lab_jobs import STUB_CLI, WORKER_RESOURCES
from tests.test_skill_lab_train import STUB_TRAIN
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
SCALE = 10 if SOAK else 1
TERMINAL = set(jobs_svc.TERMINAL_STATUSES)
STATUSES = TERMINAL | {"queued", "running"}
# where a job goes on its own, between any two reads (N17-obs396) — the queue picks it up,
# its subprocess ends; a terminal job stays put. ``cancelled`` / ``interrupted`` are never a
# job's own doing (a permitted cancel, a backend restart).
NATURAL_SUCCESSORS: dict[str, set[str]] = {
    "queued": {"running", "succeeded", "failed"},
    "running": {"succeeded", "failed"},
}
JOBS = "/api/skill-lab/jobs"


class Registry:
    """The Registry seam ``publish`` writes through (``registry_console``): records every
    ``update_record`` payload; the record is DRAFT and every update bumps the minor version
    the way the real backend does. The record is ``owner``'s (ada's account in the
    fixture): N17-obs263 — only its creator or an administrator may publish onto it."""

    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.version = 0
        self.owner: str | None = None  # users.id of the record's creator

    def console_get(self, ws, record_id):
        return {"recordId": record_id, "status": "DRAFT", "createdBy": self.owner,
                "recordVersion": f"1.{self.version}.0-skill"}

    def update_record(self, record_id, ws, **kwargs):
        self.updates.append((record_id, kwargs))
        self.version += 1
        return {"recordId": record_id, "status": "DRAFT",
                "recordVersion": f"1.{self.version}.0-skill"}


@pytest.fixture
def lab(tmp_path, monkeypatch, app, admin, ada, bob):
    """An auth-enabled console (admin + two members) over the stub CLIs."""
    from app.services import registry_console

    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bearer")
    (tmp_path / "stub_cli.py").write_text(STUB_CLI)
    (tmp_path / "stub_train.py").write_text(STUB_TRAIN)
    fake_settings = get_settings().model_copy(update={
        "skill_lab_python": sys.executable,
        "skill_lab_target_model_id": "global.moonshotai.kimi-k3",
        "skill_lab_judge_model_id": "global.zai.glm-5.3",
    })
    monkeypatch.setattr(runner, "EVAL_SCRIPT", tmp_path / "stub_cli.py")
    monkeypatch.setattr(runner, "TRAIN_SCRIPT", tmp_path / "stub_train.py")
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(taskset_svc, "get_settings",
                        lambda: SimpleNamespace(skill_lab_python=sys.executable))
    monkeypatch.setattr(runner, "sweep_exec_jobs_prefix", lambda ws, log: None)

    def fake_materialize(workspace, record_id, dest_parent, log):
        skill_dir = dest_parent / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo skill\n\nDo the thing.")
        (skill_dir / "references").mkdir()
        (skill_dir / "references" / "note.md").write_text("support file")
        return skill_dir, {"kind": "registry", "record_id": record_id, "name": "demo-skill"}

    monkeypatch.setattr(runner, "materialize_registry_skill", fake_materialize)
    registry = Registry()
    registry.owner = ada[0]
    monkeypatch.setattr(registry_console, "console_get", registry.console_get)
    monkeypatch.setattr(registry_console, "update_record", registry.update_record)
    set_default_resources(WORKER_RESOURCES)
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    return {
        "clients": {"admin": admin, "ada": ada_client, "bob": bob_client},
        "ids": {"ada": ada_id, "bob": bob_id},
        "registry": registry,
        "monkeypatch": monkeypatch,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rows() -> dict[str, SkillLabJob]:
    db = SessionLocal()
    try:
        rows = db.query(SkillLabJob).all()
        for row in rows:
            db.expunge(row)
        return {row.id: row for row in rows}
    finally:
        db.close()


def _dirs() -> set[str]:
    root = artifacts.JOBS_DIR
    return {p.name for p in root.iterdir() if p.is_dir()} if root.is_dir() else set()


def _wait(client, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"{JOBS}/{job_id}").json()
        if job.get("status") in TERMINAL or time.monotonic() >= deadline:
            return job
        time.sleep(0.05)


def _taskset_body(mode: str, *, sleepy: bool, tag: str) -> dict[str, Any]:
    def tasks(prefix, n=2):
        return [{"id": f"{prefix}_{i}", "question": "SLEEP:1.2" if sleepy and i == 0 else f"q {i}",
                 "rubric": "PASS always"} for i in range(n)]

    if mode == "single":
        return {"name": f"ts-{tag}", "mode": "single", "tasks_by_split": {"tasks": tasks("t")}}
    return {"name": f"ts-{tag}", "mode": "split",
            "tasks_by_split": {"train": tasks("tr"), "val": tasks("va")}}


# ---------------------------------------------------------------------------
# the machine
# ---------------------------------------------------------------------------


class SkillLabMachine(RuleBasedStateMachine):
    def __init__(self, lab):
        super().__init__()
        reset_skill_lab_world()
        lab["monkeypatch"].setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bearer")
        self.clients = lab["clients"]
        self.ids = lab["ids"]
        self.registry: Registry = lab["registry"]
        self.mp = lab["monkeypatch"]
        self.tasksets: dict[str, tuple[str, str]] = {}  # id → (mode, sleepy)
        self.jobs: dict[str, dict[str, Any]] = {}  # id → model
        self.credential = True
        self.n = 0

    # -- helpers ---------------------------------------------------------------
    def may_mutate(self, caller: str, job: dict[str, Any]) -> bool:
        return caller == "admin" or job["creator"] == caller

    def refresh(self, job_id: str) -> dict:
        job = self.clients["admin"].get(f"{JOBS}/{job_id}").json()
        self.jobs[job_id]["status"] = job["status"]
        return job

    def assert_progressed(self, job_id: str, before: str) -> str:
        """The job's status after a request that must have had NO effect on it (N17-obs396):
        a job runs on its own — a subprocess moves queued → running → succeeded / failed
        between two reads whatever the request was, and under xdist load that happens
        between the model's read and its re-read. So a side-effect-free request leaves the
        row in ``before`` or in a status the job reaches by ITSELF, never one only the
        request could cause: ``cancelled`` (only an earlier, permitted cancel lets a job end
        cancelled) or a re-``queued`` resume. The model takes the observed row."""
        after = self.refresh(job_id)["status"]
        allowed = {before} | NATURAL_SUCCESSORS.get(before, set())
        if self.jobs[job_id].get("cancel_requested") and before not in TERMINAL:
            allowed.add("cancelled")
        assert after in allowed, f"{job_id}: {before} → {after} without a permitted request"
        return after

    def settle(self, job_id: str) -> dict:
        job = _wait(self.clients["admin"], job_id)
        assert job["status"] in TERMINAL, job
        self.jobs[job_id]["status"] = job["status"]
        return job

    def pick(self, data, predicate) -> str | None:
        ids = [j for j, m in self.jobs.items() if predicate(m)]
        return data.draw(st.sampled_from(sorted(ids))) if ids else None

    # -- rules -------------------------------------------------------------------
    @rule(creator=st.sampled_from(["ada", "bob"]), mode=st.sampled_from(["single", "split"]),
          sleepy=st.booleans())
    def create_taskset(self, creator, mode, sleepy):
        self.n += 1
        res = self.clients[creator].post("/api/skill-lab/tasksets",
                                         json=_taskset_body(mode, sleepy=sleepy, tag=str(self.n)))
        assert res.status_code == 201, res.text
        self.tasksets[res.json()["id"]] = (mode, sleepy)

    @rule(on=st.booleans())
    def credential(self, on):
        if on:
            self.mp.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bearer")
        else:
            self.mp.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
        self.credential = on

    @precondition(lambda self: self.tasksets)
    @rule(caller=st.sampled_from(["ada", "bob", "admin"]), kind=st.sampled_from(["eval", "train"]),
          data=st.data())
    def submit(self, caller, kind, data):
        wanted = "single" if kind == "eval" else "split"
        candidates = sorted(t for t, (mode, _s) in self.tasksets.items() if mode == wanted)
        if not candidates:
            return
        taskset = data.draw(st.sampled_from(candidates))
        rows_before, dirs_before = set(_rows()), _dirs()
        body = {"type": kind, "skill_source": {"kind": "registry", "record_id": "rec-1"},
                "taskset_id": taskset}
        if kind == "train":
            body["params"] = {"epochs": 2}
        res = self.clients[caller].post(JOBS, json=body)
        note(f"{caller} submit {kind} credential={self.credential} → {res.status_code}")
        if not self.credential:
            assert res.status_code == 503, res.text
            assert res.json()["code"] == "skill_lab.model_credential_missing"
            assert set(_rows()) == rows_before and _dirs() == dirs_before, "a row or dir leaked"
            return
        assert res.status_code == 201, res.text
        job = res.json()
        assert job["status"] == "queued"
        self.jobs[job["id"]] = {"type": kind, "creator": caller, "status": "queued",
                                "sleepy": self.tasksets[taskset][1]}

    @rule(data=st.data())
    def wait_terminal(self, data):
        # no precondition on job status: a subprocess may finish between two runs of the
        # same program, and Hypothesis requires the enabled rule set to be replayable
        job_id = self.pick(data, lambda m: True)
        if job_id is None or self.jobs[job_id]["status"] in TERMINAL:
            return
        job = self.settle(job_id)
        if self.jobs[job_id].get("cancel_requested"):
            assert job["status"] == "cancelled", job
        elif self.jobs[job_id]["type"] == "train":
            assert job["status"] == "succeeded", (job["status"], job.get("error"))
        else:
            assert job["status"] == "succeeded", (job["status"], job.get("error"))

    @precondition(lambda self: self.jobs)
    @rule(caller=st.sampled_from(["ada", "bob", "admin"]), data=st.data())
    def cancel(self, caller, data):
        job_id = self.pick(data, lambda m: True)
        before = self.refresh(job_id)
        res = self.clients[caller].post(f"{JOBS}/{job_id}/cancel")
        note(f"{caller} cancel {job_id} ({before['status']}) → {res.status_code}")
        if not self.may_mutate(caller, self.jobs[job_id]):
            assert res.status_code == 403 and res.json()["code"] == "skill_lab.forbidden", res.text
            assert res.json()["detail"] == {**res.json()["detail"], "job_id": job_id,
                                            "action": "cancel"}
            self.assert_progressed(job_id, before["status"])
            return
        if before["status"] in TERMINAL:
            # the job may have finished between the read and the request
            assert res.status_code in (200, 400), res.text
            if res.status_code == 400:
                assert res.json()["code"] == "skill_lab.job_not_cancellable"
            return
        if res.status_code == 400:  # finished meanwhile
            assert res.json()["code"] == "skill_lab.job_not_cancellable"
            assert self.refresh(job_id)["status"] in TERMINAL
            return
        assert res.status_code == 200, res.text
        after = res.json()
        if after["status"] in NATURAL_SUCCESSORS.get(before["status"], set()) & TERMINAL:
            # N17-obs396: the subprocess ended between the server's read and its commit —
            # the cancel lost the race to a job that finished by itself
            self.jobs[job_id]["status"] = after["status"]
            return
        self.jobs[job_id]["cancel_requested"] = True
        assert after["status"] in ("cancelled", "running", "queued"), after["status"]
        if before["status"] == "queued" and after["status"] == "queued":
            pytest.fail("a queued job answered 200 to cancel but stayed queued")
        self.jobs[job_id]["status"] = after["status"]

    @precondition(lambda self: self.jobs)
    @rule(caller=st.sampled_from(["ada", "bob", "admin"]), data=st.data())
    def resume(self, caller, data):
        job_id = self.pick(data, lambda m: True)
        before = self.refresh(job_id)
        res = self.clients[caller].post(f"{JOBS}/{job_id}/resume")
        note(f"{caller} resume {job_id} ({before['status']}, {self.jobs[job_id]['type']}) "
             f"→ {res.status_code} credential={self.credential}")
        model = self.jobs[job_id]
        if not self.may_mutate(caller, model):
            assert res.status_code == 403 and res.json()["code"] == "skill_lab.forbidden", res.text
            self.assert_progressed(job_id, before["status"])
            return
        if not self.credential:
            assert res.status_code == 503, res.text
            assert res.json()["code"] == "skill_lab.model_credential_missing"
            self.assert_progressed(job_id, before["status"])  # a 503 changed nothing
            return
        if model["type"] != "train" or before["status"] not in ("interrupted", "failed"):
            assert res.status_code == 400, res.text
            assert res.json()["code"] == "skill_lab.job_not_resumable", res.text
            return
        assert res.status_code == 200, res.text
        assert res.json()["status"] == "queued"
        model["status"] = "queued"
        model.pop("cancel_requested", None)

    @precondition(lambda self: self.jobs)
    @rule(caller=st.sampled_from(["ada", "bob", "admin"]), data=st.data())
    def publish(self, caller, data):
        job_id = self.pick(data, lambda m: True)
        before = self.refresh(job_id)
        updates = len(self.registry.updates)
        res = self.clients[caller].post(f"{JOBS}/{job_id}/publish", json={})
        note(f"{caller} publish {job_id} ({before['status']}, {self.jobs[job_id]['type']}) "
             f"→ {res.status_code}")
        model = self.jobs[job_id]
        if not self.may_mutate(caller, model):
            assert res.status_code == 403 and res.json()["code"] == "skill_lab.forbidden", res.text
            assert len(self.registry.updates) == updates, "a forbidden publish reached the Registry"
            return
        status = before["status"]
        if model["type"] == "train" and status in NATURAL_SUCCESSORS and res.status_code != 400:
            # N17-obs396: the training finished between the model's read and the request —
            # the server answered for a succeeded job; the model takes the observed row
            status = self.assert_progressed(job_id, status)
            assert status == "succeeded", (status, res.text)
        if model["type"] != "train" or status != "succeeded":
            assert res.status_code == 400, res.text
            assert res.json()["code"] == "skill_lab.publish_unsupported", res.text
            assert len(self.registry.updates) == updates
            return
        if caller != "admin" and self.ids[caller] != self.registry.owner:
            # N17-obs263: the job is the caller's, the SOURCE record is not
            assert res.status_code == 403, res.text
            assert res.json()["code"] == "registry.forbidden", res.text
            assert res.json()["detail"]["action"] == "publish", res.text
            assert len(self.registry.updates) == updates, "a forbidden publish wrote"
            return
        assert res.status_code == 200, res.text
        if model.get("published_version") == self.registry.version:
            # N16-obs44: this job already pushed exactly this content and the record is
            # still at that version → idempotent: the existing version, no Registry write
            assert res.json()["unchanged"] is True, res.text
            assert res.json()["new_version"] == f"1.{self.registry.version}.0-skill"
            assert len(self.registry.updates) == updates, "an unchanged publish wrote"
            return
        assert res.json()["unchanged"] is False, res.text
        assert len(self.registry.updates) == updates + 1
        record_id, kwargs = self.registry.updates[-1]
        assert record_id == "rec-1" and kwargs["skill_md"].endswith("IMPROVED")
        model["published"] = model.get("published", 0) + 1
        model["published_version"] = self.registry.version

    @precondition(lambda self: self.jobs)
    @rule(caller=st.sampled_from(["ada", "bob", "admin"]), data=st.data())
    def delete(self, caller, data):
        job_id = self.pick(data, lambda m: True)
        before = self.refresh(job_id)
        res = self.clients[caller].request("DELETE", f"{JOBS}/{job_id}")
        note(f"{caller} delete {job_id} ({before['status']}) → {res.status_code}")
        if not self.may_mutate(caller, self.jobs[job_id]):
            assert res.status_code == 403 and res.json()["code"] == "skill_lab.forbidden", res.text
            assert job_id in _rows() and job_id in _dirs()
            return
        if before["status"] not in TERMINAL:
            if res.status_code == 400:
                assert res.json()["code"] == "skill_lab.job_not_deletable"
                return
            # finished between the read and the request
            assert res.status_code == 200
        assert res.status_code == 200, res.text
        assert job_id not in _rows() and job_id not in _dirs()
        assert self.clients["admin"].get(f"{JOBS}/{job_id}").status_code == 404
        del self.jobs[job_id]

    def teardown(self) -> None:
        """No job subprocess or queue write outlives the example (the process-global
        ``EvalRunQueue`` would otherwise write into the next test's ledger)."""
        for job_id, model in list(self.jobs.items()):
            if model["status"] not in TERMINAL:
                self.settle(job_id)

    # -- invariants --------------------------------------------------------------
    @invariant()
    def rows_and_directories_agree(self):
        rows = _rows()
        dirs = _dirs()
        assert set(rows) == set(self.jobs), (sorted(rows), sorted(self.jobs))
        for job_id, row in rows.items():
            assert row.status in STATUSES, row.status
            assert job_id in dirs, f"job {job_id} ({row.status}) has no artifact directory"
            assert row.created_by == (None if self.jobs[job_id]["creator"] == "admin"
                                      else self.ids[self.jobs[job_id]["creator"]])
        assert dirs <= set(rows), f"orphan job directories: {sorted(dirs - set(rows))}"
        own = set(self.jobs) | set(self.tasksets)
        found = [v for v in scan_ledger(SessionLocal) if any(i in v for i in own)]
        assert found == []


def _run(lab, *, examples: int, steps: int) -> None:
    run_state_machine_as_test(
        lambda: SkillLabMachine(lab),
        settings=settings(max_examples=examples, stateful_step_count=steps, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]),
    )


def test_skill_lab_state_machine(lab):
    _run(lab, examples=3 * SCALE, steps=18 * SCALE)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
def test_soak_skill_lab_state_machine(lab):
    _run(lab, examples=20, steps=60)


# ---------------------------------------------------------------------------
# scenarios the random walk cannot reach deterministically
# ---------------------------------------------------------------------------


def _train(lab, caller: str = "ada") -> dict:
    client = lab["clients"][caller]
    ts = client.post("/api/skill-lab/tasksets",
                     json=_taskset_body("split", sleepy=False, tag=f"r{os.urandom(2).hex()}"))
    assert ts.status_code == 201, ts.text
    res = client.post(JOBS, json={"type": "train", "taskset_id": ts.json()["id"],
                                  "skill_source": {"kind": "registry", "record_id": "rec-1"},
                                  "params": {"epochs": 2}})
    assert res.status_code == 201, res.text
    return _wait(client, res.json()["id"])


def test_restart_interrupts_a_running_train_job_and_resume_continues_it(lab):
    """testing.md §1.3 / ``sweep_interrupted_jobs``: a restart marks the row ``interrupted``
    with the honest reason — never resumes it; the creator's ``resume`` then re-enqueues the
    same config and the trainer continues from its checkpoint (architecture.md: "resume-
    from-checkpoint for interrupted runs"); a second restart finds nothing to sweep."""
    job = _train(lab)
    assert job["status"] == "succeeded", job
    db = SessionLocal()
    try:  # the process died while this job was running (its subprocess is gone with it)
        row = db.get(SkillLabJob, job["id"])
        row.status, row.finished_at = "running", None
        db.commit()
    finally:
        db.close()
    create_app(resume_jobs=True)
    set_default_resources(WORKER_RESOURCES)  # init_db mirrors the test yaml onto the row
    swept = lab["clients"]["admin"].get(f"{JOBS}/{job['id']}").json()
    assert swept["status"] == "interrupted" and "backend restart" in swept["error"], swept
    assert "resume it" in swept["error"]
    # another member cannot resume it; the creator can; the resumed run is a continuation
    res = lab["clients"]["bob"].post(f"{JOBS}/{job['id']}/resume")
    assert res.status_code == 403 and res.json()["code"] == "skill_lab.forbidden"
    res = lab["clients"]["ada"].post(f"{JOBS}/{job['id']}/resume")
    assert res.status_code == 200 and res.json()["status"] == "queued", res.text
    done = _wait(lab["clients"]["ada"], job["id"])
    assert done["status"] == "succeeded", done
    diff = artifacts.skill_diff(job["id"])
    assert diff and diff["best"].endswith("RESUMED"), diff and diff["best"][-40:]
    create_app(resume_jobs=True)
    set_default_resources(WORKER_RESOURCES)
    assert lab["clients"]["admin"].get(f"{JOBS}/{job['id']}").json()["status"] == "succeeded"


def test_missing_credential_at_resume_leaves_the_row_untouched(lab):
    """N13-obs20 extended to ``resume``: the probe runs before any row changes, so an
    interrupted job stays interrupted (and its ``finished_at`` keeps the sweep's stamp)."""
    job = _train(lab)
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job["id"])
        row.status, row.error, row.finished_at = "interrupted", "restart", datetime.now(UTC)
        db.commit()
        stamp = row.finished_at
    finally:
        db.close()
    lab["monkeypatch"].delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    res = lab["clients"]["ada"].post(f"{JOBS}/{job['id']}/resume")
    assert res.status_code == 503 and res.json()["code"] == "skill_lab.model_credential_missing"
    db = SessionLocal()
    try:
        row = db.get(SkillLabJob, job["id"])
        assert (row.status, row.error) == ("interrupted", "restart")
        assert row.finished_at.replace(tzinfo=None) == stamp.replace(tzinfo=None)
    finally:
        db.close()


def test_publish_twice_is_idempotent(lab):
    """N16-obs44 (fixed in cn/r16-l): publishing the same succeeded training job twice
    pushes the Registry record ONCE — the second call answers the existing version with
    ``unchanged: true`` and writes nothing (architecture.md: 'the improved SKILL.md is
    published back onto the same record as a bumped minor version' — one version per
    content, never a no-op version)."""
    job = _train(lab)
    client = lab["clients"]["ada"]
    first = client.post(f"{JOBS}/{job['id']}/publish", json={})
    second = client.post(f"{JOBS}/{job['id']}/publish", json={})
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    payloads = [kw["skill_md"] for _rid, kw in lab["registry"].updates]
    assert len(payloads) == 1, "the second publish must not reach the Registry"
    # idempotent: the same content must not become a second record version
    assert first.json()["new_version"] == second.json()["new_version"] == "1.1.0-skill", (
        first.json()["new_version"], second.json()["new_version"])
    assert first.json()["unchanged"] is False and second.json()["unchanged"] is True
    assert second.json()["status_after"] == second.json()["status_before"]
    log = client.get(f"{JOBS}/{job['id']}/log").json()["content"]
    assert "unchanged" in log
