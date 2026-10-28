"""The Agent-DLC console API end to end: define → evaluate → gate → sign, with roles."""

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.dlc import engine as engine_svc
from app.evaluation.models import EvalDataset, EvalRun
from app.main import create_app
from app.models.dlc import (
    AdmissionCandidate,
    AnnotationTask,
    CalibrationRecord,
    CriteriaSet,
    Criterion,
    CriterionResult,
    ReleaseRecord,
    Waiver,
    WatchConfig,
)
from app.models.ledger import Agent, Workspace
from app.services import users as users_service

WS = DEFAULT_WORKSPACE_ID
ADMIN_CREDS = {"username": "root", "password": "Admin-pw-1!"}
BIZ = {"username": "biz-owner", "email": "biz@example.com", "password": "Biz-pw-12!"}
ENG = {"username": "engineer", "email": "eng@example.com", "password": "Eng-pw-123!"}


def _clean() -> None:
    db = SessionLocal()
    try:
        for model in (CriterionResult, ReleaseRecord, Waiver, AdmissionCandidate, WatchConfig,
                      CalibrationRecord, AnnotationTask, EvalRun, Criterion, CriteriaSet):
            db.query(model).delete()
        db.query(EvalDataset).filter(EvalDataset.role == "golden").delete()
        db.query(Agent).filter(Agent.name.like("api-%")).delete()
        ws = db.get(Workspace, WS)
        ws.release_policy = {}
        ws.tier = "dev"
        db.commit()
    finally:
        db.close()


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    # the router asks AWS what kind a custom evaluator is; here `code-*` are code
    # assertions and everything else falls back to the judge default
    monkeypatch.setattr("app.routers.dlc.control_client", lambda ctx: object())
    monkeypatch.setattr(
        "app.evaluation.agentcore_eval.get_evaluator",
        lambda client, evaluator_id: {
            "evaluator": {"evaluatorConfig": {"codeBased": {"lang": "python"}}}
            if evaluator_id.startswith("code-") else {"derived": {}}
        },
    )
    _clean()
    yield create_app()
    get_settings.cache_clear()
    _clean()


def _user(client, creds, permissions):
    assert client.post("/api/auth/register", json=creds).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, creds["username"])
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [WS])
        # explicit per-user grants: these keys are given to named people, not by role
        user.permissions = {key: (key in permissions)
                            for key in users_service.AGENT_PERMISSIONS}
        db.commit()
    finally:
        db.close()
    assert client.post("/api/auth/login", json={
        "username": creds["username"], "password": creds["password"]}).status_code == 200
    return client


@pytest.fixture
def admin(app):
    with TestClient(app, client=("127.0.0.1", 4321)) as client:
        assert client.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        yield client


@pytest.fixture
def engineer(app):
    """Holds criteria.manage and eval.run, but cannot sign or admit."""
    with TestClient(app, client=("127.0.0.1", 4321)) as client:
        yield _user(client, ENG, ["criteria.manage", "eval.run", "agents.deploy"])


@pytest.fixture
def owner(app):
    """The business owner: signs criteria and admits samples, edits nothing."""
    with TestClient(app, client=("127.0.0.1", 4321)) as client:
        yield _user(client, BIZ, ["criteria.sign", "golden.admit", "judge.calibrate"])


def _agent(name="api-a", version="2", endpoint_mode="default"):
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=WS, name=name, method="harness", status="active",
                      spec={"name": name}, version=version, resource_id="h-api",
                      endpoint_mode=endpoint_mode, owner="engineer")
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


CRITERIA = [
    {"key": "R1", "text": "agent never reveals another account's price",
     "dimension": "responsibility", "tier": "redline",
     "executor": {"kind": "evaluator", "evaluator_id": "code-pii"},
     "notes": "n/a:cognition n/a:cost n/a:performance (fixture)"},
    {"key": "G1", "text": "agent states the stock status", "dimension": "quality",
     "tier": "gate", "threshold": 0.9,
     "executor": {"kind": "evaluator", "evaluator_id": "code-stock"}},
]


def _create_set(client, agent_id):
    res = client.post("/api/criteria-sets", json={
        "kind": "agent", "agent_id": agent_id, "name": "客服判据"})
    assert res.status_code == 201, res.text
    return res.json()["set"]["lineage_id"]


def test_criteria_lifecycle_and_separation_of_duties(engineer, owner, admin):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    saved = engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["summary"]["declared_gates"] == 1 and body["summary"]["tiers"]["redline"] == 1
    assert [c["key"] for c in body["criteria"]] == ["R1", "G1"]

    # an engineer may not sign
    assert engineer.post(f"/api/criteria-sets/{lineage}/sign", json={}).status_code == 403
    assert engineer.post(f"/api/criteria-sets/{lineage}/publish", json={}).status_code == 200
    signed = owner.post(f"/api/criteria-sets/{lineage}/sign", json={"note": "ok"})
    assert signed.status_code == 200, signed.text
    assert signed.json()["set"]["signed_by"] == BIZ["username"]

    # a published version is frozen; editing opens v2
    assert engineer.put(f"/api/criteria-sets/{lineage}",
                        json={"criteria": CRITERIA}).status_code == 409
    v2 = engineer.post(f"/api/criteria-sets/{lineage}/versions", json={})
    assert v2.status_code == 201 and v2.json()["set"]["version"] == 2
    harder = [dict(CRITERIA[0]), {**CRITERIA[1], "threshold": 0.97}]
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": harder})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    diff = engineer.get(f"/api/criteria-sets/{lineage}/diff", params={"a": 1, "b": 2})
    assert diff.json()["changes"] == [
        {"key": "G1", "change": "changed", "fields": ["threshold"],
         "before": {"threshold": 0.9}, "after": {"threshold": 0.97}}]

    # the decision history is readable
    events = admin.get("/api/audit", params={"action": "criteria."}).json()["events"]
    assert any(e["action"] == "criteria.sign" for e in events)


