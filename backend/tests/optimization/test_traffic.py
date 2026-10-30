"""Replay traffic for a canary as one pre-assigned ``batch_eval`` job
(N12 E3-1, plan §B.0 "Replay happens from the worker, routing stays authoritative in
the backend"). Replaces the Global ``send_gateway_traffic`` (SigV4 posts to the
experiment Gateway) and its concurrency tests.
"""

import time

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.jobs import JobDone, JobSpec, JobStatus
from app.evaluation.models import EvalEvaluator, EvalResult
from app.optimization import routing, traffic
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeExecutor

WS = ws_ctx({"artifacts_bucket": "launchpad-artifacts"})
AGENT_META = {
    "id": "agent1", "name": "agent",
    "arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r-1",
    "resource_id": "r-1", "runtime_name": "r",
}


def _owner(execution_status: str = "RUNNING") -> routing.OwnerRef:
    """A canary past setup with a live A/B row (the only owner kind since N13-obs2)."""
    db = SessionLocal()
    try:
        row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CAN",
                            champion_agent_id="agent1", champion_agent_name="agent",
                            challenger_agent_id="agent1", challenger_agent_name="agent",
                            artifacts={"agent_meta": AGENT_META,
                                       "setup": {"stable_endpoint": "stablexyz000",
                                                 "treatment_endpoint": "treatxyz000",
                                                 "runtime_id": "r-1"},
                                       "abtest": {"ab_test_id": "local_x",
                                                  "variants": routing.target_variants(
                                                      "stablexyz000", "treatxyz000", 50, 50),
                                                  "execution_status": execution_status}})
        db.add(row)
        db.commit()
        owner = routing.active_owner(db, "agent1")
    finally:
        db.close()
    assert owner is not None
    return owner


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(traffic, "_sleep", lambda *_a, **_k: None)


