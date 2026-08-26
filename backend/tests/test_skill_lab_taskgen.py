"""Skill Lab taskgen: params, command construction, lifecycle, review-then-import."""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import get_settings
from app.core.errors import AppError
from app.skill_lab import artifacts, jobs, runner, task_assets
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
             "--guidance","--min-tasks-per-skill","--existing-tasks","--target-split",
             "--attachments","--attachment-assets"):
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
           "existing_tasks": args.existing_tasks, "target_split": args.target_split,
           "attachments": args.attachments, "attachment_assets": args.attachment_assets}
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
    monkeypatch.setattr(task_assets, "STAGING_DIR", tmp_path / "staging")
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


def _stage(client, *files):
    """Upload through the real staging API so tokens/digests are genuine."""
    response = client.post(
        "/api/skill-lab/task-assets",
        files=[("files", (name, data, "application/octet-stream")) for name, data in files],
    )
    assert response.status_code == 201, response.text
    return [{"staged_asset": row["staged_asset"]} for row in response.json()["assets"]]


def _job_dirs() -> list[Path]:
    return sorted(artifacts.JOBS_DIR.glob("*")) if artifacts.JOBS_DIR.exists() else []


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


# ── attachments: snapshot, limits, immutability ─────────────────────────────


CSV = b"region,revenue\nAPAC,1240\n"
MD = "# Q2\n\n- 收入增长 14%\n".encode()


def test_attachments_are_snapshotted_verbatim_and_handed_to_the_cli(lab):
    staged = _stage(lab, ("rows.csv", CSV), ("notes.md", MD))
    job = _submit(lab, attachments=staged).json()
    finished = _wait(lab, job["id"])
    assert finished["status"] == "succeeded", finished

    inputs = artifacts.job_dir(job["id"]) / "inputs"
    manifest = json.loads((inputs / "attachments.json").read_text())
    assert [(row["name"], row["media_type"], row["size"]) for row in manifest] == [
        ("rows.csv", "text/csv", len(CSV)),
        ("notes.md", "text/markdown", len(MD)),
    ]
    for row, payload in zip(manifest, (CSV, MD), strict=True):
        blob = inputs / task_assets.ASSETS_DIRNAME / row["sha256"]
        assert blob.read_bytes() == payload

    # The CLI is told where both halves live, and only when there are any.
    summary = json.loads((artifacts.out_root(job["id"]) / "gen_summary.json").read_text())
    assert summary["attachments"] == str(inputs / "attachments.json")
    assert summary["attachment_assets"] == str(inputs / task_assets.ASSETS_DIRNAME)


def test_a_job_without_attachments_is_unchanged(lab):
    """The regression that matters most: the existing path must not gain flags."""
    job = _submit(lab).json()
    finished = _wait(lab, job["id"])
    assert finished["status"] == "succeeded", finished
    summary = json.loads((artifacts.out_root(job["id"]) / "gen_summary.json").read_text())
    assert summary["attachments"] is None and summary["attachment_assets"] is None
    assert not (artifacts.job_dir(job["id"]) / "inputs" / "attachments.json").exists()


def test_staging_is_consumed_so_the_snapshot_is_the_only_source(lab, tmp_path):
    staged = _stage(lab, ("rows.csv", CSV))
    job = _submit(lab, attachments=staged).json()
    _wait(lab, job["id"])
    # The staged blob is gone; wiping staging entirely must not affect the job.
    assert not list((tmp_path / "staging").rglob("ta_*"))
    task_assets.sweep_expired(datetime.now(UTC) + timedelta(days=2))
    digest = json.loads(
        (artifacts.job_dir(job["id"]) / "inputs" / "attachments.json").read_text()
    )[0]["sha256"]
    blob = artifacts.job_dir(job["id"]) / "inputs" / task_assets.ASSETS_DIRNAME / digest
    assert blob.read_bytes() == CSV


@pytest.mark.parametrize(
    ("attachments", "status", "code"),
    (
        ([{"name": "no-token.csv"}], 422, "skill_lab.asset_descriptor_invalid"),
        ([{"staged_asset": "ta_bogus"}], 404, "skill_lab.asset_token_not_found"),
    ),
)
def test_attachment_tokens_are_verified_before_the_job_exists(lab, attachments, status, code):
    response = _submit(lab, attachments=attachments)
    assert response.status_code == status, response.text
    assert response.json()["code"] == code
    assert _job_dirs() == [] and lab.get("/api/skill-lab/jobs?type=taskgen").json() == []


