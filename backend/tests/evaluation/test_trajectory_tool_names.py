"""N17-obs407 (eval Round 3 D3-a, DIFF-GLOBAL-EVAL B4): an ``expected_trajectory`` names the
tools as the agent calls them — checked at dataset create / edit against the linked agent and
at run start against the run's agent; the one other Gateway spelling is rewritten to the
canonical name, anything else is 422 with the agent's tool names."""

import json

from app.core.db import SessionLocal
from app.evaluation import agent_tools
from app.evaluation.models import EvalDataset, EvalRun
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal

HR = "hr-database___get_employee"
HR_RUNTIME = "launchpad_gw_" + HR


def _harness_spec(name: str = "hr-agent") -> dict:
    return {
        "name": name, "method": "harness",
        "tools": [
            {"type": "gateway", "name": "hr-database", "config": {"tools": [HR]}},
            {"type": "builtin", "name": "code-interpreter"},
        ],
        "knowledge_bases": [{"kb_id": "KB1", "name": "policies"}],
    }


def _agent(spec: dict, method: str) -> str:
    db = SessionLocal()
    try:
        agent = make_agent(db, name=spec["name"], method=method)
        agent.spec = spec
        db.commit()
        return agent.id
    finally:
        db.close()


def _scenario(trajectory: list[str], sid: str = "s1") -> dict:
    return {"scenario_id": sid, "turns": [{"input": "who is E1?"}],
            "expected_trajectory": trajectory}


# ── the catalogue ──────────────────────────────────────────────────────────
def test_harness_catalogue_prefixes_gateway_tools_and_adds_support_tools():
    tools = agent_tools.agent_tools(_harness_spec())
    assert {HR_RUNTIME, "code_interpreter", "kb_search", "kb_deep_search"} <= tools.names
    assert HR not in tools.names and not tools.open
    assert tools.canonical(HR) == HR_RUNTIME  # the declared spelling gains the alias


def test_zip_runtime_catalogue_keeps_gateway_names_bare():
    spec = {"name": "zr", "method": "zip_runtime",
            "tools": [{"type": "gateway", "name": "hr-database", "config": {"tools": [HR]}},
                      {"type": "rest", "name": "pay-api", "config": {"url": "https://x.cn"}}]}
    tools = agent_tools.agent_tools(spec)
    assert {HR, "pay_api"} <= tools.names
    assert tools.canonical(HR_RUNTIME) == HR  # the alias is dropped for a generated runtime


def test_undeclared_gateway_ref_is_a_target_pattern():
    spec = {"name": "h", "method": "harness",
            "tools": [{"type": "gateway", "name": "hr-database"}]}
    tools = agent_tools.agent_tools(spec)
    assert tools.knows(HR_RUNTIME) and not tools.knows("launchpad_gw_other___x")
    assert tools.canonical(HR) == HR_RUNTIME


def test_open_catalogues_check_nothing():
    for spec in ({"name": "b", "method": "byoc"}, {"name": "s", "method": "studio"},
                 {"name": "c", "method": "zip_runtime", "code": "print(1)"}):
        tools = agent_tools.agent_tools(spec)
        assert tools.open
        check = agent_tools.check_trajectories([_scenario(["anything"])], tools)
        assert not check.unknown and not check.rewrites


# ── dataset create / edit ──────────────────────────────────────────────────
def test_create_linked_dataset_stores_the_runtime_name(client):
    agent_id = _agent(_harness_spec(), "harness")
    res = client.post("/api/eval/datasets", json={
        "name": "hr-golden", "agent_id": agent_id,
        "items": [_scenario([HR, "kb_search"])]})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["agent_id"] == agent_id
    assert body["items"][0]["expected_trajectory"] == [HR_RUNTIME, "kb_search"]
    assert body["tool_name_rewrites"] == [{"scenario_id": "s1", "from": HR, "to": HR_RUNTIME}]


def test_create_linked_dataset_refuses_an_unknown_tool(client):
    agent_id = _agent(_harness_spec(), "harness")
    res = client.post("/api/eval/datasets", json={
        "name": "hr-bad", "agent_id": agent_id,
        "items": [_scenario(["kb_serch", "code_interpreter"])]})
    assert res.status_code == 422
    body = res.json()
    assert body["code"] == "dataset.unknown_tool"
    assert body["detail"]["unknown"] == {"s1": ["kb_serch"]}
    assert HR_RUNTIME in body["detail"]["agent_tools"]
    assert "kb_search" in body["detail"]["suggestions"]["kb_serch"]
    db = SessionLocal()
    try:
        assert db.query(EvalDataset).filter_by(name="hr-bad").count() == 0
    finally:
        db.close()


def test_upload_linked_dataset_is_checked_too(client):
    agent_id = _agent(_harness_spec(), "harness")
    res = client.post("/api/eval/datasets/upload", json={
        "name": "hr-up", "agent_id": agent_id,
        "jsonl": json.dumps(_scenario(["nope"]))})
    assert res.status_code == 422 and res.json()["code"] == "dataset.unknown_tool"


def test_linking_an_agent_rechecks_the_stored_items(client):
    agent_id = _agent(_harness_spec(), "harness")
    created = client.post("/api/eval/datasets", json={
        "name": "hr-later", "items": [_scenario([HR])]}).json()
    assert created["agent_id"] is None  # no link → stored as written
    assert created["items"][0]["expected_trajectory"] == [HR]
    res = client.put(f"/api/eval/datasets/{created['id']}", json={"agent_id": agent_id})
    assert res.status_code == 200, res.text
    assert res.json()["items"][0]["expected_trajectory"] == [HR_RUNTIME]
    assert res.json()["agent_id"] == agent_id
    bad = client.put(f"/api/eval/datasets/{created['id']}",
                     json={"items": [_scenario(["ghost"])]})
    assert bad.status_code == 422 and bad.json()["code"] == "dataset.unknown_tool"
    unlinked = client.put(f"/api/eval/datasets/{created['id']}", json={"agent_id": ""})
    assert unlinked.status_code == 200 and unlinked.json()["agent_id"] is None


