"""N12 E0-5 — the backend ↔ worker job protocol (plan §B.0): payload round-trip, the S3
key layout, deterministic record ids, shard planning, stale detection and the reserved
Skill-Lab kinds (lead ruling 4)."""

from datetime import UTC, datetime, timedelta

import pytest

from app.evaluation import jobs


def _spec(**over):
    base = dict(job_id="run1234abcd", job_type="batch_eval",
                workspace={"account_id": "111122223333", "region": "cn-northwest-1"},
                sink={"bucket": "launchpad-artifacts", "prefix": "evaluations/run1234abcd/a1/"},
                spec={"run_id": "run1234abcd", "sessions": []})
    base.update(over)
    return jobs.JobSpec(**base)


def test_job_spec_round_trips_through_the_invoke_payload():
    spec = _spec()
    payload = spec.to_payload()
    assert payload["job_type"] == "batch_eval" and payload["attempt"] == 1
    assert payload["deadline_s"] == 25200
    assert jobs.JobSpec.from_payload(payload) == spec


@pytest.mark.parametrize("kind", jobs.RESERVED_JOB_TYPES)
def test_reserved_skill_lab_kinds_are_refused_with_the_agreed_code(kind):
    assert kind in ("skill_taskgen", "skill_train", "skill_eval")
    with pytest.raises(jobs.KindUnsupportedError) as exc:
        jobs.validate_job_type(kind)
    assert exc.value.code == "evaluation.kind_unsupported"
    with pytest.raises(jobs.KindUnsupportedError):
        jobs.JobSpec.from_payload(_spec(job_type=kind).to_payload())


def test_unknown_kinds_are_refused_too():
    with pytest.raises(jobs.KindUnsupportedError):
        jobs.validate_job_type("make_coffee")


@pytest.mark.parametrize("kind", jobs.JOB_TYPES)
def test_implemented_kinds_validate(kind):
    assert jobs.validate_job_type(kind) == kind


def test_s3_key_layout_matches_the_protocol():
    assert jobs.job_prefix("j1", 2) == "evaluations/j1/a2/"
    assert jobs.status_key("j1", 2) == "evaluations/j1/a2/status.json"
    assert jobs.results_key("j1", 2) == "evaluations/j1/a2/results.jsonl"
    assert jobs.invocations_key("j1", 2) == "evaluations/j1/a2/invocations.jsonl"
    assert jobs.done_key("j1", 2) == "evaluations/j1/a2/done.json"
    assert jobs.artifact_key("j1", 2, "insights.json") == "evaluations/j1/a2/insights.json"
    assert jobs.cancel_key("j1") == "evaluations/j1/cancel"  # attempt-independent
    assert jobs.shards_key("run1") == "evaluations/run1/shards.json"
    assert _spec().sink["prefix"] == jobs.job_prefix("run1234abcd", 1)


def test_record_id_is_deterministic_and_distinct_per_ordinal():
    a = jobs.record_id("j1", "s1", "Builtin.Helpfulness", 0)
    assert a == jobs.record_id("j1", "s1", "Builtin.Helpfulness", 0)
    assert len(a) == 40 and a != jobs.record_id("j1", "s1", "Builtin.Helpfulness", 1)
    assert a != jobs.record_id("j2", "s1", "Builtin.Helpfulness", 0)


def test_runtime_session_ids_are_fresh_and_long_enough():
    first, second = jobs.runtime_session_id("j1", 1), jobs.runtime_session_id("j1", 1)
    assert first != second and len(first) >= 33
    assert first.startswith("evaljob-j1-a1-")


def test_status_and_done_round_trip_and_defaults():
    status = jobs.JobStatus(phase="waiting", progress={"done": 2, "total": 5, "message": "m"})
    assert status.state == "RUNNING" and status.updated_at
    assert jobs.JobStatus.from_dict(status.to_dict()) == status
    with pytest.raises(ValueError):
        jobs.JobStatus(phase="dancing")
    done = jobs.JobDone(state="COMPLETED", result_count=3,
                        summary={"scores": [{"evaluatorId": "e", "score": 1.0, "count": 3}]})
    assert jobs.JobDone.from_dict(done.to_dict()) == done
    with pytest.raises(ValueError):
        jobs.JobDone(state="RUNNING")


def test_shard_planner_boundaries():
    # fits: one shard covering every session
    assert jobs.plan_shards(10, 3) == [(0, 10)]
    # over the session cap → chunks of max_sessions
    assert jobs.plan_shards(301, 1, max_sessions=150) == [(0, 150), (150, 300), (300, 301)]
    # over the time budget → enough shards that each fits (20 s per judge call, 4-way)
    plan = jobs.plan_shards(150, 10, judge_seconds_est=20, judge_concurrency=4, budget_s=3600)
    assert plan[0][0] == 0 and plan[-1][1] == 150
    assert all(jobs.estimate_seconds(b - a, 10, judge_seconds_est=20, judge_concurrency=4)
               <= 3600 for a, b in plan)
    assert jobs.plan_shards(0, 3) == []
    assert jobs.shard_job_id("run1", 2) == "run1-s2"


def test_stale_detection_uses_the_status_timestamp():
    now = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
    fresh = jobs.JobStatus(phase="evaluating", updated_at=(now - timedelta(seconds=30)).isoformat())
    old = jobs.JobStatus(phase="evaluating", updated_at=(now - timedelta(seconds=700)).isoformat())
    assert not jobs.is_stale(fresh, now=now, stale_s=600)
    assert jobs.is_stale(old, now=now, stale_s=600)
    assert jobs.is_stale(None, now=now, stale_s=600)  # no status at all