def test_a_red_line_on_a_judge_is_refused_with_findings(engineer):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    bad = [{**CRITERIA[0], "executor": {"kind": "evaluator",
                                        "evaluator_id": "Builtin.Helpfulness"}}]
    res = engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": bad})
    assert res.status_code == 422
    codes = {f["code"] for f in res.json()["detail"]["findings"]}
    assert "criteria.redline_judge" in codes


def test_templates_are_shared_and_adopted_explicitly(engineer, owner):
    template = engineer.post("/api/criteria-sets", json={
        "kind": "template", "name": "工业品客服", "scenario": "industrial"}).json()
    lineage_t = template["set"]["lineage_id"]
    engineer.put(f"/api/criteria-sets/{lineage_t}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage_t}/publish", json={})

    agent_id = _agent()
    res = engineer.post("/api/criteria-sets", json={
        "kind": "agent", "agent_id": agent_id, "name": "a",
        "template_lineage_id": lineage_t, "template_version": 1})
    assert res.status_code == 201
    lineage_a = res.json()["set"]["lineage_id"]
    assert {c["origin"] for c in res.json()["criteria"]} == {"template"}

    # the template moves on; the agent set is told, not changed
    engineer.post(f"/api/criteria-sets/{lineage_t}/versions", json={})
    engineer.put(f"/api/criteria-sets/{lineage_t}", json={
        "criteria": CRITERIA + [{"key": "G2", "text": "agent cites the policy",
                                 "dimension": "quality", "tier": "observe",
                                 "executor": {"kind": "evaluator",
                                              "evaluator_id": "code-cite"}}]})
    engineer.post(f"/api/criteria-sets/{lineage_t}/publish", json={})
    engineer.post(f"/api/criteria-sets/{lineage_a}/publish", json={})
    before = engineer.get(f"/api/criteria-sets/{lineage_a}").json()
    assert before["newer_template_version"] == 2
    assert [c["key"] for c in before["criteria"]] == ["R1", "G1"]
    adopted = engineer.post(f"/api/criteria-sets/{lineage_a}/adopt-template",
                            params={"template_version": 2}, json={})
    assert adopted.status_code == 201
    assert [c["key"] for c in adopted.json()["criteria"]] == ["R1", "G1", "G2"]
    assert adopted.json()["set"]["signed_by"] is None  # a new ruler needs a new signature


def _create_bare(client, lineage):
    return client.post("/api/golden-sets",
                       json={"name": "bare", "criteria_lineage_id": lineage}).json()["id"]


def _golden(client, lineage, items=2, *, seeder=None):
    """A golden set whose three splits are curated, as a release gate requires."""
    res = client.post("/api/golden-sets", json={"name": "g", "criteria_lineage_id": lineage})
    assert res.status_code == 201, res.text
    dataset_id = res.json()["id"]
    seeded = (seeder or client).post(f"/api/golden-sets/{dataset_id}/seed", json={
        "items": [{"split": "holdout", "scenario_id": f"h{n}",
                   "turns": [{"input": f"hq{n}", "expected_response": f"ha{n}"}]}
                  for n in range(items)],
    })
    assert seeded.status_code == 201, seeded.text
    body = client.post(f"/api/golden-sets/{dataset_id}/items", json={
        "split": "regression",
        "items": [{"scenario_id": f"r{n}",
                   "turns": [{"input": f"q{n}", "expected_response": f"a{n}"}]}
                  for n in range(items)],
    })
    assert body.status_code == 201, body.text
    return dataset_id


def test_golden_splits_coverage_and_the_closed_holdout(engineer, owner):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    assert engineer.post(f"/api/golden-sets/{_create_bare(engineer, lineage)}/seed",
                         json={"items": [{"scenario_id": "x",
                                          "turns": [{"input": "q"}]}]}).status_code == 403
    dataset_id = _golden(engineer, lineage, seeder=owner)
    body = engineer.get(f"/api/golden-sets/{dataset_id}").json()
    assert set(body["splits"]) == {"dev", "regression", "holdout"}
    assert body["splits"]["regression"]["active_items"] == 2
    assert [r["key"] for r in body["coverage"]["criteria"]] == ["R1", "G1"]
    assert all(r["thin"] for r in body["coverage"]["criteria"])  # no items mapped yet

    moved = engineer.post(f"/api/golden-sets/{dataset_id}/move",
                          json={"scenario_id": "r0", "to": "dev"})
    assert moved.json()["splits"]["dev"]["items"] == 1
    retired = engineer.post(f"/api/golden-sets/{dataset_id}/retire", json={
        "split": "regression", "scenario_id": "r1", "reason": "superseded"})
    assert retired.json()["splits"]["regression"]["active_items"] == 0
    # the API offers no way to write the holdout split
    assert engineer.post(f"/api/golden-sets/{dataset_id}/items", json={
        "split": "holdout", "items": [{"scenario_id": "x", "turns": [{"input": "q"}]}],
    }).status_code == 422