def test_duplicate_attachment_names_are_refused_case_insensitively(lab):
    first = _stage(lab, ("Rows.csv", CSV))
    second = _stage(lab, ("rows.csv", b"other,bytes\n1,2\n"))
    response = _submit(lab, attachments=first + second)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "skill_lab.asset_duplicate_name"


def test_attachment_count_and_aggregate_bytes_are_bounded(lab, monkeypatch):
    monkeypatch.setattr(runner, "TASKGEN_MAX_ATTACHMENTS", 2)
    three = _stage(lab, ("a.csv", CSV), ("b.csv", CSV + b"x\n"), ("c.csv", CSV + b"y\n"))
    over_count = _submit(lab, attachments=three)
    assert over_count.status_code == 422, over_count.text
    assert over_count.json()["code"] == "skill_lab.asset_limit_exceeded"

    monkeypatch.setattr(runner, "TASKGEN_MAX_ATTACHMENT_BYTES", len(CSV))
    over_bytes = _submit(lab, attachments=three[:2])
    assert over_bytes.status_code == 413, over_bytes.text
    assert over_bytes.json()["code"] == "skill_lab.asset_limit_exceeded"


def test_a_refused_attachment_leaves_no_job_directory_behind(lab):
    good = _stage(lab, ("rows.csv", CSV))
    response = _submit(lab, attachments=good + [{"staged_asset": "ta_missing"}])
    assert response.status_code == 404, response.text
    # Neither half of the write survives: no job directory and no ledger row, so a
    # partially-resolved attachment set can never be picked up by the restart sweep.
    assert _job_dirs() == []
    assert lab.get("/api/skill-lab/jobs?type=taskgen").json() == []


# ── attachments: declaration → descriptor on import/apply ──────────────────


def _declaring_stub(ids_to_names: dict[str, list[str]]) -> str:
    """A stub CLI that writes `attachments` declarations for chosen task ids."""
    return STUB_GEN_CLI.replace(
        'tasks = [{"id": i, "question": "q for " + i, "rubric": "PASS always"} for i in ids]',
        "declared = " + json.dumps(ids_to_names) + "\n"
        'tasks = [{"id": i, "question": "q for " + i, "rubric": "PASS always",\n'
        '          **({"attachments": declared[i]} if i in declared else {})}\n'
        "         for i in ids]",
    )


@pytest.fixture
def declaring_lab(lab, tmp_path, monkeypatch):
    """`lab`, but the generator declares rows.csv on the first generated task."""
    stub = tmp_path / "declaring_cli.py"
    stub.write_text(_declaring_stub({"gen_001": ["rows.csv"]}))
    monkeypatch.setattr(runner, "TASKGEN_SCRIPT", stub)
    return lab


def test_declarations_become_owned_descriptors_on_import(declaring_lab):
    staged = _stage(declaring_lab, ("rows.csv", CSV), ("brief.md", MD))
    job = _submit(declaring_lab, attachments=staged, params={"count": 2}).json()
    assert _wait(declaring_lab, job["id"])["status"] == "succeeded"

    imported = declaring_lab.post(
        f"/api/skill-lab/jobs/{job['id']}/import-taskset", json={"name": "from-taskgen"}
    )
    assert imported.status_code == 201, imported.text
    taskset_id = imported.json()["taskset"]["id"]
    detail = declaring_lab.get(
        f"/api/skill-lab/tasksets/{taskset_id}?full=true"
    ).json()["tasks_by_split"]["tasks"]

    first, second = detail[0], detail[1]
    # The declaration is gone; `files` carries the whole truth.
    assert "attachments" not in first
    descriptor = first["files"]["data/rows.csv"]
    assert descriptor["asset"] == f"sha256:{hashlib.sha256(CSV).hexdigest()}"
    assert (descriptor["name"], descriptor["media_type"], descriptor["size"]) == (
        "rows.csv",
        "text/csv",
        len(CSV),
    )
    # A task that declared nothing gains nothing — the unused upload is not forced in.
    assert not second.get("files")

    # Bytes landed in the task set itself, so it no longer depends on the job.
    blob = (
        taskset_svc.taskset_dir(taskset_id)
        / "assets"
        / hashlib.sha256(CSV).hexdigest()
    )
    assert blob.read_bytes() == CSV