# ─── plan ────────────────────────────────────────────────────────────────────
def test_plan_builds_one_pre_assigned_session_per_prompt():
    owner = _owner()
    items = [("scn-1", "hello"), ("scn-2", "what time is it"), ("item_3", "bye")]
    spec, assigned = traffic.plan(
        WS, AGENT_META, items, owner,
        evaluators=["Builtin.Helpfulness", "Builtin.Refusal"],
    )
    assert isinstance(spec, JobSpec)
    assert spec.job_type == "batch_eval"
    assert len(spec.job_id) <= 16  # fits EvalResult.run_id
    assert spec.workspace == {"account_id": WS.account_id, "region": WS.region}
    assert spec.sink == {"bucket": "launchpad-artifacts",
                         "prefix": f"evaluations/{spec.job_id}/a1/"}
    body = spec.spec
    assert body["run_id"] == spec.job_id
    assert body["replay"] is True
    assert body["target"] == {
        "runtime_arn": AGENT_META["arn"], "protocol": "http",
        "runtime_user_id": traffic.TRAFFIC_USER_ID, "actor_id": traffic.TRAFFIC_USER_ID,
    }
    assert body["invoke_concurrency"] == traffic.TRAFFIC_MAX_CONCURRENCY
    assert body["evaluator_defs"] == [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.Refusal"}]
    assert body["wait"]["log_groups"] == [
        "/aws/bedrock-agentcore/runtimes/r-1-stablexyz000",
        "/aws/bedrock-agentcore/runtimes/r-1-treatxyz000", "aws/spans"]
    assert body["owner"] == {"kind": "canary", "id": owner.id}
    sessions = body["sessions"]
    assert [s["scenario_id"] for s in sessions] == ["scn-1", "scn-2", "item_3"]
    assert [s["prompts"] for s in sessions] == [["hello"], ["what time is it"], ["bye"]]
    assert len({s["session_id"] for s in sessions}) == 3
    assert all(len(s["session_id"]) >= 33 for s in sessions)
    for s in sessions:
        a = assigned[s["session_id"]]
        assert s["arm"] == a["arm"] and s["baggage"] == a["baggage"]
        endpoint = "treatxyz000" if s["arm"] == "T1" else "stablexyz000"
        assert s["qualifier"] == endpoint  # canary arms ride their named endpoint
        assert "bundle" not in s["launchpad_meta"]
        assert s["content_log_group"] == f"/aws/bedrock-agentcore/runtimes/r-1-{endpoint}"
    # the assignments were written BEFORE dispatch — the table is authoritative
    db = SessionLocal()
    try:
        rows = db.query(ExperimentAssignment).filter_by(owner_id=owner.id).all()
    finally:
        db.close()
    assert {r.session_id: r.arm for r in rows} == {k: v["arm"] for k, v in assigned.items()}


def test_plan_resolves_custom_evaluator_rows_into_definitions():
    owner = _owner()
    db = SessionLocal()
    try:
        db.add(EvalEvaluator(id="cust_abc123def456", workspace_id=DEFAULT_WORKSPACE_ID,
                             name="tone", level="TRACE", definition="judge",
                             instructions="Rate the tone",
                             rating_scale=[{"label": "ok", "value": 1}],
                             model_id="global.zai.glm-5.3"))
        db.commit()
    finally:
        db.close()
    spec, _ = traffic.plan(WS, AGENT_META, [("s", "p")], owner,
                           evaluators=["cust_abc123def456", "Builtin.Helpfulness"])
    defs = spec.spec["evaluator_defs"]
    assert defs[0]["id"] == "cust_abc123def456"
    assert defs[0]["definition"] == "judge" and defs[0]["instructions"] == "Rate the tone"
    assert defs[0]["rating_scale"] == [{"label": "ok", "value": 1}]
    assert defs[1] == {"id": "Builtin.Helpfulness"}


def test_plan_unknown_custom_evaluator_is_an_error():
    owner = _owner()
    with pytest.raises(ValueError, match="cust_missing"):
        traffic.plan(WS, AGENT_META, [("s", "p")], owner, evaluators=["cust_missing"])


def test_plan_with_no_items_is_an_error():
    owner = _owner()
    with pytest.raises(ValueError, match="no prompts"):
        traffic.plan(WS, AGENT_META, [], owner, evaluators=["Builtin.Helpfulness"])


def test_plan_canary_sessions_carry_their_endpoint_qualifier_and_arm_log_groups():
    variants = routing.target_variants("stableabc123", "treatabc123")
    db = SessionLocal()
    try:
        row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CAN",
                            champion_agent_id="agent1", champion_agent_name="a",
                            challenger_agent_id="agent1", challenger_agent_name="a",
                            artifacts={"agent_meta": AGENT_META,
                                       "setup": {"stable_endpoint": "stableabc123",
                                                 "treatment_endpoint": "treatabc123",
                                                 "runtime_id": "r-1"},
                                       "abtest": {"ab_test_id": "local_can",
                                                  "variants": variants,
                                                  "execution_status": "RUNNING"}})
        db.add(row)
        db.commit()
        owner = routing.active_owner(db, "agent1")
    finally:
        db.close()
    spec, assigned = traffic.plan(WS, AGENT_META, [(f"s{i}", "p") for i in range(40)], owner,
                                  evaluators=["Builtin.Helpfulness"])
    quals = {s["qualifier"] for s in spec.spec["sessions"]}
    assert quals <= {"stableabc123", "treatabc123"} and "stableabc123" in quals
    for s in spec.spec["sessions"]:
        expected = "treatabc123" if s["arm"] == "T1" else "stableabc123"
        assert s["qualifier"] == expected
        assert "bundle" not in s["launchpad_meta"]
        # N-SCN-bug3: the worker waits for THIS session's content in its arm's own group
        assert s["content_log_group"] == f"/aws/bedrock-agentcore/runtimes/r-1-{expected}"
    assert spec.spec["wait"]["log_groups"] == [
        "/aws/bedrock-agentcore/runtimes/r-1-stableabc123",
        "/aws/bedrock-agentcore/runtimes/r-1-treatabc123",
        "aws/spans",
    ]


# ─── run: submit → poll → ingest → reconcile ─────────────────────────────────
def _records(spec: JobSpec, *, skip: set[str] = frozenset()) -> list[dict]:
    out = []
    for s in spec.spec["sessions"]:
        if s["session_id"] in skip:
            continue
        out.append({"id": f"rec-{s['session_id'][:8]}", "session_id": s["session_id"],
                    "trace_id": None, "evaluator_id": "Builtin.Helpfulness", "level": "TRACE",
                    "score": 0.9 if s["arm"] == "T1" else 0.5, "label": "good",
                    "explanation": "fine", "error_type": None, "error_message": None})
    return out