def _finish_run(run_id, *, verdicts):
    """Complete a run and snapshot the given verdicts as its criterion results."""
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        run.status = "completed"
        db.commit()
        records = [{
            "session.id": scenario,
            "gen_ai.evaluation.name": evaluator,
            "aws.bedrock_agentcore.evaluator.arn": f"arn:x:::evaluator/{evaluator}",
            "aws.bedrock_agentcore.evaluation_level": "Session",
            "gen_ai.evaluation.score.value": 1.0 if passed else 0.0,
            "gen_ai.evaluation.score.label": "PASS" if passed else "FAIL",
            "gen_ai.evaluation.explanation": "because",
        } for scenario, evaluator, passed in verdicts]
    finally:
        db.close()
    engine_svc.finalize_run(run_id, workspace=None, records=records)


def _release_setup(engineer, owner, *, endpoint_mode="live"):
    agent_id = _agent(endpoint_mode=endpoint_mode)
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    owner.post(f"/api/criteria-sets/{lineage}/sign", json={})
    _golden(engineer, lineage, seeder=owner)
    db = SessionLocal()
    try:
        ws = db.get(Workspace, WS)
        ws.release_policy = {"release_mode": "gated"}
        db.commit()
    finally:
        db.close()
    return agent_id, lineage


def test_the_dataset_list_names_a_golden_split_and_its_criteria(engineer, owner):
    """The task wizard preselects a golden split's criteria evaluators from these."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, seeder=owner)
    splits = engineer.get(f"/api/golden-sets/{dataset_id}").json()["splits"]
    listed = {d["id"]: d for d in engineer.get("/api/eval/datasets").json()["datasets"]}
    regression = listed[splits["regression"]["id"]]
    assert (regression["role"], regression["split"], regression["criteria_lineage_id"]) == (
        "golden", "regression", lineage)
    assert listed[dataset_id]["split"] is None
    res = engineer.post("/api/eval/datasets", json={
        "name": "plain", "items": [{"scenario_id": "p", "turns": [{"input": "q"}]}]})
    assert res.status_code in (200, 201), res.text
    plain = {d["id"]: d for d in engineer.get("/api/eval/datasets").json()["datasets"]}[
        res.json()["id"]]
    assert (plain["role"], plain["split"], plain["criteria_lineage_id"]) == ("scratch", None, None)


def test_a_golden_set_is_not_editable_or_deletable_as_a_plain_dataset(engineer, owner):
    """The plain dataset routes would skip admission and rewrite a sealed holdout."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, seeder=owner)
    splits = engineer.get(f"/api/golden-sets/{dataset_id}").json()["splits"]
    holdout = splits["holdout"]["id"]
    forged = [{"scenario_id": "h0", "turns": [{"input": "hq0", "expected_response": "forged"}]}]
    for client in (engineer, owner):
        res = client.put(f"/api/eval/datasets/{holdout}", json={"items": forged})
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "dataset.golden_managed"
        for target in (holdout, dataset_id):
            res = client.delete(f"/api/eval/datasets/{target}")
            assert res.status_code == 409, res.text
    after = engineer.get(f"/api/golden-sets/{dataset_id}").json()["splits"]
    assert after["holdout"]["active_items"] == splits["holdout"]["active_items"]
    # metadata stays editable: a rename touches no item
    assert engineer.put(f"/api/eval/datasets/{holdout}",
                        json={"description": "sealed"}).status_code == 200


def test_an_admin_deletes_a_golden_set_with_its_splits(engineer, owner, admin):
    """The only way out of a sealed holdout is to replace the whole set."""
    from app.core.db import SessionLocal
    from app.evaluation.models import EvalDataset, EvalRun

    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, seeder=owner)
    split_ids = [s["id"] for s in
                 engineer.get(f"/api/golden-sets/{dataset_id}").json()["splits"].values()]
    for client in (engineer, owner):
        assert client.delete(f"/api/golden-sets/{dataset_id}").status_code == 403
    with SessionLocal() as db:
        run = EvalRun(workspace_id=WS, agent_id=agent_id, agent_name="a", name="busy",
                      dataset_id=split_ids[0],
                      status="evaluating")
        db.add(run)
        db.commit()
        run_id = run.id
    busy = admin.delete(f"/api/golden-sets/{dataset_id}")
    assert busy.status_code == 409 and busy.json()["code"] == "golden.delete_busy"
    with SessionLocal() as db:
        db.get(EvalRun, run_id).status = "completed"
        db.commit()
    res = admin.delete(f"/api/golden-sets/{dataset_id}")
    assert res.status_code == 200, res.text
    assert res.json()["items"]["holdout"] == 2
    assert engineer.get(f"/api/golden-sets/{dataset_id}").status_code == 404
    with SessionLocal() as db:
        assert all(db.get(EvalDataset, i) is None for i in [*split_ids, dataset_id])
    # a fresh set for the same criteria can be curated again, holdout included
    assert _golden(engineer, lineage, seeder=owner)


