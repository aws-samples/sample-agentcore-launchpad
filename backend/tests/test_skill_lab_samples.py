"""Skill Lab demo samples: vendored data validity, read-only semantics, seeding."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.skill_lab import samples
from app.skill_lab import tasksets as taskset_svc

VENDOR_DEMO = (
    Path(__file__).resolve().parents[2] / "vendor" / "skillopt" / "data" / "skilleval_demo"
)


@pytest.fixture
def lab(tmp_path, monkeypatch, client):
    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    return client


# ── vendored data guards ───────────────────────────────────────────────────


def test_vendored_demo_subset_is_complete():
    """Every file the sample definitions reference must be vendored — seeding
    degrades per item, so a missing file would fail silently in production."""
    for skill in samples.SAMPLE_SKILLS:
        assert (VENDOR_DEMO / skill.source).exists(), skill.source
    for taskset in samples.SAMPLE_TASKSETS:
        for rel in taskset.sources.values():
            assert (VENDOR_DEMO / rel).is_file(), rel
    # the exclusions that keep this repo license-clean stay excluded
    assert not (VENDOR_DEMO.parent / "searchqa_split").exists()
    assert not (VENDOR_DEMO.parents[1] / "ckpt").exists()


def test_vendored_demo_tasksets_pass_the_real_validator():
    """Seeding skips the validator subprocess (the venv may not exist at
    bootstrap time) — this test is the guarantee that skip rests on: the exact
    vendored files pass the exact vendored load_tasks validator."""
    files = {
        f"{taskset.name}:{split}": VENDOR_DEMO / rel
        for taskset in samples.SAMPLE_TASKSETS
        for split, rel in taskset.sources.items()
    }
    taskset_svc._validate_files(files, sys.executable)  # raises AppError on any reject


# ── sample task set semantics ──────────────────────────────────────────────


def _seed_tasksets(workspace_id="default"):
    db = SessionLocal()
    try:
        logged: list[str] = []
        count = samples._seed_tasksets(db, workspace_id, logged.append)
        return count, logged
    finally:
        db.close()


def test_sample_tasksets_seed_idempotently_and_are_read_only(lab):
    count, _ = _seed_tasksets()
    assert count == 3
    count, logged = _seed_tasksets()
    assert count == 0 and sum("exists" in line for line in logged) == 3

    rows = lab.get("/api/skill-lab/tasksets").json()
    sample_rows = [row for row in rows if row["sample"]]
    assert len(sample_rows) == 3
    by_mode = {row["name"]: row for row in sample_rows}
    assert by_mode["Excel handling demo tasks"]["mode"] == "single"
    assert by_mode["Log triage demo tasks"]["counts"] == {"train": 4, "val": 3, "test": 3}

    target = sample_rows[0]
    put = lab.put(
        f"/api/skill-lab/tasksets/{target['id']}",
        json={"tasks_by_split": {"tasks": []}},
    )
    assert put.status_code == 409
    assert put.json()["code"] == "skill_lab.sample_readonly"
    delete = lab.delete(f"/api/skill-lab/tasksets/{target['id']}")
    assert delete.status_code == 409

    # runnable: the eval submit path resolves a split file from a sample set
    from app.skill_lab import jobs

    db = SessionLocal()
    try:
        path, split, _name = jobs._resolve_tasks_file(
            db, "default", by_mode["Log triage demo tasks"]["id"], None
        )
        assert path.is_file() and split == "test"
    finally:
        db.close()


def test_taskgen_expansion_refuses_sample_targets(lab, monkeypatch):
    _seed_tasksets()
    rows = [row for row in lab.get("/api/skill-lab/tasksets").json() if row["sample"]]
    single = next(row for row in rows if row["mode"] == "single")
    from app.skill_lab import jobs, runner

    monkeypatch.setattr(runner, "require_worker", lambda ws: {})
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as err:
            jobs.submit_taskgen_job(
                db,
                "default",
                SimpleNamespace(),
                skill_source={"kind": "registry", "record_id": "r"},
                taskset_id=single["id"],
                target_split="tasks",
            )
        assert err.value.code == "skill_lab.sample_readonly"
    finally:
        db.close()


# ── skill seeding ──────────────────────────────────────────────────────────


def test_sample_skills_seed_and_skip_existing(monkeypatch):
    from app.services import registry_console

    registered: list[tuple[str, str]] = []

    def fake_register(bundle, workspace, *, name_override=None, description_override=None):
        if name_override == "sample-report":
            raise AppError("registry.name_exists", "exists", status_code=409)
        registered.append((name_override, bundle.files[0] if bundle.files else ""))
        assert description_override
        # AgentCore Registry refuses SKILL.md without leading frontmatter (live
        # ValidationException 2026-08-18) — seeding must synthesize it, since the
        # vendored demo files are byte-identical to upstream and carry none.
        assert bundle.skill_md.startswith("---"), name_override
        assert bundle.name == name_override  # frontmatter name round-trips
        return {"record_id": "x"}

    monkeypatch.setattr(registry_console, "register_skill_bundle", fake_register)
    logged: list[str] = []
    count = samples._seed_skills(SimpleNamespace(), logged.append)
    assert count == 2
    names = [name for name, _ in registered]
    assert names == ["sample-logtriage", "sample-logtriage-v2"]
    # directory bundles carry their support files, not just SKILL.md
    assert any("SKILL.md" in files for _, files in registered)
    assert sum("exists" in line for line in logged) == 1