def test_run_dispatches_once_mirrors_progress_ingests_and_reconciles():
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [(f"s{i}", f"p{i}") for i in range(4)],
                                  owner, evaluators=["Builtin.Helpfulness"])
    executor = FakeExecutor()
    progress: list[str] = []
    planned = list(assigned)
    never_invoked = planned[3]

    polls = {"n": 0}
    real_status = executor.status

    def status(ws, job_id):
        polls["n"] += 1
        if polls["n"] == 1:
            executor.statuses[job_id] = JobStatus(
                phase="waiting", progress={"done": 2, "total": 4, "message": "waiting for spans"})
        elif polls["n"] == 2:
            executor.complete(job_id, _records(spec, skip={never_invoked}),
                              summary={"scores": [{"evaluatorId": "Builtin.Helpfulness",
                                                   "score": 0.7, "count": 3}]})
            executor.invocations = lambda ws, jid: [  # the optional invocations channel
                {"session_id": sid, "scenario_id": f"s{i}", "arm": assigned[sid]["arm"],
                 "status": "ok" if i < 2 else "failed", "code": None}
                for i, sid in enumerate(planned) if sid != never_invoked
            ]
        return real_status(ws, job_id)

    executor.status = status  # type: ignore[method-assign]
    result = traffic.run(WS, owner, spec, assigned, executor=executor,
                         progress=progress.append, config_id="oe-1")

    assert [j.job_id for j in executor.submitted] == [spec.job_id]
    assert any("waiting" in p and "2/4" in p for p in progress)
    assert result["job_id"] == spec.job_id
    assert result["job_state"] == "COMPLETED"
    assert result["sent"] == 2 and result["failed"] == 1
    assert result["status_counts"] == {"ok": 2, "failed": 1}
    assert result["session_ids"] == planned[:2]  # input order, invoked-ok only
    assert result["result_count"] == 3
    assert result["dropped_sessions"] == [never_invoked]
    # per-arm counts of the invoked sessions (zero-count arms are omitted)
    from collections import Counter

    assert result["arms"] == dict(Counter(assigned[s]["arm"] for s in planned[:3]))
    assert result["summary"]["scores"][0]["evaluatorId"] == "Builtin.Helpfulness"
    db = SessionLocal()
    try:
        rows = db.query(EvalResult).order_by(EvalResult.session_id).all()
        kept = {r.session_id for r in db.query(ExperimentAssignment).filter_by(owner_id=owner.id)}
    finally:
        db.close()
    assert len(rows) == 3
    assert {r.session_id for r in rows} == set(planned[:3])
    assert all(r.source == "run" and r.run_id == spec.job_id and r.config_id == "oe-1"
               for r in rows)
    assert all(r.arm == assigned[r.session_id]["arm"] for r in rows)  # arm stamped at ingest
    assert all(r.workspace_id == DEFAULT_WORKSPACE_ID for r in rows)
    assert kept == set(planned[:3])  # the never-invoked assignment is gone


def test_run_without_an_invocations_channel_derives_invocations_from_results():
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [("a", "p"), ("b", "p"), ("c", "p")], owner,
                                  evaluators=["Builtin.Helpfulness"])
    planned = list(assigned)
    executor = FakeExecutor()
    executor.complete(spec.job_id, _records(spec, skip={planned[2]}))
    result = traffic.run(WS, owner, spec, assigned, executor=executor, progress=lambda _m: None)
    assert [j.job_id for j in executor.submitted] == [spec.job_id]
    assert result["sent"] == 2 and result["failed"] == 0
    assert result["status_counts"] == {"ok": 2}
    assert result["dropped_sessions"] == [planned[2]]


def test_run_ingest_is_idempotent_across_polls():
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [("a", "p"), ("b", "p")], owner,
                                  evaluators=["Builtin.Helpfulness"])
    executor = FakeExecutor()
    records = _records(spec)
    calls = {"n": 0}
    real_status = executor.status

    def status(ws, job_id):
        calls["n"] += 1
        if calls["n"] == 1:
            executor.results[job_id] = records[:1]  # a partial flush, then the rest
        else:
            executor.complete(job_id, records)
        return real_status(ws, job_id)

    executor.status = status  # type: ignore[method-assign]
    traffic.run(WS, owner, spec, assigned, executor=executor, progress=lambda _m: None)
    db = SessionLocal()
    try:
        assert db.query(EvalResult).count() == 2
    finally:
        db.close()