def test_release_gate_blocks_a_red_line_and_signs_a_clean_candidate(engineer, owner, admin,
                                                                   monkeypatch):
    from app.dlc import releases as release_svc

    endpoints: dict[str, str] = {"live": "1"}
    monkeypatch.setattr(release_svc, "point_endpoint",
                        lambda control, agent, name, version: endpoints.update(
                            {name: str(version)}) or {"liveVersion": str(version)})
    monkeypatch.setattr(release_svc, "endpoint_version",
                        lambda control, agent, name: endpoints.get(name))
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    agent_id, lineage = _release_setup(engineer, owner)

    # a deploy opens the release on `candidate`
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        release_svc.after_deploy(db, agent, note=None, actor="engineer")
        db.commit()
    finally:
        db.close()
    state = engineer.get(f"/api/agents/{agent_id}/release").json()
    assert state["release_mode"] == "gated"
    assert state["pending"]["candidate_version"] == "2"
    assert endpoints == {"live": "1", "candidate": "2"}  # production untouched

    submitted = []
    monkeypatch.setattr("app.evaluation.service.submit_run",
                        lambda **kw: submitted.append(kw) or _stub_run(kw))
    assert engineer.post(f"/api/agents/{agent_id}/release/evaluate",
                         json={"repeats": 1}).status_code == 202
    # the gate replays the regression split AND the untouched holdout
    assert {kw["split"] for kw in submitted} == {"regression", "holdout"}
    assert {kw["qualifier"] for kw in submitted} == {"candidate"}
    runs = {kw["split"]: kw["_run_id"] for kw in submitted}

    # while the holdout run is still queued the gate reports progress, not a verdict
    _finish_run(runs["regression"],
                verdicts=[("r0", "code-pii", False), ("r0", "code-stock", True),
                          ("r1", "code-pii", True), ("r1", "code-stock", True)])
    pending = engineer.get(f"/api/agents/{agent_id}/release/gate").json()
    assert pending["status"] == "evaluating"
    _finish_run(runs["holdout"], verdicts=[("h0", "code-pii", True), ("h0", "code-stock", True),
                                           ("h1", "code-pii", True), ("h1", "code-stock", True)])
    gate = engineer.get(f"/api/agents/{agent_id}/release/gate").json()
    assert gate["report"]["verdict"] == "BLOCKED"
    assert gate["report"]["redline_violations"] == ["R1"]
    # re-reading the gate does not move when the verdict was reached
    time.sleep(0.01)
    again = engineer.get(f"/api/agents/{agent_id}/release/gate").json()
    assert again["report"]["decided_at"] == gate["report"]["decided_at"]
    blocked_at = gate["report"]["decided_at"]
    assert engineer.post(f"/api/agents/{agent_id}/release/sign", json={}).status_code == 403
    assert admin.post(f"/api/agents/{agent_id}/release/sign", json={}).status_code == 409

    # fix it: the regression run is replayed clean
    _finish_run(runs["regression"],
                verdicts=[("r0", "code-pii", True), ("r0", "code-stock", True),
                          ("r1", "code-pii", True), ("r1", "code-stock", True)])
    gate = engineer.get(f"/api/agents/{agent_id}/release/gate").json()
    assert gate["report"]["verdict"] == "PASS", [
        (c["key"], c["verdict"], c.get("reason")) for c in gate["report"]["criteria"]]
    assert gate["report"]["decided_at"] != blocked_at  # a new outcome is a new decision
    signed = admin.post(f"/api/agents/{agent_id}/release/sign", json={"note": "ship"})
    assert signed.status_code == 200, signed.text
    assert endpoints["live"] == "2"
    assert signed.json()["records"][0]["decision"] == "released"
    card = engineer.get(f"/api/agents/{agent_id}/scorecard").json()
    assert card["release"]["live_version"] == "2", card["release"]

    rolled = admin.post(f"/api/agents/{agent_id}/release/rollback", json={})
    assert rolled.status_code == 200 and endpoints["live"] == "1"
    # the scorecard tile follows the rollback, not the ledger (still the rolled-back "2")
    card = engineer.get(f"/api/agents/{agent_id}/scorecard").json()
    assert card["release"]["ledger_version"] == "2"
    assert card["release"]["live_version"] == "1", card["release"]


def _session_ids(kw):
    return [str(i.get("scenario_id")) for i in kw["dataset_items"]]


def _stub_run(kw):
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=WS, agent_id=kw["agent"].id, agent_name="a",
                      status="queued", split=kw["split"], repeats=kw["repeats"],
                      criteria_set_id=kw["criteria_set_id"],
                      criteria_set_version=kw["criteria_set_version"],
                      endpoint_qualifier=kw["qualifier"], agent_version=kw["agent_version"],
                      dataset_id=kw["dataset_id"], session_ids=_session_ids(kw),
                      attempts=[{"scenario_id": sid, "attempt": 1, "session_id": sid}
                                for sid in _session_ids(kw)])
        db.add(run)
        db.commit()
        kw["_run_id"] = run.id
        return run
    finally:
        db.close()


