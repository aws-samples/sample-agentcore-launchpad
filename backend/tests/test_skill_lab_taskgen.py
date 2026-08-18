"""Skill Lab taskgen: params, command construction, lifecycle, review-then-import."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.skill_lab import artifacts, jobs, runner
from app.skill_lab import tasksets as taskset_svc
from tests.conftest import set_default_resources

# Mimics scripts/generate_tasks.py's CLI surface. Behavior is keyed on the
# --guidance value (the env is allowlisted, so flags are the only channel):
# "FAIL" exits nonzero; "IDS:a,b" writes those ids; default writes gen_%03d.
STUB_GEN_CLI = '''
import argparse, json, pathlib, sys
parser = argparse.ArgumentParser()
parser.add_argument("--skill", action="append")
for flag in ("--backend","--model","--count","--timeout","--out_root",
             "--guidance","--min-tasks-per-skill","--existing-tasks","--target-split"):
    parser.add_argument(flag)
args = parser.parse_args()
guidance = args.guidance or ""
if guidance == "FAIL":
    print("generation exploded", flush=True); sys.exit(3)
count = int(args.count)
if guidance.startswith("IDS:"):
    ids = guidance[4:].split(",")
else:
    ids = ["gen_%03d" % i for i in range(1, count + 1)]
tasks = [{"id": i, "question": "q for " + i, "rubric": "PASS always"} for i in ids]
out = pathlib.Path(args.out_root); out.mkdir(parents=True, exist_ok=True)
(out / "generated_tasks.json").write_text(json.dumps(tasks))
summary = {"count": len(tasks), "requested_count": count, "backend": args.backend,
           "model": args.model, "skills": args.skill,
           "existing_tasks": args.existing_tasks, "target_split": args.target_split}
(out / "gen_summary.json").write_text(json.dumps(summary))
print("[taskgen] done: %d tasks" % len(tasks), flush=True)
'''

WORKER_RESOURCES = {
    "skill_lab_worker_runtime_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/rt",
    "skill_lab_worker_role_arn": "arn:aws:iam::111122223333:role/worker",
    "skill_lab_worker_image_digest": "sha256:x",
    "artifacts_bucket": "bkt",
}


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    stub = tmp_path / "stub_gen_cli.py"
    stub.write_text(STUB_GEN_CLI)
    fake_settings = get_settings().model_copy(update={"skill_lab_python": sys.executable})
    monkeypatch.setattr(runner, "TASKGEN_SCRIPT", stub)
    monkeypatch.setattr(runner, "get_settings", lambda: fake_settings)
    monkeypatch.setattr(artifacts, "JOBS_DIR", tmp_path / "jobs")
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    monkeypatch.setattr(runner, "sweep_exec_jobs_prefix", lambda ws, log: None)

    def fake_materialize(workspace, record_id, dest_parent, log):
        skill_dir = dest_parent / "skills" / f"skill-{record_id}"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(f"# {record_id}\n")
        log("materialized stub skill")
        return skill_dir, {"kind": "registry", "record_id": record_id, "name": f"skill-{record_id}"}

    monkeypatch.setattr(runner, "materialize_registry_skill", fake_materialize)
    set_default_resources(WORKER_RESOURCES)
    return client


def _submit(client, **overrides):
    body = {
        "type": "taskgen",
        "skill_source": {"kind": "registry", "record_id": "rec-1"},
        "params": {"count": 3},
    }
    body.update(overrides)
    return client.post("/api/skill-lab/jobs", json=body)


def _wait(client, job_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    job = None
    while time.monotonic() < deadline:
        job = client.get(f"/api/skill-lab/jobs/{job_id}").json()
        if job["status"] in ("succeeded", "failed", "cancelled"):
            return job
        time.sleep(0.1)
    pytest.fail(f"job {job_id} did not finish in {timeout}s (last: {job})")


def _taskset(client, tasks, name="ts", mode="single"):
    body = {"name": name, "mode": mode, "tasks_by_split": tasks}
    response = client.post("/api/skill-lab/tasksets", json=body)
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ── params + command contracts ─────────────────────────────────────────────


def test_clamp_taskgen_params(lab):
    params = runner.clamp_taskgen_params(None)
    assert params["target_backend"] == "claude_code_exec"
    assert params["model"] == get_settings().skill_lab_target_model_id
    assert params["count"] == 5 and params["timeout"] == 900

    codex = runner.clamp_taskgen_params({"target_backend": "codex_exec"})
    assert codex["model"] == get_settings().skill_lab_codex_target_model_id

    for bad in ({"count": 0}, {"count": 31}, {"timeout": 10}, {"target_backend": "x"},
                {"guidance": "g" * 4001}):
        with pytest.raises(AppError) as err:
            runner.clamp_taskgen_params(bad)
        assert err.value.status_code == 422


def test_build_taskgen_command_multi_skill_and_expansion(lab, tmp_path):
    params = runner.clamp_taskgen_params({"count": 2, "guidance": "edge cases"})
    dirs = [tmp_path / "a", tmp_path / "b", tmp_path / "c"]
    snapshot = tmp_path / "existing_tasks.json"
    command = runner.build_taskgen_command(
        skill_dirs=dirs, out_root=tmp_path / "out", params=params,
        expansion=(snapshot, "val"),
    )
    text = " ".join(command)
    assert text.count("--skill") == 3
    # multi-skill floors the count at one task per skill
    assert "--count 3" in text
    assert "--min-tasks-per-skill 1" in text
    assert "--guidance edge cases" in text
    assert f"--existing-tasks {snapshot}" in text and "--target-split val" in text

    single = runner.build_taskgen_command(
        skill_dirs=dirs[:1], out_root=tmp_path / "out", params=params
    )
    assert "--min-tasks-per-skill" not in " ".join(single)
    assert "--count 2" in " ".join(single)


# ── lifecycle: generate → review → import ──────────────────────────────────


def test_generate_review_import(lab):
    job = _submit(lab).json()
    assert job["type"] == "taskgen" and job["taskset_id"] == ""
    done = _wait(lab, job["id"])
    assert done["status"] == "succeeded"

    results = lab.get(f"/api/skill-lab/jobs/{job['id']}/results").json()
    assert results["type"] == "taskgen"
    assert results["count"] == 3
    assert [t["id"] for t in results["tasks"]] == ["gen_001", "gen_002", "gen_003"]
    assert results["summary"]["backend"] == "claude_code_exec"

    imported = lab.post(
        f"/api/skill-lab/jobs/{job['id']}/import-taskset", json={"name": "generated set"}
    )
    assert imported.status_code == 201, imported.text
    taskset = imported.json()["taskset"]
    assert taskset["counts"] == {"tasks": 3}
    listed = lab.get("/api/skill-lab/tasksets").json()
    assert any(row["id"] == taskset["id"] for row in listed)

    again = lab.post(
        f"/api/skill-lab/jobs/{job['id']}/import-taskset", json={"name": "twice"}
    )
    assert again.status_code == 409
    assert again.json()["code"] == "skill_lab.already_imported"


def test_multi_skill_source(lab):
    job = _submit(
        lab, skill_source={"kind": "registry", "record_ids": ["rec-1", "rec-2"]}
    ).json()
    done = _wait(lab, job["id"])
    assert done["status"] == "succeeded"
    assert done["skill_source"] == {
        "kind": "registry", "record_ids": ["rec-1", "rec-2"],
        "names": ["skill-rec-1", "skill-rec-2"],
    }
    summary = lab.get(f"/api/skill-lab/jobs/{job['id']}/results").json()["summary"]
    assert len(summary["skills"]) == 2


def test_failed_generation_keeps_log(lab):
    job = _submit(lab, params={"count": 3, "guidance": "FAIL"}).json()
    done = _wait(lab, job["id"])
    assert done["status"] == "failed"
    assert lab.get(f"/api/skill-lab/jobs/{job['id']}/results").status_code == 404
    log = lab.get(f"/api/skill-lab/jobs/{job['id']}/log").json()["content"]
    assert "generation exploded" in log


def test_import_guards(lab, client):
    # not a taskgen job → 400 (row crafted directly; no CLI run needed)
    from app.core.db import SessionLocal
    from app.skill_lab.models import SkillLabJob

    db = SessionLocal()
    eval_row = SkillLabJob(
        workspace_id="default", type="eval", taskset_id="x", params={}, status="succeeded"
    )
    running = SkillLabJob(
        workspace_id="default", type="taskgen", taskset_id="", params={}, status="running"
    )
    db.add_all([eval_row, running])
    db.commit()
    eval_id, running_id = eval_row.id, running.id
    db.close()

    response = client.post(f"/api/skill-lab/jobs/{eval_id}/import-taskset", json={"name": "n"})
    assert response.status_code == 400
    assert response.json()["code"] == "skill_lab.not_a_taskgen_job"

    response = client.post(f"/api/skill-lab/jobs/{running_id}/import-taskset", json={"name": "n"})
    assert response.status_code == 409
    assert response.json()["code"] == "skill_lab.job_not_finished"

    # succeeded taskgen row whose out/ was never written → 409 results_missing
    db = SessionLocal()
    hollow = SkillLabJob(
        workspace_id="default", type="taskgen", taskset_id="", params={}, status="succeeded"
    )
    db.add(hollow)
    db.commit()
    hollow_id = hollow.id
    db.close()
    response = client.post(f"/api/skill-lab/jobs/{hollow_id}/import-taskset", json={"name": "n"})
    assert response.status_code == 409
    assert response.json()["code"] == "skill_lab.results_missing"


# ── expansion ──────────────────────────────────────────────────────────────


def _seed_tasks(ids):
    return [{"id": i, "question": f"q {i}", "rubric": "PASS always"} for i in ids]


def test_expansion_apply(lab):
    ts = _taskset(lab, {"tasks": _seed_tasks(["task_001", "task_002"])})
    job = _submit(lab, taskset_id=ts, target_split="tasks").json()
    assert job["taskset_id"] == ts and job["split"] == "tasks"
    done = _wait(lab, job["id"])
    assert done["status"] == "succeeded"

    # the CLI received the expansion snapshot of the CURRENT taskset content
    summary = lab.get(f"/api/skill-lab/jobs/{job['id']}/results").json()["summary"]
    assert summary["existing_tasks"] and summary["target_split"] == "tasks"
    snapshot = json.loads(Path(summary["existing_tasks"]).read_text())
    assert {t["id"] for t in snapshot["tasks_by_split"]["tasks"]} == {"task_001", "task_002"}

    applied = lab.post(f"/api/skill-lab/jobs/{job['id']}/apply-expansion")
    assert applied.status_code == 200, applied.text
    assert applied.json()["taskset"]["counts"] == {"tasks": 5}

    again = lab.post(f"/api/skill-lab/jobs/{job['id']}/apply-expansion")
    assert again.status_code == 409
    assert again.json()["code"] == "skill_lab.already_imported"


def test_expansion_split_mode_new_test_split(lab):
    ts = _taskset(
        lab,
        {"train": _seed_tasks(["tr_1"]), "val": _seed_tasks(["va_1"])},
        mode="split",
    )
    job = _submit(lab, taskset_id=ts, target_split="test").json()
    done = _wait(lab, job["id"])
    assert done["status"] == "succeeded"
    applied = lab.post(f"/api/skill-lab/jobs/{job['id']}/apply-expansion")
    assert applied.status_code == 200, applied.text
    assert applied.json()["taskset"]["counts"] == {"train": 1, "val": 1, "test": 3}


def test_expansion_conflict_when_taskset_changed(lab):
    ts = _taskset(lab, {"tasks": _seed_tasks(["task_001"])})
    job = _submit(lab, taskset_id=ts, target_split="tasks").json()
    done = _wait(lab, job["id"])
    assert done["status"] == "succeeded"

    # the set gains gen_001 behind the job's back → apply must refuse
    update = lab.put(
        f"/api/skill-lab/tasksets/{ts}",
        json={"tasks_by_split": {"tasks": _seed_tasks(["task_001", "gen_001"])}},
    )
    assert update.status_code == 200, update.text
    applied = lab.post(f"/api/skill-lab/jobs/{job['id']}/apply-expansion")
    assert applied.status_code == 409
    assert applied.json()["code"] == "skill_lab.expansion_conflict"


def test_expansion_validation(lab):
    response = _submit(lab, taskset_id="nope")  # half a pair
    assert response.status_code == 422

    ts = _taskset(lab, {"tasks": _seed_tasks(["task_001"])}, name="single-set")
    response = _submit(lab, taskset_id=ts, target_split="train")
    assert response.status_code == 422  # single mode → only 'tasks'

    split_ts = _taskset(
        lab,
        {"train": _seed_tasks(["tr_1"]), "val": _seed_tasks(["va_1"])},
        name="split-set",
        mode="split",
    )
    response = _submit(lab, taskset_id=split_ts, target_split="weird")
    assert response.status_code == 422


def test_expansion_target_counts_as_taskset_in_use(lab):
    ts = _taskset(lab, {"tasks": _seed_tasks(["task_001"])})
    job = _submit(lab, taskset_id=ts, target_split="tasks").json()
    _wait(lab, job["id"])
    from app.core.db import SessionLocal

    db = SessionLocal()
    assert jobs.taskset_in_use(db, "default", ts) is True
    db.close()
    response = lab.delete(f"/api/skill-lab/tasksets/{ts}")
    assert response.status_code == 409