def test_linking_a_missing_agent_is_refused(client):
    res = client.post("/api/eval/datasets", json={
        "name": "x", "agent_id": "nope", "items": [{"prompt": "hi"}]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.agent_not_found"


# ── run start ──────────────────────────────────────────────────────────────
def test_run_start_refuses_names_the_run_agent_lacks(client, monkeypatch):
    stub_environment(monkeypatch)
    agent_id = _agent(_harness_spec("hr-run-bad"), "harness")
    ds = client.post("/api/eval/datasets", json={
        "name": "unlinked", "items": [_scenario(["employee_lookup"])]}).json()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds["id"],
        "evaluators": ["Builtin.TrajectoryExactOrderMatch"], "wait_seconds": 0})
    assert res.status_code == 422
    assert res.json()["code"] == "run.unknown_tool"
    assert res.json()["detail"]["unknown"] == {"s1": ["employee_lookup"]}
    db = SessionLocal()
    try:
        assert db.query(EvalRun).filter_by(agent_id=agent_id).count() == 0
    finally:
        db.close()


def test_run_start_replays_the_canonical_name_and_records_the_rewrite(client, monkeypatch):
    env, _calls = stub_environment(monkeypatch)
    agent_id = _agent(_harness_spec("hr-run"), "harness")
    ds = client.post("/api/eval/datasets", json={
        "name": "unlinked-ok", "items": [_scenario([HR])]}).json()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds["id"],
        "evaluators": ["Builtin.TrajectoryExactOrderMatch"], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["tool_name_rewrites"] == [{"scenario_id": "s1", "from": HR, "to": HR_RUNTIME}]
    (job,) = env.submitted
    truth = json.dumps(job.spec["ground_truth"])
    assert HR_RUNTIME in truth and f'"{HR}"' not in truth
    # the dataset keeps what its author wrote (no link: nothing to store canonically)
    assert client.get("/api/eval/datasets").json()["datasets"][0]["items"][0][
        "expected_trajectory"] == [HR]


# ── never silent: ambiguity, reporting, open catalogues (lead notes) ──────
def test_a_bare_tool_name_shared_by_two_targets_is_ambiguous_and_refused(client):
    spec = {"name": "two-targets", "method": "harness", "tools": [
        {"type": "gateway", "name": "hr-database", "config": {"tools": [HR]}},
        {"type": "gateway", "name": "payroll", "config": {"tools": ["payroll___get_employee"]}}]}
    agent_id = _agent(spec, "harness")
    res = client.post("/api/eval/datasets", json={
        "name": "ambiguous", "agent_id": agent_id, "items": [_scenario(["get_employee"])]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.unknown_tool"
    # no suffix guessing: both candidates are offered, neither is picked
    assert set(res.json()["detail"]["suggestions"]["get_employee"]) <= {
        HR_RUNTIME, "launchpad_gw_payroll___get_employee"}


def test_a_prefix_only_mismatch_is_rewritten_and_reported_on_the_run(client, monkeypatch):
    stub_environment(monkeypatch)
    agent_id = _agent(_harness_spec("hr-report"), "harness")
    ds = client.post("/api/eval/datasets", json={
        "name": "report", "items": [_scenario([HR])]}).json()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds["id"],
        "evaluators": ["Builtin.TrajectoryExactOrderMatch"], "wait_seconds": 0})
    assert res.status_code == 201
    assert res.json()["tool_name_rewrites"] == [
        {"scenario_id": "s1", "from": HR, "to": HR_RUNTIME}]  # in the 201 itself
    run = wait_terminal(client, res.json()["id"])
    assert run["tool_name_rewrites"][0]["to"] == HR_RUNTIME
    assert run["warnings"] == []  # a checked catalogue gets no post-run hint


def test_an_open_agent_gets_a_warning_for_a_tool_its_spans_never_show(client, monkeypatch):
    stub_environment(monkeypatch)
    agent_id = _agent({"name": "byoc-graph", "method": "byoc"}, "byoc")
    ds = client.post("/api/eval/datasets", json={
        "name": "open", "items": [_scenario(["ghost_tool"])]}).json()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds["id"],
        "evaluators": ["Builtin.TrajectoryExactOrderMatch"], "wait_seconds": 0})
    assert res.status_code == 201, res.text  # open: never blocked
    run = wait_terminal(client, res.json()["id"])
    (warning,) = run["warnings"]
    assert warning["kind"] == "expected_tool_not_observed"
    assert warning["scenario_id"] == "s1" and warning["tools"] == ["ghost_tool"]
    assert "never appear" in warning["message"]


def test_an_older_worker_without_observed_tools_means_no_warnings():
    from app.evaluation import service
    from app.evaluation.jobs import JobDone

    run = EvalRun(agent_id="", agent_name="x", session_ids=["s1"],
                  scenario_map={"s1": {"scenario_id": "a", "expected_tools": ["t"]}})
    assert service.trajectory_warnings(run, [JobDone(state="COMPLETED", summary={})]) is None
    warned = service.trajectory_warnings(run, [JobDone(state="COMPLETED", summary={
        "observed_tools": {"s1": ["other"]}})])
    assert warned[0]["tools"] == ["t"]
    seen = service.trajectory_warnings(run, [JobDone(state="COMPLETED", summary={
        "observed_tools": {"s2": ["t"]}})])  # seen in ANOTHER session: a naming hint is moot
    assert seen == []