def test_waivers_need_a_second_person_and_never_cover_a_red_line(engineer, admin, owner):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    expires = (datetime.now(UTC) + timedelta(days=7)).isoformat()
    red = engineer.post(f"/api/agents/{agent_id}/waivers", json={
        "criterion_key": "R1", "reason": "x", "risk_owner": "r", "expires_on": expires})
    assert red.status_code == 409 and red.json()["code"] == "waiver.redline"
    asked = engineer.post(f"/api/agents/{agent_id}/waivers", json={
        "criterion_key": "G1", "actual": 0.8, "threshold": 0.9, "reason": "vendor outage",
        "risk_owner": "risk-lead", "compensating_control": "manual review",
        "expires_on": expires})
    assert asked.status_code == 201, asked.text
    waiver_id = asked.json()["id"]
    assert owner.post(f"/api/waivers/{waiver_id}/approve", json={}).status_code == 403
    approved = admin.post(f"/api/waivers/{waiver_id}/approve", json={})
    assert approved.status_code == 200 and approved.json()["active"] is True


def test_calibration_blind_then_decided(engineer, owner, admin):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    judge = [CRITERIA[0], {"key": "J1", "text": "agent is polite when refusing",
                           "dimension": "quality", "tier": "gate", "threshold": 0.9,
                           "executor": {"kind": "evaluator",
                                        "evaluator_id": "Builtin.Helpfulness"}}]
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": judge})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    body = engineer.get(f"/api/criteria-sets/{lineage}").json()
    row = next(c for c in body["criteria"] if c["key"] == "J1")
    assert row["effective_tier"] == "observe" and row["calibration"]["reason"] == (
        "never_calibrated")

    items = [{"ref": f"i{n}", "input": f"q{n}", "answer": f"a{n}",
              "judge_label": "pass" if n < 8 else "fail"} for n in range(14)]
    task = engineer.post("/api/annotation-tasks", json={
        "agent_id": agent_id, "criteria_lineage_id": lineage, "criterion_key": "J1",
        "annotators": [ENG["username"], BIZ["username"]], "adjudicator": BIZ["username"],
        "items": items})
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    # an annotator cannot see the judge's label while labelling
    view = engineer.get(f"/api/annotation-tasks/{task_id}").json()
    assert all("judge_label" not in item for item in view["items"])
    for n, item in enumerate(items):
        label = "pass" if n < 8 else "fail"
        for client in (engineer, owner):
            res = client.post(f"/api/annotation-tasks/{task_id}/labels",
                              json={"item_ref": item["ref"], "label": label})
            assert res.status_code == 201, res.text
    agreement = owner.get(f"/api/annotation-tasks/{task_id}/agreement").json()
    assert agreement["judge_human_kappa"] == pytest.approx(1.0)
    assert agreement["suggested_verdict"] == "aligned"
    no_permission = engineer.post(f"/api/annotation-tasks/{task_id}/decide",
                                  json={"verdict": "aligned"})
    assert no_permission.status_code == 403
    assert no_permission.json()["code"] == "auth.permission_required"
    # and the owner holds `judge.calibrate` but labelled this task: the labels are the
    # evidence, so the person who wrote them does not also rule on them
    own_labels = owner.post(f"/api/annotation-tasks/{task_id}/decide",
                            json={"verdict": "aligned"})
    assert own_labels.status_code == 403
    assert own_labels.json()["code"] == "calibration.own_labels"
    decided = admin.post(f"/api/annotation-tasks/{task_id}/decide", json={"verdict": "aligned"})
    assert decided.status_code == 200 and decided.json()["verdict"] == "aligned"
    after = engineer.get(f"/api/criteria-sets/{lineage}").json()
    row = next(c for c in after["criteria"] if c["key"] == "J1")
    assert row["calibrated"] is True and row["effective_tier"] == "gate"