def test_expansion_keeps_the_targets_own_assets_and_adds_the_new_one(declaring_lab):
    # A task set that already owns an asset, created the ordinary way.
    existing = _stage(declaring_lab, ("prior.csv", b"a,b\n1,2\n"))[0]
    taskset_id = _taskset(
        declaring_lab,
        {"tasks": [{"id": "kept_1", "question": "q", "rubric": "PASS",
                    "files": {"data/prior.csv": existing}}]},
        name="expandable",
    )
    staged = _stage(declaring_lab, ("rows.csv", CSV))
    job = _submit(
        declaring_lab,
        attachments=staged,
        taskset_id=taskset_id,
        target_split="tasks",
        params={"count": 1},
    ).json()
    assert _wait(declaring_lab, job["id"])["status"] == "succeeded"
    applied = declaring_lab.post(f"/api/skill-lab/jobs/{job['id']}/apply-expansion")
    assert applied.status_code == 200, applied.text

    tasks = declaring_lab.get(
        f"/api/skill-lab/tasksets/{taskset_id}?full=true"
    ).json()["tasks_by_split"]["tasks"]
    assert [task["id"] for task in tasks] == ["kept_1", "gen_001"]
    # Both the pre-existing asset and the newly bound one are present and readable.
    assert tasks[0]["files"]["data/prior.csv"]["name"] == "prior.csv"
    assert tasks[1]["files"]["data/rows.csv"]["name"] == "rows.csv"
    assets = taskset_svc.taskset_dir(taskset_id) / "assets"
    assert (assets / hashlib.sha256(CSV).hexdigest()).read_bytes() == CSV
    assert (assets / hashlib.sha256(b"a,b\n1,2\n").hexdigest()).exists()


def test_import_refuses_a_declaration_the_job_was_never_given(lab, tmp_path, monkeypatch):
    stub = tmp_path / "ghost_cli.py"
    stub.write_text(_declaring_stub({"gen_001": ["ghost.csv"]}))
    monkeypatch.setattr(runner, "TASKGEN_SCRIPT", stub)
    staged = _stage(lab, ("rows.csv", CSV))
    job = _submit(lab, attachments=staged, params={"count": 1}).json()
    assert _wait(lab, job["id"])["status"] == "succeeded"
    response = lab.post(f"/api/skill-lab/jobs/{job['id']}/import-taskset", json={"name": "ghost"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "skill_lab.attachment_unknown"
    assert lab.get("/api/skill-lab/tasksets").json() == []


def test_the_internal_snapshot_channel_is_not_reachable_from_a_request(lab):
    """`extra_sources` must stay internal: a caller that knows a digest still
    cannot mint a descriptor for it through the public task-set API."""
    staged = _stage(lab, ("rows.csv", CSV))
    job = _submit(lab, attachments=staged, params={"count": 1}).json()
    assert _wait(lab, job["id"])["status"] == "succeeded"
    forged = {
        "asset": f"sha256:{hashlib.sha256(CSV).hexdigest()}",
        "name": "rows.csv",
        "media_type": "text/csv",
        "size": len(CSV),
    }
    response = lab.post(
        "/api/skill-lab/tasksets",
        json={
            "name": "forged",
            "mode": "single",
            "tasks_by_split": {
                "tasks": [{"id": "t1", "question": "q", "rubric": "PASS",
                           "files": {"data/rows.csv": forged}}]
            },
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "skill_lab.asset_not_owned"


def test_job_params_record_the_attachment_names_for_any_status(lab):
    """The panel must be able to show what a job was given while it is still
    running, when no gen_summary exists yet."""
    staged = _stage(lab, ("rows.csv", CSV), ("notes.md", MD))
    job = _submit(lab, attachments=staged).json()
    assert job["params"]["attachment_names"] == ["rows.csv", "notes.md"]
    listed = lab.get("/api/skill-lab/jobs?type=taskgen").json()[0]
    assert listed["params"]["attachment_names"] == ["rows.csv", "notes.md"]
    assert "attachment_names" not in _submit(lab).json()["params"]


def test_imported_taskgen_set_defers_to_the_run_level_judge_mode(declaring_lab):
    """End to end: the stored document must not carry a mode nobody chose.

    A live prod eval submitted as `chat` escalated to the agentic judge because the
    imported set carried a derived `judge_mode: auto` that re-read as explicit.
    """
    staged = _stage(declaring_lab, ("rows.csv", CSV))
    job = _submit(declaring_lab, attachments=staged, params={"count": 1}).json()
    assert _wait(declaring_lab, job["id"])["status"] == "succeeded"
    imported = declaring_lab.post(
        f"/api/skill-lab/jobs/{job['id']}/import-taskset", json={"name": "no-derived-mode"}
    )
    assert imported.status_code == 201, imported.text
    task = declaring_lab.get(
        f"/api/skill-lab/tasksets/{imported.json()['taskset']['id']}?full=true"
    ).json()["tasks_by_split"]["tasks"][0]
    assert "judge_mode" not in task
    assert not [key for key in task if key.startswith("_")]
    # The attachment binding still happened.
    assert "data/rows.csv" in task["files"]
