"""Skill Lab eval jobs: command contract, lifecycle, cancel, sweep, results.

The vendored CLI is replaced by a stub script whose behavior is driven by the
TASK CONTENT (the env is allowlisted, so control flags could not ride there):
a task question of SLEEP:<s> sleeps, FAIL:<rc> exits nonzero, SPAWNCHILD forks
a sleeping child (process-group-kill proof); anything else writes one passing
results.json row per task.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.skill_lab import artifacts, jobs, runner
from app.skill_lab import tasksets as taskset_svc
from tests.conftest import set_default_resources

STUB_CLI = '''
import argparse, json, os, pathlib, subprocess, sys, time
parser = argparse.ArgumentParser()
for flag in ("--skill","--tasks","--out_root","--target_backend","--model",
             "--optimizer_backend","--optimizer_model","--judge_mode",
             "--workers","--timeout","--limit"):
    parser.add_argument(flag)
args = parser.parse_args()
tasks = json.loads(pathlib.Path(args.tasks).read_text())
out = pathlib.Path(args.out_root); out.mkdir(parents=True, exist_ok=True)
print("[skilleval] tasks: %d from %s" % (len(tasks), args.tasks), flush=True)
rows = []
for task in tasks:
    q = task["question"]
    if q.startswith("SLEEP:"):
        time.sleep(float(q.split(":",1)[1]))
    elif q.startswith("FAIL:"):
        print("boom", flush=True); sys.exit(int(q.split(":",1)[1]))
    elif q.startswith("SPAWNCHILD"):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        (out / "child_pid.txt").write_text(str(child.pid))
        time.sleep(60)
    rows.append({"id": task["id"], "task_type": "default", "hard": 1, "soft": 1.0,
                 "score_valid": True, "duration_s": 0.1, "judge_status": "valid_pass",
                 "judge_reason": "ok", "response": "done", "artifacts": []})
print("[skilleval] judging %d responses" % len(rows), flush=True)
(out / "results.json").write_text(json.dumps(rows))
print("[skilleval] done", flush=True)
'''

WORKER_RESOURCES = {
    "skill_lab_worker_runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/rt",
    "skill_lab_worker_role_arn": "arn:aws:iam::111122223333:role/worker",
    "skill_lab_worker_image_digest": "sha256:x",
    "artifacts_bucket": "bkt",
}


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    stub = tmp_path / "stub_cli.py"
    stub.write_text(STUB_CLI)
    # A real Settings copy, not a namespace: spawn() derives the child's rlimit
    # profile from it (model_copy), so a stand-in would hide that code path.
    fake_settings = get_settings().model_copy(
        update={
            "skill_lab_python": sys.executable,
            "skill_lab_target_model_id": "us.anthropic.claude-sonnet-5",
            "skill_lab_judge_model_id": "us.anthropic.claude-sonnet-5",
        }
    )
    monkeypatch.setattr(runner, "EVAL_SCRIPT", stub)
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    # the janitor lists real S3 — never in hermetic tests
    monkeypatch.setattr(runner, "sweep_exec_jobs_prefix", lambda ws, log: None)

    def fake_materialize(workspace, record_id, dest_parent, log):
        skill_dir = dest_parent / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# demo\n")
        log("materialized stub skill")
        return skill_dir, {"kind": "registry", "record_id": record_id, "name": "demo-skill"}

    monkeypatch.setattr(runner, "materialize_registry_skill", fake_materialize)
    set_default_resources(WORKER_RESOURCES)
    return client


def _taskset(client, questions: list[str], name="ts") -> str:
    tasks = [
        {"id": f"task_{i:03d}", "question": q, "rubric": "PASS always"}
        for i, q in enumerate(questions, start=1)
    ]
    response = client.post(
        "/api/skill-lab/tasksets",
        json={"name": name, "mode": "single", "tasks_by_split": {"tasks": tasks}},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _submit(client, taskset_id, **overrides):
    body = {
        "type": "eval",
        "skill_source": {"kind": "registry", "record_id": "rec-1"},
        "taskset_id": taskset_id,
    }
    body.update(overrides)
    return client.post("/api/skill-lab/jobs", json=body)


def _wait(client, job_id, statuses=("succeeded", "failed", "cancelled"), timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/skill-lab/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not reach {statuses} in {timeout}s (last: {job})")


# ── command + env contracts ────────────────────────────────────────────────


def test_command_contract(lab, tmp_path):
    params = runner.clamp_params({"workers": 3, "limit": 5})
    command = runner.build_eval_command(
        skill_dir=tmp_path / "skill",
        tasks_file=tmp_path / "tasks.json",
        out_dir=tmp_path / "out",
        params=params,
    )
    text = " ".join(command)
    assert command[0] == sys.executable
    assert "--target_backend claude_code_exec" in text
    assert "--optimizer_backend bedrock_chat" in text
    assert "--judge_mode chat" in text
    assert "--workers 3" in text and "--limit 5" in text


def test_env_is_allowlisted(lab, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    workspace = SimpleNamespace(resources=dict(WORKER_RESOURCES), region="us-west-2")
    env = runner.build_job_env(workspace)
    assert "LAUNCHPAD_ADMIN_PASSWORD" not in env
    assert env["AWS_ACCESS_KEY_ID"] == "AKIATEST"
    assert env["SKILLOPT_EXEC_RUNNER"] == "agentcore"
    assert env["SKILLOPT_AGENTCORE_S3_PREFIX"] == "skill-lab/exec-jobs"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"  # killed jobs must not lose buffered log output


# ── lifecycle ──────────────────────────────────────────────────────────────


def test_lifecycle_success_with_results_and_log(lab):
    ts = _taskset(lab, ["do a", "do b"])
    created = _submit(lab, ts)
    assert created.status_code == 201, created.text
    job = _wait(lab, created.json()["id"])
    assert job["status"] == "succeeded"
    assert job["skill_source"]["name"] == "demo-skill"

    results = lab.get(f"/api/skill-lab/jobs/{job['id']}/results").json()
    assert results["summary"] == {
        "tasks": 2, "passed": 2, "invalid": 0, "pass_rate": 1.0,
        "soft_mean": 1.0, "duration_s": 0.2,
    }
    assert [r["id"] for r in results["rows"]] == ["task_001", "task_002"]

    log_1 = lab.get(f"/api/skill-lab/jobs/{job['id']}/log").json()
    assert "[skilleval] tasks: 2" in log_1["content"]
    log_2 = lab.get(
        f"/api/skill-lab/jobs/{job['id']}/log", params={"offset": log_1["next_offset"]}
    ).json()
    assert log_2["content"] == ""  # nothing new past EOF


def test_failure_captures_exit_code_and_tail(lab):
    ts = _taskset(lab, ["FAIL:3"])
    job = _wait(lab, _submit(lab, ts).json()["id"])
    assert job["status"] == "failed"
    assert "process exited 3" in job["error"]
    assert "boom" in job["error"]
    # results are pending forever for a failed run → 404 with the status
    pending = lab.get(f"/api/skill-lab/jobs/{job['id']}/results")
    assert pending.status_code == 404
    assert pending.json()["code"] == "skill_lab.results_pending"


def test_cancel_running_kills_the_process_group(lab):
    ts = _taskset(lab, ["SPAWNCHILD"])
    job_id = _submit(lab, ts).json()["id"]
    # wait for the stub to record its child pid
    child_file = artifacts.out_root(job_id) / "child_pid.txt"
    deadline = time.monotonic() + 15
    while not child_file.is_file():
        assert time.monotonic() < deadline, "stub never spawned its child"
        time.sleep(0.1)
    child_pid = int(child_file.read_text())

    cancelled = lab.post(f"/api/skill-lab/jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    job = _wait(lab, job_id, statuses=("cancelled",))
    assert job["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        pytest.fail("grandchild survived the group kill")


def test_cancel_during_the_spawn_window_still_kills(lab, monkeypatch):
    """A cancel that lands after the `running` flip but before the process is
    registered must not leave a full-price run going to completion."""
    from sqlalchemy import select

    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabJob

    real_spawn = runner.spawn

    def spawn_then_cancel(command, env, log_file):
        proc = real_spawn(command, env, log_file)
        db = SessionLocal()
        try:
            for row in db.scalars(select(SkillLabJob)).all():
                row.cancel_requested = True
            db.commit()
        finally:
            db.close()
        return proc

    monkeypatch.setattr(runner, "spawn", spawn_then_cancel)
    ts = _taskset(lab, ["SLEEP:60"], name="spawn-window")
    # The stub would run for a minute; settling as cancelled inside the wait
    # below is only possible if the group kill happened in that window.
    job = _wait(lab, _submit(lab, ts).json()["id"], statuses=("cancelled",), timeout=15.0)
    assert job["status"] == "cancelled"


def test_cancel_while_queued(lab):
    blocker_ts = _taskset(lab, ["SLEEP:8"], name="blocker")
    queued_ts = _taskset(lab, ["quick"], name="queued")
    blocker = _submit(lab, blocker_ts).json()
    queued = _submit(lab, queued_ts).json()
    assert queued["queue_position"] >= 1

    cancelled = lab.post(f"/api/skill-lab/jobs/{queued['id']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    lab.post(f"/api/skill-lab/jobs/{blocker['id']}/cancel")
    _wait(lab, blocker["id"], statuses=("cancelled",))
    # the cancelled-in-queue job never ran: no out/, no log
    assert not artifacts.out_root(queued["id"]).exists()


def test_terminal_jobs_cannot_cancel_but_can_delete(lab):
    ts = _taskset(lab, ["quick"])
    job = _wait(lab, _submit(lab, ts).json()["id"])
    refused = lab.post(f"/api/skill-lab/jobs/{job['id']}/cancel")
    assert refused.status_code == 400
    directory = artifacts.job_dir(job["id"])
    assert directory.exists()
    assert lab.delete(f"/api/skill-lab/jobs/{job['id']}").json() == {"ok": True}
    assert not directory.exists()
    assert lab.get(f"/api/skill-lab/jobs/{job['id']}").status_code == 404


def test_interrupted_sweep(lab):
    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabJob

    db = SessionLocal()
    try:
        db.add(
            SkillLabJob(
                workspace_id="default", type="eval", status="running",
                taskset_id="ts_x", skill_source={}, params={},
            )
        )
        db.commit()
    finally:
        db.close()
    jobs.sweep_interrupted_jobs()
    listed = lab.get("/api/skill-lab/jobs").json()
    assert listed[0]["status"] == "interrupted"
    assert "restart" in (listed[0]["error"] or "")


# ── guards ─────────────────────────────────────────────────────────────────


def test_submit_refused_without_worker_resources(lab):
    set_default_resources({})
    ts_missing = _submit(lab, "ts_whatever")
    assert ts_missing.status_code == 503
    assert ts_missing.json()["code"] == "skill_lab.not_provisioned"


def test_submit_refused_on_assumed_role_workspace(lab, monkeypatch):
    workspace = SimpleNamespace(
        resources=dict(WORKER_RESOURCES), region="us-west-2", role_arn="arn:role/spoke"
    )
    with pytest.raises(Exception) as err:
        runner.require_worker(workspace)
    assert "assumed-role" in str(err.value)


def test_param_bounds(lab):
    ts = _taskset(lab, ["quick"])
    bad = _submit(lab, ts, params={"workers": 99})
    assert bad.status_code == 422
    assert bad.json()["code"] == "skill_lab.bad_params"


def test_unknown_split_rejected_and_single_ignores_split(lab):
    ts = _taskset(lab, ["quick"])
    bad = _submit(lab, ts, split="test")
    # single-mode sets have no split files; explicit split → 422
    assert bad.status_code == 422


def test_taskset_delete_blocked_while_referenced(lab):
    ts = _taskset(lab, ["quick"])
    job = _wait(lab, _submit(lab, ts).json()["id"])
    blocked = lab.delete(f"/api/skill-lab/tasksets/{ts}")
    assert blocked.status_code == 409
    assert blocked.json()["code"] == "skill_lab.taskset_in_use"
    lab.delete(f"/api/skill-lab/jobs/{job['id']}")
    assert lab.delete(f"/api/skill-lab/tasksets/{ts}").json() == {"ok": True}


# ── results math + artifacts ───────────────────────────────────────────────


def test_results_exclude_invalid_rows_from_denominators(lab, tmp_path):
    job_id = "job_fixture01"
    out = artifacts.out_root(job_id)
    out.mkdir(parents=True)
    rows = [
        {"id": "a", "hard": 1, "soft": 1.0, "score_valid": True, "duration_s": 1.0},
        {"id": "b", "hard": 0, "soft": 0.5, "score_valid": True, "duration_s": 1.0},
        {"id": "c", "hard": 0, "soft": 0.0, "score_valid": False, "duration_s": 0.0,
         "judge_error": "infra"},
    ]
    (out / "results.json").write_text(json.dumps(rows))
    results = artifacts.eval_results(job_id)
    assert results["summary"]["tasks"] == 3
    assert results["summary"]["invalid"] == 1
    assert results["summary"]["pass_rate"] == 0.5   # 1/2 scored, c excluded
    assert results["summary"]["soft_mean"] == 0.75


def test_artifact_traversal_guard_and_reads(lab):
    job_id = "job_fixture02"
    out = artifacts.out_root(job_id)
    (out / "rollouts").mkdir(parents=True)
    (out / "rollouts" / "note.txt").write_text("hello")
    (out / "rollouts" / "blob.bin").write_bytes(b"\x00\x01")

    listing = artifacts.list_artifacts(job_id, "rollouts")
    assert [f["name"] for f in listing["files"]] == ["blob.bin", "note.txt"]
    assert artifacts.read_artifact(job_id, "rollouts/note.txt")["content"] == "hello"
    assert artifacts.read_artifact(job_id, "rollouts/blob.bin")["kind"] == "binary"
    from app.core.errors import AppError

    with pytest.raises(AppError):
        artifacts.list_artifacts(job_id, "../../secrets")
    with pytest.raises(AppError):
        artifacts.list_artifacts(job_id, "/etc")
    with pytest.raises(AppError):  # a NUL would blow up Path.resolve() as a 500
        artifacts.list_artifacts(job_id, "note\0.txt")

    # A symlink planted inside out/ (the CLI copies rollout trees around) is
    # resolved before the containment check, so it cannot widen the window.
    (out / "escape").symlink_to("/etc")
    with pytest.raises(AppError):
        artifacts.list_artifacts(job_id, "escape")
    with pytest.raises(AppError):
        artifacts.read_artifact(job_id, "escape/hostname")


def test_reads_are_capped(lab, monkeypatch):
    job_id = "job_fixture03"
    out = artifacts.out_root(job_id)
    out.mkdir(parents=True)
    monkeypatch.setattr(artifacts, "TEXT_ARTIFACT_CAP", 8)
    monkeypatch.setattr(artifacts, "LOG_CHUNK_CAP", 8)
    (out / "big.txt").write_text("0123456789abcdef")
    read = artifacts.read_artifact(job_id, "big.txt")
    assert read["content"] == "01234567" and read["truncated"] is True
    assert read["size"] == 16  # the real size, not the served slice

    (artifacts.job_dir(job_id) / "log.txt").write_text("0123456789")
    first = artifacts.read_log(job_id)
    assert first == {"content": "01234567", "next_offset": 8, "eof": False}
    second = artifacts.read_log(job_id, first["next_offset"])
    assert second == {"content": "89", "next_offset": 10, "eof": True}


# ── ad-hoc upload source ───────────────────────────────────────────────────


def _skill_zip(name: str, extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\ndescription: a demo skill\nversion: 1.2.3\n---\n\n# demo\n",
        )
        for path, body in (extra or {}).items():
            archive.writestr(path, body)
    return buffer.getvalue()


def _stage_zip(client, data: bytes):
    return client.post(
        "/api/registry/skills/inspect",
        files={"file": ("skill.zip", data, "application/zip")},
    )


def test_uploaded_skill_keeps_support_files(lab):
    staged = _stage_zip(lab, _skill_zip("demo-upload", {"refs/table.csv": "a,b\n1,2\n"}))
    assert staged.status_code == 200, staged.text
    body = staged.json()
    ts = _taskset(lab, ["quick"], name="upload-ts")
    job = _wait(
        lab,
        _submit(
            lab,
            ts,
            skill_source={"kind": "upload", "staging_id": body["staging_id"], "index": 0},
        ).json()["id"],
    )
    assert job["status"] == "succeeded"
    assert job["skill_source"] == {"kind": "upload", "name": "demo-upload", "version": "1.2.3"}
    skill_dir = artifacts.job_dir(job["id"]) / "skills" / "demo-upload"
    # AC: a multi-file bundle reaches the rollout workspace whole
    assert (skill_dir / "SKILL.md").is_file()
    assert (skill_dir / "refs" / "table.csv").read_text() == "a,b\n1,2\n"

    # staging is deliberately kept — the same upload can be scored again
    again = _submit(
        lab, ts, skill_source={"kind": "upload", "staging_id": body["staging_id"], "index": 0}
    )
    assert again.status_code == 201, again.text
    _wait(lab, again.json()["id"])


def test_uploaded_skill_name_cannot_escape_the_job_dir(lab):
    """`name` is caller-supplied SKILL.md frontmatter — and a path segment.

    `skills/../../escaped` would land beside the job dirs, where deleting the
    refused job could never clean it up.
    """
    staged = _stage_zip(lab, _skill_zip("../../escaped"))
    assert staged.status_code == 200, staged.text
    ts = _taskset(lab, ["quick"], name="escape-ts")
    refused = _submit(
        lab,
        ts,
        skill_source={
            "kind": "upload",
            "staging_id": staged.json()["staging_id"],
            "index": 0,
        },
    )
    assert refused.status_code == 422
    assert refused.json()["code"] == "skill_lab.skill_unreadable"
    assert not (artifacts.JOBS_DIR / "escaped").exists()
    assert lab.get("/api/skill-lab/jobs").json() == []  # no row, no job dir


def test_job_dir_layout_and_materialization_log(lab):
    ts = _taskset(lab, ["quick"])
    job = _wait(lab, _submit(lab, ts).json()["id"])
    directory = artifacts.job_dir(job["id"])
    assert (directory / "skills" / "demo-skill" / "SKILL.md").is_file()
    log = lab.get(f"/api/skill-lab/jobs/{job['id']}/log").json()["content"]
    assert log.startswith("materialized stub skill")