def test_annotation_task_without_lineage_binds_the_agents_criteria(engineer):
    # an API caller that names only the agent still gets a task bound to the agent's
    # published standard; unbound, its verdict would carry no evaluator id and no gate
    # could ever count it
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    published = engineer.get(f"/api/criteria-sets/{lineage}").json()
    task = engineer.post("/api/annotation-tasks", json={
        "agent_id": agent_id, "criterion_key": CRITERIA[0]["key"],
        "annotators": [ENG["username"], BIZ["username"]],
        "items": [{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass"}]})
    assert task.status_code == 201, task.text
    assert task.json()["criteria_set_id"] == published["set"]["id"]


def test_admission_requires_golden_admit_and_a_human_answer(engineer, owner):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    _golden(engineer, lineage, seeder=owner)
    db = SessionLocal()
    try:
        candidate = AdmissionCandidate(workspace_id=WS, agent_id=agent_id, source="feedback",
                                       source_ref="f1", question="do you ship to SG",
                                       answer="no idea", session_id="s1",
                                       redaction={"status": "clean", "entities": []})
        db.add(candidate)
        db.commit()
        candidate_id = candidate.id
    finally:
        db.close()
    queue = engineer.get("/api/admission", params={"agent_id": agent_id}).json()
    assert queue["counts"]["new"] == 1
    payload = {"split": "regression", "expected_response": "yes, 3–5 days",
               "expected_source": "annotator", "criteria_ids": ["G1"],
               "case_tier": "known_bad"}
    assert engineer.post(f"/api/admission/{candidate_id}/admit",
                         json=payload).status_code == 403
    admitted = owner.post(f"/api/admission/{candidate_id}/admit", json=payload)
    assert admitted.status_code == 201, admitted.text
    assert admitted.json()["item"]["metadata"]["dlc"]["expected_source"] == "annotator"
    refused = owner.post(f"/api/admission/{candidate_id}/admit", json=payload)
    assert refused.status_code == 409


def test_cost_estimate_scales_with_repeats(engineer, owner):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, items=4, seeder=owner)
    db = SessionLocal()
    try:
        split = db.query(EvalDataset).filter(EvalDataset.split_of == dataset_id,
                                            EvalDataset.split == "regression").one()
        split_id = split.id
    finally:
        db.close()
    one = engineer.post("/api/eval/runs/estimate", json={
        "agent_id": agent_id, "dataset_id": split_id, "evaluators": [], "repeats": 1}).json()
    three = engineer.post("/api/eval/runs/estimate", json={
        "agent_id": agent_id, "dataset_id": split_id, "evaluators": [], "repeats": 3}).json()
    assert one["sessions"] == 4 and three["sessions"] == 12
    assert three["duration_minutes"] >= one["duration_minutes"]
    assert engineer.post("/api/eval/runs/estimate", json={"repeats": 99}).status_code == 422


def test_the_release_card_prices_the_gate_evaluation_it_will_start(engineer, owner,
                                                                   monkeypatch):
    """Live: the 判据 → 放行门 candidate card asked for an estimate with no dataset and no
    evaluators, so it always read "未定价 · 0 个会话 · 该模型没有价格数据" — even for a
    GLM-5.3 agent, which is priced — and the Evaluate click never confirmed a cost the
    gate itself then demanded confirmation for."""
    from app.dlc import cost as cost_svc
    from app.dlc import releases as release_svc

    monkeypatch.setattr(release_svc, "point_endpoint", lambda *a, **k: {})
    monkeypatch.setattr(release_svc, "endpoint_version", lambda *a, **k: "1")
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    agent_id, _ = _release_setup(engineer, owner)
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.spec = {**(agent.spec or {}), "model_id": "global.zai.glm-5.3"}
        db.commit()
        # before a release is open there is no gate run to price; say so, not "unpriced"
        idle = engineer.post("/api/eval/runs/estimate", json={
            "agent_id": agent_id, "repeats": 1, "release": True}).json()
        assert idle["sessions"] == 0 and idle["unpriced"] is False
        assert idle["gate_unavailable"] == "release.nothing_pending"
        release_svc.after_deploy(db, db.get(Agent, agent_id), note=None, actor="engineer")
        db.commit()
    finally:
        db.close()
    priced = []
    real_estimate = cost_svc.estimate
    monkeypatch.setattr(cost_svc, "estimate",
                        lambda *a, **k: priced.append(k) or real_estimate(*a, **k))
    card = engineer.post("/api/eval/runs/estimate", json={
        "agent_id": agent_id, "repeats": 2, "release": True}).json()
    # regression (2) + holdout (2) × 2 repeats, the agent's GLM-5.3 price
    assert card["sessions"] == 8 and card["items"] == 4
    assert card["unpriced"] is False and card["total_usd"] > 0
    assert card["agent_basis"] in ("rough", "history_7d")
    assert priced[-1]["evaluators"] == ["code-pii", "code-stock"]

    # the gate enforces exactly what the card showed
    submitted = []
    monkeypatch.setattr("app.evaluation.service.submit_run",
                        lambda **kw: submitted.append(kw) or _stub_run(kw))
    assert engineer.post(f"/api/agents/{agent_id}/release/evaluate",
                         json={"repeats": 2}).status_code == 202
    gate = {k: v for k, v in priced[-1].items() if k != "db"}
    card_call = {k: v for k, v in priced[0].items() if k != "db"}
    assert gate["items"] == card_call["items"] and gate["repeats"] == card_call["repeats"]


def test_watch_config_and_scorecard(engineer, admin, owner):
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    _golden(engineer, lineage, seeder=owner)
    put = engineer.put(f"/api/agents/{agent_id}/watch", json={
        "criteria_lineage_id": lineage, "every": "daily", "at_hour": 2, "repeats": 2,
        "max_cost_usd": 5})
    assert put.status_code == 200, put.text
    assert put.json()["config"]["repeats"] == 2 and put.json()["config"]["next_due_at"]
    assert put.json()["alerts"]["quiet"] is True  # no runs yet, and that is said plainly

    card = engineer.get(f"/api/agents/{agent_id}/scorecard").json()
    assert [d["dimension"] for d in card["dimensions"]] == [
        "cognition", "quality", "responsibility", "cost", "performance"]
    quality = next(d for d in card["dimensions"] if d["dimension"] == "quality")
    assert quality["standard"] == 0.9 and quality["declared_gates"] == 1
    assert next(d for d in card["dimensions"]
                if d["dimension"] == "cognition")["not_applicable"] is True
    assert card["criteria_set"]["version"] == 1


def test_the_scorecard_shows_the_live_version_not_the_open_candidate(engineer, owner,
                                                                    monkeypatch):
    """After a gated deploy the ledger version is the candidate; production is still live."""
    from app.dlc import releases as release_svc

    monkeypatch.setattr(release_svc, "point_endpoint", lambda *a, **k: {})
    monkeypatch.setattr(release_svc, "endpoint_version", lambda *a, **k: "2")
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    agent_id, _ = _release_setup(engineer, owner)
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.version = "3"  # the redeploy that opened the release
        db.commit()
        release_svc.after_deploy(db, db.get(Agent, agent_id), note=None, actor="engineer")
        db.commit()
    finally:
        db.close()
    card = engineer.get(f"/api/agents/{agent_id}/scorecard").json()
    assert card["release"]["ledger_version"] == "3"
    assert card["release"]["live_version"] == "2", card["release"]
    assert card["release"]["candidate_version"] == "3"


def test_reads_are_member_visible_but_audit_is_admin_only(engineer, admin):
    assert engineer.get("/api/criteria-sets").status_code == 200
    assert engineer.get("/api/golden-sets").status_code == 200
    assert engineer.get("/api/admission").status_code == 200
    assert engineer.get("/api/audit").status_code == 403
    assert admin.get("/api/audit").status_code == 200


def test_the_release_gate_enforces_the_cost_policy_not_just_shows_it(engineer, owner, admin,
                                                                      monkeypatch):
    from app.dlc import cost as cost_svc
    from app.dlc import releases as release_svc

    monkeypatch.setattr(release_svc, "point_endpoint", lambda *a, **k: {})
    monkeypatch.setattr(release_svc, "endpoint_version", lambda *a, **k: "1")
    monkeypatch.setattr("app.services.agentcore.client.control_client", lambda ctx: object())
    agent_id, _ = _release_setup(engineer, owner)
    db = SessionLocal()
    try:
        release_svc.after_deploy(db, db.get(Agent, agent_id), note=None, actor="engineer")
        db.commit()
    finally:
        db.close()
    submitted = []
    monkeypatch.setattr("app.evaluation.service.submit_run",
                        lambda **kw: submitted.append(kw) or _stub_run(kw))

    monkeypatch.setattr(cost_svc, "estimate", lambda *a, **k: {
        "total_usd": 12.0, "sessions": 40, "max_usd": 10.0, "over_limit": True,
        "confirm_required": True, "confirm_usd": 5.0})
    over = engineer.post(f"/api/agents/{agent_id}/release/evaluate",
                         json={"repeats": 5, "confirm_cost": True})
    assert over.status_code == 409 and over.json()["code"] == "run.cost_over_limit"
    assert submitted == []  # refused before a single session was spent

    monkeypatch.setattr(cost_svc, "estimate", lambda *a, **k: {
        "total_usd": 6.0, "sessions": 40, "max_usd": None, "over_limit": False,
        "confirm_required": True, "confirm_usd": 5.0})
    unconfirmed = engineer.post(f"/api/agents/{agent_id}/release/evaluate",
                                json={"repeats": 5})
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["code"] == "run.cost_confirm_required"
    assert submitted == []
    confirmed = engineer.post(f"/api/agents/{agent_id}/release/evaluate",
                              json={"repeats": 5, "confirm_cost": True})
    assert confirmed.status_code == 202, confirmed.text
    assert {kw["repeats"] for kw in submitted} == {5}


def test_a_task_run_over_a_golden_split_is_scored_against_its_criteria(engineer, owner,
                                                                     monkeypatch):
    from app.dlc import cost as cost_svc

    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, seeder=owner)
    regression = engineer.get(f"/api/golden-sets/{dataset_id}").json()["splits"]["regression"]
    submitted = []
    monkeypatch.setattr("app.evaluation.service.submit_run",
                        lambda **kw: submitted.append(kw) or _stub_run(
                            {**kw, "qualifier": None, "agent_version": "2"}))
    monkeypatch.setattr(cost_svc, "estimate", lambda *a, **k: {
        "total_usd": 0.4, "sessions": 6, "over_limit": False, "confirm_required": False})

    # pass^k needs scenarios to replay — it is refused on past sessions
    sessions = engineer.post("/api/eval/runs", json={
        "agent_id": agent_id, "session_ids": ["s1"], "repeats": 3,
        "evaluators": ["Builtin.Helpfulness"]})
    assert sessions.status_code == 422 and sessions.json()["code"] == "run.repeats_scope"

    res = engineer.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": regression["id"], "repeats": 3,
        "evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code in (200, 201, 202), res.text
    kw = submitted[-1]
    assert kw["repeats"] == 3 and kw["split"] == "regression"
    assert kw["criteria_set_version"] == 1 and kw["criteria_set_id"]
    assert kw["cost_estimate"]["sessions"] == 6


def test_a_metric_dimension_reports_its_measurement_not_a_blank(engineer, owner,
                                                                monkeypatch):
    """Cost and performance criteria measure a value against a rule, so the five-dimension
    scorecard must show that value — a dash reads as "never measured"."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    saved = engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": [
        {**CRITERIA[0], "notes": "n/a:cognition n/a:cost"},
        CRITERIA[1],
        {"key": "L1", "text": "answers inside 3s at P95", "dimension": "performance",
         "tier": "gate", "executor": {"kind": "metric"},
         "metric_rule": {"metric": "latency_p95_ms", "op": "<=", "value": 3000}},
    ]})
    assert saved.status_code == 200, saved.text
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    _golden(engineer, lineage, seeder=owner)
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=WS, agent_id=agent_id, agent_name="a", status="completed",
                      criteria_set_id=engineer.get(f"/api/criteria-sets/{lineage}").json()
                      ["set"]["id"],
                      criteria_set_version=1, split="regression", agent_version="2")
        run.criteria_summary = {"criteria": {
            "L1": {"key": "L1", "kind": "metric", "tier": "gate", "dimension": "performance",
                   "value": 1146.9, "n": 3, "verdict": "pass",
                   "rule": {"metric": "latency_p95_ms", "op": "<=", "value": 3000}},
        }}
        db.add(run)
        db.commit()
    finally:
        db.close()
    card = engineer.get(f"/api/agents/{agent_id}/scorecard").json()
    perf = next(d for d in card["dimensions"] if d["dimension"] == "performance")
    assert perf["not_applicable"] is False
    assert perf["metrics"] == [{"key": "L1", "metric": "latency_p95_ms", "op": "<=",
                               "bound": 3000, "value": 1146.9, "verdict": "pass", "n": 3}]


def test_the_task_list_withholds_the_judge_from_an_annotator_like_the_detail_route(
    engineer, owner, admin
):
    """Security review finding: the list route hardcoded `privileged=True`, so every
    annotator could read the judge's verdicts (and everyone else's votes) for all 100
    recent tasks — exactly what blind labelling exists to withhold."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": [
        CRITERIA[0],
        {"key": "J1", "text": "polite", "dimension": "quality", "tier": "gate",
         "threshold": 0.9,
         "executor": {"kind": "evaluator", "evaluator_id": "Builtin.Helpfulness"}},
    ]})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    task = engineer.post("/api/annotation-tasks", json={
        "agent_id": agent_id, "criteria_lineage_id": lineage, "criterion_key": "J1",
        "annotators": [ENG["username"], BIZ["username"]],
        "items": [{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass",
                   "judge_explanation": "because"}]})
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]
    engineer.post(f"/api/annotation-tasks/{task_id}/labels",
                  json={"item_ref": "i0", "label": "fail"})

    listed = engineer.get("/api/annotation-tasks").json()["tasks"]
    mine = next(t for t in listed if t["id"] == task_id)
    item = mine["items"][0]
    assert "judge_label" not in item and "judge_explanation" not in item
    assert "labels" not in item  # nor the other annotator's vote
    assert item["my_label"] == "fail"  # their own is theirs to see

    # the owner holds judge.calibrate, so they read the numbers they have to rule on
    privileged = owner.get("/api/annotation-tasks").json()["tasks"]
    assert next(t for t in privileged if t["id"] == task_id)["items"][0]["judge_label"] == "pass"
    admin_view = admin.get("/api/annotation-tasks").json()["tasks"]
    assert next(t for t in admin_view if t["id"] == task_id)["items"][0]["judge_label"] == "pass"