def test_run_failed_job_raises_with_the_worker_error_and_keeps_partial_rows():
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [("a", "p"), ("b", "p")], owner,
                                  evaluators=["Builtin.Helpfulness"])
    executor = FakeExecutor()
    real_status = executor.status

    def status(ws, job_id):
        executor.results[job_id] = _records(spec)[:1]
        executor.dones[job_id] = JobDone(state="FAILED", error="judge exploded", result_count=1)
        return real_status(ws, job_id)

    executor.status = status  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="judge exploded") as info:
        traffic.run(WS, owner, spec, assigned, executor=executor, progress=lambda _m: None)
    db = SessionLocal()
    try:
        assert db.query(EvalResult).count() == 1
    finally:
        db.close()
    # N-SCN-bug3: the error carries what DID complete so the caller can record the attempt
    assert isinstance(info.value, traffic.ReplayJobFailed)
    partial = info.value.result
    assert partial["job_id"] == spec.job_id and partial["job_state"] == "FAILED"
    assert partial["sent"] == 1 and partial["result_count"] == 1
    assert partial["session_ids"] == [spec.spec["sessions"][0]["session_id"]]


def test_run_times_out_cancels_and_raises(monkeypatch):
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [("a", "p")], owner,
                                  evaluators=["Builtin.Helpfulness"])
    executor = FakeExecutor()
    clock = {"t": 0.0}
    monkeypatch.setattr(traffic, "_monotonic", lambda: clock["t"])
    monkeypatch.setattr(traffic, "_sleep", lambda _s: clock.__setitem__("t", clock["t"] + 100.0))
    with pytest.raises(TimeoutError):
        traffic.run(WS, owner, spec, assigned, executor=executor,
                    progress=lambda _m: None, budget_s=250)
    assert executor.cancelled == [spec.job_id]
    assert traffic._monotonic is not time.monotonic  # the patch was in force during the run


def test_run_stopped_job_keeps_what_arrived_and_reports_the_state():
    owner = _owner()
    spec, assigned = traffic.plan(WS, AGENT_META, [("a", "p"), ("b", "p")], owner,
                                  evaluators=["Builtin.Helpfulness"])
    executor = FakeExecutor()
    executor.results[spec.job_id] = _records(spec)[:1]
    executor.dones[spec.job_id] = JobDone(state="STOPPED", result_count=1)
    result = traffic.run(WS, owner, spec, assigned, executor=executor, progress=lambda _m: None)
    assert result["job_state"] == "STOPPED" and result["result_count"] == 1


# ─── dataset items ───────────────────────────────────────────────────────────
def test_items_from_prompts_labels_each_prompt():
    assert traffic.items_from_prompts(["a", "b"]) == [("item_1", "a"), ("item_2", "b")]


def test_a_multi_turn_scenario_replays_every_turn_in_one_session():
    # N17-obs321: canary 发送测试流量 used to send only a scenario's first turn while its
    # truth described the whole conversation — every turn now rides in the session's prompts
    from types import SimpleNamespace

    dataset = SimpleNamespace(kind="predefined", items=[
        {"scenario_id": "mt", "turns": [{"input": "第一问"}, {"input": {"content": "第二问"}},
                                        {"input": "  "}, {"input": "第三问"}]},
        {"scenario_id": "single", "turns": [{"input": " 只有一问 "}]},
    ])
    items = traffic.resolve_traffic_items(dataset)
    assert items == [("mt", ["第一问", "第二问", "第三问"]), ("single", "只有一问")]
    spec, _ = traffic.plan(WS, AGENT_META, items, _owner(), evaluators=["Builtin.Helpfulness"])
    assert [s["prompts"] for s in spec.spec["sessions"]] == [["第一问", "第二问", "第三问"],
                                                              ["只有一问"]]
    assert [s["scenario_id"] for s in spec.spec["sessions"]] == ["mt", "single"]
