# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Round-9 fault probes at the Skill Lab seam (r17-s): the harness is a subprocess stub stack
(`tests/test_skill_lab_jobs.STUB_CLI`) whose task questions are the fault knobs — `FAIL:<code>`
(the CLI exits non-zero), `ERROR:infra` / `ERROR:exec` (every rollout errors). Contract
(skill-lab-executor.md §6 + `verdict.all_rollouts_failed_reason`, N10v3-bug3): exit code 0 is the
CLI's verdict on itself — a run whose rollouts ALL failed is a FAILED job naming the first error
and the infrastructure / exec split; one scored rollout keeps the job `succeeded`; a non-zero
exit is `failed` with `process exited <code>`."""

from __future__ import annotations

import uuid

import pytest

from tests.test_skill_lab_jobs import _submit, _wait, lab  # noqa: F401 — fixture by name


def _taskset(client, questions: list[str]) -> str:
    body = {"name": f"ts-fault-{uuid.uuid4().hex[:6]}", "mode": "single",
            "tasks_by_split": {"tasks": [
                {"id": f"t_{i}", "question": q, "rubric": "PASS always"}
                for i, q in enumerate(questions)]}}
    res = client.post("/api/skill-lab/tasksets", json=body)
    assert res.status_code == 201, res.text
    return res.json()["id"]


@pytest.mark.parametrize("kind", ["infra", "exec"])
def test_every_rollout_failing_is_a_failed_job_naming_the_split(lab, client, kind):
    ts = _taskset(client, [f"ERROR:{kind}", f"ERROR:{kind}"])
    res = _submit(client, ts)
    assert res.status_code == 201, res.text
    job = _wait(client, res.json()["id"])
    assert job["status"] == "failed", job
    assert "all 2 rollouts failed" in (job.get("error") or ""), job.get("error")
    split = "2 infrastructure · 0 exec" if kind == "infra" else "0 infrastructure · 2 exec"
    assert split in job["error"], job["error"]
    assert "first error:" in job["error"]


def test_one_scored_rollout_keeps_the_job_succeeded(lab, client):
    ts = _taskset(client, ["ERROR:infra", "q ok"])
    res = _submit(client, ts)
    assert res.status_code == 201, res.text
    job = _wait(client, res.json()["id"])
    assert job["status"] == "succeeded", job


@pytest.mark.parametrize("code", [1, 137])
def test_a_non_zero_exit_is_a_failed_job_with_the_code(lab, client, code):
    ts = _taskset(client, [f"FAIL:{code}", "q ok"])
    res = _submit(client, ts)
    assert res.status_code == 201, res.text
    job = _wait(client, res.json()["id"])
    assert job["status"] == "failed", job
    assert f"process exited {code}" in (job.get("error") or ""), job.get("error")