def test_a_watch_cannot_pin_another_workspaces_golden_set(engineer, owner):
    """Security review finding: `dataset_id` was stored unchecked, so another
    workspace's sealed holdout could be replayed against an agent the caller controls."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    _golden(engineer, lineage, seeder=owner)
    db = SessionLocal()
    try:
        other = Workspace(id="ws-other", name="Other", tier="dev",
                          account_id="222222222222", region="us-west-2")
        db.add(other)
        db.flush()
        foreign = EvalDataset(workspace_id=other.id, name="theirs", kind="predefined",
                             role="golden", items=[])
        db.add(foreign)
        db.commit()
        foreign_id = foreign.id
    finally:
        db.close()
    refused = engineer.put(f"/api/agents/{agent_id}/watch", json={
        "criteria_lineage_id": lineage, "dataset_id": foreign_id})
    assert refused.status_code == 404 and refused.json()["code"] == "golden.not_found"
    db = SessionLocal()
    try:
        db.query(EvalDataset).filter(EvalDataset.workspace_id == "ws-other").delete()
        db.query(Workspace).filter(Workspace.id == "ws-other").delete()
        db.commit()
    finally:
        db.close()


def test_admission_records_how_the_text_was_screened(engineer, owner):
    """Security review finding: the redaction preview only ran on the detail route, so
    admitting straight from the queue stored the raw transcript. It is computed here
    now, and a workspace with no guardrail is recorded rather than silently trusted."""
    agent_id = _agent()
    lineage = _create_set(engineer, agent_id)
    engineer.put(f"/api/criteria-sets/{lineage}", json={"criteria": CRITERIA})
    engineer.post(f"/api/criteria-sets/{lineage}/publish", json={})
    dataset_id = _golden(engineer, lineage, seeder=owner)
    db = SessionLocal()
    try:
        candidate = AdmissionCandidate(workspace_id=WS, agent_id=agent_id, source="feedback",
                                       source_ref="f-unscreened",
                                       question="my card is 4111 1111 1111 1111",
                                       answer="noted", session_id="s9")
        db.add(candidate)  # note: NO redaction preview on the row
        db.commit()
        candidate_id = candidate.id
    finally:
        db.close()
    admitted = owner.post(f"/api/admission/{candidate_id}/admit", json={
        "split": "regression", "expected_response": "we never read card numbers back",
        "expected_source": "annotator", "criteria_ids": ["G1"], "case_tier": "known_bad"})
    assert admitted.status_code == 201, admitted.text
    meta = admitted.json()["item"]["metadata"]["dlc"]
    # the test workspace has no guardrail, so the screening could not run — and the item
    # says so instead of passing for screened
    assert meta["redaction"] in ("unavailable", "clean", "redacted")
    coverage = engineer.get(f"/api/golden-sets/{dataset_id}").json()["coverage"]
    assert coverage["unscreened_items"] == (1 if meta["redaction"] == "unavailable" else 0)
    db = SessionLocal()
    try:
        row = db.get(AdmissionCandidate, candidate_id)
        assert row.redaction.get("status")  # computed and persisted by admit()
    finally:
        db.close()
