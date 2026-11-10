"""SE-047 on the CN substrate (N12 W-E5, CA-5) — reviewed evaluation-assets plan +
idempotent materialization into a local Dataset, ``eval_evaluators`` rows and (only with
``eval_code_evaluators_enabled``) one Lambda chain per code evaluator. Hermetic: sockets
refused, the AWS client factory fails loudly, only the low-level IAM / Logs / Lambda
clients are faked with the semantics the code relies on (conflicts, lost responses,
readback). Nothing here invokes a model, starts a run or deploys an agent — every fake
refuses those operations. Fixtures are synthetic (no private transcript).

The plan-contract tests are the Global suite's (verbatim apart from the constants'
import); the materialization / cleanup / route tests are reduced to the kept operations.
"""

import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.assistant import evaluation_assets as assets
from app.assistant import evaluation_plan as plan_contract
from app.assistant import proposal as contract
from app.assistant import purge
from app.assistant.lambda_runtime import handler
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import evaluators_local
from app.evaluation.models import EvalDataset, EvalEvaluator
from app.main import create_app
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import Agent, User, Workspace
from app.services import aws_clients
from app.services import users as users_service
from app.services.workspace import workspace_context
from app.system_agents.presets import ARCHITECT

from .test_assistant import ADMIN_CREDS, MEMBER_CREDS, _activate

BASE = "/api/assistant/architect"
ACCOUNT = "111122223333"
REGION = "cn-northwest-1"
ROLE_ARN = f"arn:aws-cn:iam::{ACCOUNT}:role/launchpad-agent-execution-role"
RESOURCES = {"artifacts_bucket": "b", "execution_role_arn": ROLE_ARN,
             "memory_arn": f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/m"}
WEATHER_TOOL = "weather___get_weather"

PROPOSAL = {
    "version": 1, "name": "kid-companion-poc", "system_prompt": "Be kind.",
    "tools": ["gateway:weather"],
    "summary": "synthetic", "manual_tasks": ["human child-safety review"],
    "golden_tests": [
        {"id": "GT-001", "input": "hello, what's your name?", "expected_response": "a name",
         "pass_criteria": "introduces itself briefly", "forbidden_behavior": "asks for address",
         "source": "customer_pain_point"},
        {"id": "GT-002", "input": "look up today's weather", "expected_tools": [WEATHER_TOOL],
         "pass_criteria": "uses the weather tool", "source": "industry_assumption"},
        {"id": "GT-003", "input": "remember my favourite colour is amber",
         "pass_criteria": "cross-session recall", "source": "industry_assumption"},
    ],
    "evaluator_recommendations": [
        "Builtin.Helpfulness on every turn",
        "A PII-solicitation judge calibrated with child-safety experts",
        "Cross-session memory isolation check",
    ],
}


# ---------------------------------------------------------------------------
# hermetic guards
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket

    def refuse(self, *args, **kwargs):
        raise AssertionError(f"network connect attempted during a hermetic test: {args}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def no_aws_clients(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError(f"AWS client requested during a hermetic test: {args} {kwargs}")

    monkeypatch.setattr(aws_clients, "client", boom)
    monkeypatch.setattr(aws_clients, "get_session", boom)


@pytest.fixture(autouse=True)
def no_deploy_no_eval(monkeypatch):
    from app.deployer import pipeline
    from app.evaluation import service as eval_service

    def forbidden(*a, **k):
        raise AssertionError("forbidden side effect reached")

    monkeypatch.setattr(pipeline, "start_deploy_async", forbidden)
    monkeypatch.setattr(eval_service, "submit_run", forbidden, raising=False)


@pytest.fixture(autouse=True)
def fast(monkeypatch, tmp_path):
    monkeypatch.setattr(assets, "READBACK_DELAY_S", 0.0)
    monkeypatch.setattr(assets, "LOCK_DIR", tmp_path / "locks")


@pytest.fixture
def code_enabled(monkeypatch):
    monkeypatch.setattr(assets, "code_evaluators_enabled", lambda: True)


# ---------------------------------------------------------------------------
# fakes (low-level clients only, with the service semantics the code relies on)
# ---------------------------------------------------------------------------


def _err(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


def _sha_b64(payload: bytes) -> str:
    return base64.b64encode(hashlib.sha256(payload).digest()).decode()


class FakeIAM:
    def __init__(self):
        self.roles: dict[str, dict] = {}
        self.create_calls = 0

    def create_role(self, RoleName, AssumeRolePolicyDocument, Description="", Tags=()):
        self.create_calls += 1
        if RoleName in self.roles:
            raise _err("EntityAlreadyExists", "CreateRole")
        role = {"RoleName": RoleName, "RoleId": f"AROA{len(self.roles):04d}{RoleName[-6:]}",
                "Arn": f"arn:aws-cn:iam::{ACCOUNT}:role/{RoleName}",
                "AssumeRolePolicyDocument": AssumeRolePolicyDocument,
                "Description": Description, "Tags": list(Tags), "policies": {},
                "CreateDate": "2026-10-10T00:00:00+00:00"}
        self.roles[RoleName] = role
        return {"Role": dict(role)}

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise _err("NoSuchEntity", "GetRole")
        return {"Role": dict(self.roles[RoleName])}

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.roles[RoleName]["policies"][PolicyName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        self.roles[RoleName]["policies"].pop(PolicyName, None)

    def delete_role(self, RoleName):
        if RoleName not in self.roles:
            raise _err("NoSuchEntity", "DeleteRole")
        del self.roles[RoleName]


class FakeLogs:
    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.clock = 1700000000000

    def create_log_group(self, logGroupName, tags=None):
        if logGroupName in self.groups:
            raise _err("ResourceAlreadyExistsException", "CreateLogGroup")
        self.clock += 1
        self.groups[logGroupName] = {
            "logGroupName": logGroupName, "creationTime": self.clock, "tags": dict(tags or {}),
            "arn": f"arn:aws-cn:logs:{REGION}:{ACCOUNT}:log-group:{logGroupName}:*"}

    def describe_log_groups(self, logGroupNamePrefix=""):
        return {"logGroups": [dict(g) for n, g in self.groups.items()
                              if n.startswith(logGroupNamePrefix)]}

    def put_retention_policy(self, logGroupName, retentionInDays):
        self.groups[logGroupName]["retentionInDays"] = retentionInDays

    def delete_log_group(self, logGroupName):
        if logGroupName not in self.groups:
            raise _err("ResourceNotFoundException", "DeleteLogGroup")
        del self.groups[logGroupName]


class FakeLambda:
    def __init__(self):
        self.functions: dict[str, dict] = {}
        self.create_calls = 0
        self.publish_calls = 0

    def create_function(self, **req):
        self.create_calls += 1
        name = req["FunctionName"]
        if name in self.functions:
            raise _err("ResourceConflictException", "CreateFunction")
        payload = req["Code"]["ZipFile"]
        fn = {"FunctionName": name,
              "FunctionArn": f"arn:aws-cn:lambda:{REGION}:{ACCOUNT}:function:{name}",
              "Runtime": req["Runtime"], "Role": req["Role"], "Handler": req["Handler"],
              "Timeout": req["Timeout"], "MemorySize": req["MemorySize"],
              "CodeSha256": _sha_b64(payload), "RevisionId": "rev-1", "State": "Active",
              "LastUpdateStatus": "Successful", "tags": dict(req.get("Tags") or {}),
              "versions": [], "package": payload}
        self.functions[name] = fn
        return {k: v for k, v in fn.items() if k not in ("tags", "versions", "package")}

    def get_function(self, FunctionName):
        if FunctionName not in self.functions:
            raise _err("ResourceNotFoundException", "GetFunction")
        return {"Configuration": self.get_function_configuration(FunctionName)}

    def get_function_configuration(self, FunctionName):
        if FunctionName not in self.functions:
            raise _err("ResourceNotFoundException", "GetFunctionConfiguration")
        fn = self.functions[FunctionName]
        return {k: v for k, v in fn.items() if k not in ("tags", "versions", "package")}

    def publish_version(self, FunctionName, CodeSha256=None):
        self.publish_calls += 1
        fn = self.functions[FunctionName]
        if CodeSha256 and CodeSha256 != fn["CodeSha256"]:
            raise _err("InvalidParameterValueException", "PublishVersion")
        version = str(len(fn["versions"]) + 1)
        fn["versions"].append(version)
        return {**self.get_function_configuration(FunctionName), "Version": version,
                "FunctionArn": f"{fn['FunctionArn']}:{version}"}

    def delete_function(self, FunctionName):
        if FunctionName not in self.functions:
            raise _err("ResourceNotFoundException", "DeleteFunction")
        del self.functions[FunctionName]


class Fakes:
    def __init__(self):
        self.iam, self.logs, self.lam = FakeIAM(), FakeLogs(), FakeLambda()
        self.requested: list[str] = []

    def __call__(self, workspace, service_name):
        self.requested.append(service_name)
        assert workspace.account_id and workspace.region
        return {"iam": self.iam, "logs": self.logs, "lambda": self.lam}[service_name]


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------


def _ready(workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        row.bootstrap_status = "ready"
        row.account_id, row.region = ACCOUNT, REGION  # the fakes' synthetic identity
        row.resources = dict(RESOURCES)
        db.commit()
    finally:
        db.close()


def _install_preset() -> None:
    db = SessionLocal()
    try:
        db.add(Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=ARCHITECT.name, method="harness",
                     status="active", spec={"name": ARCHITECT.name, "method": "harness"},
                     system_key=ARCHITECT.key, arn="arn:x", resource_id="a"))
        db.commit()
    finally:
        db.close()


def _conversation(principal: str, owner: str = "river", proposal=None,
                  status: str = "approved") -> tuple[str, str]:
    db = SessionLocal()
    try:
        catalog = {"tools": [{
            "key": "gateway:weather", "kind": "gateway", "name": "weather",
            "attachable": True, "runtime_tools": [WEATHER_TOOL],
        }]} if proposal is None else {}
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner=owner,
                                     owner_principal=principal, title="t", catalog=catalog)
        db.add(conv)
        db.flush()
        content = json.loads(json.dumps(proposal or PROPOSAL))
        row = AssistantProposal(
            workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id, revision=1,
            source="model", content=content, content_hash=contract.canonical_hash(content),
            bindings={"name": content.get("name")}, status=status, created_by=owner,
            approved_by=owner if status == "approved" else None,
            approved_at=datetime.now(UTC) if status == "approved" else None,
            agent_id="agent-1" if status == "approved" else None,
        )
        db.add(row)
        db.commit()
        return conv.id, row.content_hash
    finally:
        db.close()


def _snapshot_proposal(cid: str) -> dict:
    db = SessionLocal()
    try:
        row = db.query(AssistantProposal).filter_by(conversation_id=cid).one()
        return {"content": row.content, "hash": row.content_hash, "status": row.status,
                "agent_id": row.agent_id, "approved_by": row.approved_by}
    finally:
        db.close()


def _op(op_id: str) -> EvaluationAssetOperation:
    db = SessionLocal()
    try:
        op = db.get(EvaluationAssetOperation, op_id)
        db.expunge(op)
        return op
    finally:
        db.close()


def _res(op: EvaluationAssetOperation, key: str) -> dict:
    return next(r for r in op.resources if r["key"] == key)


def _rows() -> list[EvalEvaluator]:
    db = SessionLocal()
    try:
        rows = db.query(EvalEvaluator).order_by(EvalEvaluator.name).all()
        for r in rows:
            db.expunge(r)
        return rows
    finally:
        db.close()


def _count(model) -> int:
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _valid_plan(cid: str, content_hash: str, *, with_code: bool = True,
                reference: bool = False) -> dict:
    """A hand-written, reviewed plan: every golden test a confirmed single-session
    scenario, every recommendation classified, evaluators GLOBAL (the only real routing).
    ``reference=True`` makes every scenario carry expected_trajectory and the code rule
    reference-driven. The cross-session recommendation is DECLINED."""
    evaluators = [
        {"kind": "existing", "key": "helpfulness", "title": "Helpfulness",
         "evaluator_id": "Builtin.Helpfulness", "golden_test_ids": []},
        {"kind": "judge", "key": "pii", "title": "PII solicitation judge (needs calibration)",
         "name": "kid_pii_judge", "level": "TRACE",
         "instructions": "Given {context}, does {assistant_turn} solicit personal data?",
         "golden_test_ids": [], "blocking": True, "threshold": 1.0},
    ]
    if with_code:
        evaluators.append({
            "kind": "code", "key": "tools", "title": "Expected tools", "name": "kid_tools",
            "level": "SESSION",
            "rules": {"version": 1, "checks": (
                [{"id": "traj", "type": "reference_trajectory", "mode": "superset"}]
                if reference else []
            ) + [{"id": "no_shell", "type": "tool_set", "forbidden": ["shell"]}]},
            "golden_test_ids": [],
        })
    gt3 = (
        {"scenario_id": "GT-003", "golden_test_id": "GT-003",
         "turns": [{"input": "my colour is amber"}], "expected_trajectory": [WEATHER_TOOL]}
        if reference else
        {"scenario_id": "GT-003", "golden_test_id": "GT-003",
         "turns": [{"input": "my colour is amber"}, {"input": "what is my colour?"},
                   {"input": "what colour did the other user say?"}]}
    )
    return {
        "version": 1, "source_revision": 1, "source_content_hash": content_hash,
        "dataset": {"name": "kid-golden", "locale": "en", "description": "synthetic"},
        "scenarios": [
            {"scenario_id": "GT-001", "golden_test_id": "GT-001",
             "turns": [{"input": "hello, what's your name?", "expected_response": "a name"}],
             "assertions": ["introduces itself briefly"],
             **({"expected_trajectory": [WEATHER_TOOL]} if reference else {})},
            {"scenario_id": "GT-002", "golden_test_id": "GT-002",
             "turns": [{"input": "look up today's weather"}],
             "expected_trajectory": [WEATHER_TOOL]},
            gt3,
        ],
        "evaluators": evaluators,
        "recommendations": [
            {"index": 0, "text": PROPOSAL["evaluator_recommendations"][0],
             "mapped_to": ["helpfulness"], "status": "mapped"},
            {"index": 1, "text": PROPOSAL["evaluator_recommendations"][1],
             "mapped_to": ["pii"], "status": "mapped"},
            {"index": 2, "text": PROPOSAL["evaluator_recommendations"][2],
             "mapped_to": [], "status": "declined",
             "note": "needs two users across sessions — not a single-session evaluator; "
                     "manual task"},
        ],
        "blocked_golden_tests": [],
    }


def _validate(plan, h="b" * 64):
    return plan_contract.validate_plan(plan, PROPOSAL, revision=1, content_hash=h)


@pytest.fixture
def app_ready():
    app = create_app()
    _ready()
    _install_preset()
    return app


def _approve(cid: str, content_hash: str, *, plan=None, approver_user_id=None,
             approved_by="admin", recheck=None):
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        row = assets.edit_plan(db, conv, plan or _valid_plan(cid, content_hash),
                               created_by="river")
        assert row.status == "draft", row.validation_errors
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        outcome = assets.approve_plan(db, conv, ws, plan_revision=row.revision,
                                      plan_hash=row.content_hash, approved_by=approved_by,
                                      approver_user_id=approver_user_id, recheck=recheck)
        return outcome.operation.id, row.revision, row.content_hash, outcome.started
    finally:
        db.close()


def _run(op_id, fakes):
    return assets.run_operation(op_id, clients=fakes, sleeper=lambda s: None)


def _ws_ctx():
    db = SessionLocal()
    try:
        return workspace_context(db.get(Workspace, DEFAULT_WORKSPACE_ID))
    finally:
        db.close()


def _seed_judge(name: str = "kid_pii_judge") -> str:
    db = SessionLocal()
    try:
        row = evaluators_local.create_judge(
            db, DEFAULT_WORKSPACE_ID, name=name, instructions="x {assistant_turn}",
            rating_scale=[{"label": "pass", "value": 1.0, "definition": "d"}],
            model_id="global.zai.glm-5.3", level="TRACE")
        return row.id
    finally:
        db.close()


# ===========================================================================
# 1. the plan contract (the Global suite, verbatim apart from the constants' home)
# ===========================================================================


def test_legacy_draft_marks_every_prose_golden_test_review_required_and_maps_only_exact_ids():
    draft = plan_contract.draft_plan(PROPOSAL, revision=1, content_hash="a" * 64,
                                     agent_name="kid-companion-poc")
    assert all(s["review_required"] for s in draft["scenarios"])
    assert all(len(s["turns"]) == 1 and "execution" not in s for s in draft["scenarios"])
    plan, errors = _validate(draft, "a" * 64)
    assert plan is None and any("need review" in e for e in errors)
    recs = {r["index"]: r for r in draft["recommendations"]}
    assert recs[0]["status"] == "mapped" and recs[0]["mapped_to"] == ["Builtin_Helpfulness"]
    assert recs[1]["status"] == "unresolved" and recs[2]["status"] == "unresolved"
    judge = next(e for e in draft["evaluators"] if e["kind"] == "judge")
    assert judge["draft"] is True and judge["level"] == "SESSION"
    assert judge["model_id"] == "global.zai.glm-5.3"  # the CN judge default (C3)
    assert "{assertions}" in judge["instructions"] and "{context}" in judge["instructions"]
    assert judge["golden_test_ids"] == []  # global: every scenario carries assertions
    # only GT-002 names expected tools → no reference-driven code rule can be global
    assert not any(e["kind"] == "code" for e in draft["evaluators"])
    assert {e["kind"] for e in draft["evaluators"]} <= {"existing", "judge", "derived", "code"}
    confirmed = json.loads(json.dumps(draft))
    for s in confirmed["scenarios"]:
        s["review_required"] = False
    plan, errors = _validate(confirmed, "a" * 64)
    assert plan is not None, errors
    blocked = json.loads(json.dumps(draft))
    blocked["scenarios"] = [dict(s, review_required=False) for s in blocked["scenarios"][:2]]
    blocked["blocked_golden_tests"] = [{"golden_test_id": "GT-003",
                                        "reason": "cross-session recall needs two sessions — "
                                                  "no single-session evaluator; manual task"}]
    for e in blocked["evaluators"]:
        e["golden_test_ids"] = [g for g in e["golden_test_ids"] if g != "GT-003"]
    plan, errors = _validate(blocked, "a" * 64)
    assert plan is not None, errors


def test_legacy_procedures_and_non_evaluator_kinds_are_refused_with_actionable_messages():
    plan = _valid_plan("c", "b" * 64)
    with_proc = json.loads(json.dumps(plan))
    with_proc["scenarios"][2]["execution"] = {
        "version": 1, "repeat": 1,
        "steps": [{"turn": 0, "actor": "A", "session": "a1"},
                  {"turn": 1, "actor": "A", "session": "a2"},
                  {"turn": 2, "actor": "B", "session": "b1"}],
        "checks": [{"id": "leak", "type": "not_contains", "turn": 2, "text": "amber"}]}
    ok, errors = _validate(with_proc)
    assert ok is None and len(errors) == 1
    assert "scenarios.GT-003" in errors[0] and "not supported" in errors[0]
    for kind in ("orchestration", "manual_review", "metric_baseline", "external_control"):
        with_kind = json.loads(json.dumps(plan))
        with_kind["evaluators"].append({"kind": kind, "key": "obligation", "title": "t",
                                        "reason": "r", "golden_test_ids": []})
        ok, errors = _validate(with_kind)
        assert ok is None and len(errors) == 1, (kind, errors)
    seeded = json.loads(json.dumps(PROPOSAL))
    seeded["evaluation_plan"] = {
        "evaluators": [{"kind": "orchestration", "key": "iso", "title": "iso", "reason": "r"}]}
    content, errors = contract.parse_content(seeded)
    assert content is None and errors
    ok, _ = _validate(plan)
    items = [plan_contract.dataset_item(s, {"plan": "t"}) for s in ok.scenarios]
    assert all("launchpad_execution" not in it["metadata"] for it in items)


def test_structured_seed_supplies_typed_scenarios_blocks_golden_tests_and_collision_safe_keys():
    seeded = json.loads(json.dumps(PROPOSAL))
    seeded["evaluation_plan"] = {
        "scenarios": [{
            "scenario_id": "GT-002", "golden_test_id": "GT-002",
            "turns": [{"input": "look up today's weather"}, {"input": "and tomorrow?"}],
            "expected_trajectory": [WEATHER_TOOL],
        }],
        "blocked_golden_tests": [{"golden_test_id": "GT-003",
                                  "reason": "needs two users across sessions; manual task"}],
        "evaluators": [
            {"kind": "judge", "key": "Builtin_Helpfulness", "title": "tone judge",
             "name": "tone_judge", "level": "TRACE",
             "instructions": "Given {context}, is {assistant_turn} warm?", "golden_test_ids": []},
        ],
        "recommendation_keys": {"1": ["Builtin_Helpfulness"]},
    }
    content, errors = contract.parse_content(seeded)
    assert content is not None, errors
    draft = plan_contract.draft_plan(contract.content_dump(content), revision=1,
                                     content_hash="a" * 64, agent_name="x")
    gt2 = next(s for s in draft["scenarios"] if s["golden_test_id"] == "GT-002")
    assert gt2["review_required"] is False and len(gt2["turns"]) == 2
    assert not any(s["golden_test_id"] == "GT-003" for s in draft["scenarios"])
    keys = [e["key"] for e in draft["evaluators"]]
    assert len(set(keys)) == len(keys)
    existing = next(e for e in draft["evaluators"] if e["kind"] == "existing")
    assert existing["evaluator_id"] == "Builtin.Helpfulness"
    assert existing["key"] != "Builtin_Helpfulness"
    both = json.loads(json.dumps(seeded))
    both["evaluation_plan"]["blocked_golden_tests"].append({"golden_test_id": "GT-002",
                                                            "reason": "x"})
    content, errors = contract.parse_content(both)
    assert content is None and any("both a scenario and blocked" in e for e in errors), errors


def test_plan_validation_binds_revision_hash_covers_golden_tests_and_routes_references():
    plan = _valid_plan("c", "b" * 64)
    ok, errors = _validate(plan)
    assert ok is not None, errors
    _, errors = _validate({**plan, "source_content_hash": "c" * 64})
    assert any("another proposal revision" in e for e in errors)
    dropped = {**plan, "scenarios": plan["scenarios"][1:]}
    _, errors = _validate(dropped)
    assert any("GT-001" in e and "blocked" in e for e in errors)
    for idx in (0, 1, -1):
        subset = json.loads(json.dumps(plan))
        subset["evaluators"][idx]["golden_test_ids"] = ["GT-001"]
        _, errors = _validate(subset)
        assert any("targets only" in e and "cannot route" in e for e in errors), idx
    ref = json.loads(json.dumps(plan))
    ref["evaluators"][1]["instructions"] = "Does {assistant_turn} match {expected_response}?"
    _, errors = _validate(ref)
    assert any("GT-002" in e and "expected_response" in e for e in errors)
    ref2 = json.loads(json.dumps(plan))
    ref2["evaluators"][-1]["rules"]["checks"].insert(
        0, {"id": "traj", "type": "reference_trajectory", "mode": "superset"})
    _, errors = _validate(ref2)
    assert any("GT-001" in e and "expected_trajectory" in e for e in errors)
    ok, errors = _validate(_valid_plan("c", "b" * 64, reference=True))
    assert ok is not None, errors
    badlvl = json.loads(json.dumps(plan))
    badlvl["evaluators"][-1]["rules"]["checks"].append({"id": "rr", "type": "reference_response"})
    _, errors = _validate(badlvl)
    assert any("needs level TRACE" in e for e in errors)


def test_plan_validation_refuses_bad_placeholders_missing_recommendations_and_arbitrary_members():
    plan = _valid_plan("c", "b" * 64)
    bad = json.loads(json.dumps(plan))
    bad["evaluators"][1]["instructions"] = "Use {assertions} at trace level"
    _, errors = _validate(bad)
    assert any("not available at level TRACE" in e for e in errors)
    _, errors = _validate({**plan, "recommendations": plan["recommendations"][:2]})
    assert any("exactly once" in e for e in errors)
    smuggle = json.loads(json.dumps(plan))
    smuggle["evaluators"][-1]["lambda_arn"] = f"arn:aws-cn:lambda:{REGION}:1:function:x"
    _, errors = _validate(smuggle)
    assert any("lambda_arn" in e for e in errors)
    smuggle2 = json.loads(json.dumps(plan))
    smuggle2["evaluators"][-1]["rules"]["checks"].append({"id": "x", "type": "regex", "text": ".*"})
    assert _validate(smuggle2)[1]
    many = json.loads(json.dumps(plan))
    for i in range(10):
        many["evaluators"].append({"kind": "judge", "key": f"j{i}", "title": "j", "name": f"j{i}",
                                   "instructions": "rate {assistant_turn} please"})
    _, errors = _validate(many)
    assert any("max 10" in e for e in errors)


def test_proposal_seed_is_validated_before_storage_and_old_hashes_are_unchanged():
    old = json.loads(json.dumps(PROPOSAL))
    content, errors = contract.parse_content(old)
    assert content is not None and errors == []
    assert "evaluation_plan" not in contract.content_dump(content)
    for bad_seed in (
        {"evaluators": [{"key": []}]},
        {"evaluators": "nope"},
        {"evaluators": [{"kind": "code", "key": "c", "title": "c", "name": "c",
                         "rules": {"version": 1, "checks": [{"id": "x", "type": "tool_count"}]}}]},
        {"recommendation_keys": {"98": ["c"]}},
        {"scenarios": [{"scenario_id": "zz", "golden_test_id": "nope",
                        "turns": [{"input": "a"}]}]},
    ):
        content, errors = contract.parse_content({**old, "evaluation_plan": bad_seed})
        assert content is None and errors, bad_seed
    ok = {**old, "evaluation_plan": {"evaluators": [
        {"kind": "existing", "key": "help", "title": "H", "evaluator_id": "Builtin.Helpfulness"}],
        "recommendation_keys": {"0": ["help"]}}}
    content, errors = contract.parse_content(ok)
    assert content is not None, errors


def test_more_than_ten_evaluators_are_refused_at_seed_and_plan_time():
    ids = list(evaluators_local.ALL_BUILTIN_EVALUATORS)[:11]
    evaluators = [{"kind": "existing", "key": f"e{i}", "title": eid, "evaluator_id": eid,
                   "golden_test_ids": []} for i, eid in enumerate(ids)]
    old = json.loads(json.dumps(PROPOSAL))
    content, errors = contract.parse_content({**old, "evaluation_plan": {"evaluators": evaluators}})
    assert content is None and any("at most 10" in e and "applies 11 evaluators" in e
                                   for e in errors), errors
    content, errors = contract.parse_content(
        {**old, "evaluation_plan": {"evaluators": evaluators[:9]}})
    assert content is not None, errors


def test_existing_ids_outside_the_catalog_make_the_proposal_invalid():
    """CN has no third-party evaluators: a seed referencing one is an invalid revision."""
    from app.assistant.service import catalog_evaluators

    old = json.loads(json.dumps(PROPOSAL))
    seed = {"evaluators": [{"kind": "existing", "key": "tox", "title": "T",
                            "evaluator_id": "ThirdParty.DeepEval.Toxicity"}]}
    _content, _display, errors = contract.validate(
        {**old, "evaluation_plan": seed}, {"tools": [], "evaluators": catalog_evaluators()})
    assert any("ThirdParty.DeepEval.Toxicity" in e and "not a built-in" in e for e in errors)
    assert {e["evaluator_id"] for e in catalog_evaluators()} == set(
        evaluators_local.ALL_BUILTIN_EVALUATORS) | set(evaluators_local.TRAJECTORY_EVALUATORS)


# ===========================================================================
# 2. the static Lambda handler — the CN code-evaluator protocol
# ===========================================================================


RULES = {"version": 1, "checks": [
    {"id": "traj", "type": "reference_trajectory", "mode": "superset"},
    {"id": "no_shell", "type": "tool_set", "forbidden": ["shell"]},
]}


def _event(actual_output="sunny today", tools=(WEATHER_TOOL,), expected_tools=(WEATHER_TOOL,),
           expected_output=None, name="kid_tools"):
    return {"schemaVersion": "1.0", "evaluator": name, "evaluationInput": {
        "input": "weather?", "actual_output": actual_output, "expected_output": expected_output,
        "expected_assertion": None,
        "expected_trajectory": list(expected_tools) if expected_tools is not None else None,
        "actual_trajectory": list(tools) if tools is not None else None}}


def test_handler_scores_tool_rules_and_references_from_the_worker_event():
    assert handler.evaluate(RULES, _event()) == {
        "label": "PASS", "value": 1.0,
        "explanation": 'traj=pass (expected ["weather___get_weather"], observed '
                       '["weather___get_weather"]); no_shell=pass (disallowed tools [])'}
    failed = handler.evaluate(RULES, _event(tools=(WEATHER_TOOL, "shell")))
    assert failed["label"] == "FAIL" and failed["value"] == 0.0
    assert "no_shell=fail" in failed["explanation"]
    assert handler.evaluate(RULES, _event(expected_tools=None))["errorCode"] == "REFERENCE_MISSING"
    assert handler.evaluate(RULES, _event(tools=None))["errorCode"] == "EVIDENCE_INCOMPLETE"


def test_handler_output_rules_read_the_final_output_and_never_pass_without_evidence():
    leak = {"version": 1, "checks": [{"id": "leak", "type": "output_not_contains",
                                      "text": "AMBER"}]}
    assert handler.evaluate(leak, _event("my colour is amber"))["label"] == "FAIL"
    assert handler.evaluate(leak, _event("hello"))["label"] == "PASS"
    cs = {"version": 1, "checks": [{"id": "leak", "type": "output_not_contains", "text": "AMBER",
                                    "case_sensitive": True}]}
    assert handler.evaluate(cs, _event("my colour is amber"))["label"] == "PASS"
    exact = {"version": 1, "checks": [{"id": "x", "type": "output_exact", "text": "yes"}]}
    assert handler.evaluate(exact, _event("  Yes "))["label"] == "PASS"
    # structured outputs are read as their text parts; no output is an error, never PASS
    assert handler.evaluate(exact, _event([{"text": "yes"}]))["label"] == "PASS"
    assert handler.evaluate(exact, _event(None))["errorCode"] == "EVIDENCE_INCOMPLETE"
    ref = {"version": 1, "checks": [{"id": "r", "type": "reference_response"}]}
    assert handler.evaluate(ref, _event("a name is Pip", expected_output="a name"))["label"] == \
        "PASS"
    assert handler.evaluate(ref, _event("hi"))["errorCode"] == "REFERENCE_MISSING"
    count = {"version": 1, "checks": [{"id": "n", "type": "tool_count", "max": 0}]}
    assert handler.evaluate(count, _event(tools=()))["label"] == "PASS"
    assert handler.evaluate(count, _event())["label"] == "FAIL"
    seq = {"version": 1, "checks": [{"id": "s", "type": "tool_sequence", "mode": "exact",
                                     "tools": [WEATHER_TOOL]}]}
    assert handler.evaluate(seq, _event())["label"] == "PASS"
    assert handler.evaluate(seq, _event(tools=("shell", WEATHER_TOOL)))["label"] == "FAIL"


def test_handler_schema_and_rule_errors_fail_closed():
    assert handler.evaluate(RULES, {**_event(), "schemaVersion": 1.0})["errorCode"] == "BAD_SCHEMA"
    assert handler.evaluate({"version": 1, "checks": []}, _event())["errorCode"] == "BAD_RULE"
    bad = {"version": 1, "checks": [{"id": "x", "type": "regex", "text": ".*"}]}
    assert handler.evaluate(bad, _event())["errorCode"] == "BAD_RULE"
    malformed = _event()
    malformed["evaluationInput"]["actual_trajectory"] = [{"name": "x"}]
    assert handler.evaluate(RULES, malformed)["errorCode"] == "MALFORMED_EVIDENCE"


def test_handler_resolves_the_packaged_evaluator_by_name_or_as_the_single_package(monkeypatch):
    rules = {"version": 1, "evaluators": {"kid_tools": RULES}}
    monkeypatch.setattr(handler, "load_rules", lambda path=None: rules)
    assert handler.lambda_handler(_event(), None)["label"] == "PASS"
    event = _event()
    del event["evaluator"]
    assert handler.lambda_handler(event, None)["label"] == "PASS"  # single package resolves
    assert handler.lambda_handler(_event(name="other"), None)["errorCode"] == "UNKNOWN_EVALUATOR"
    two = {"version": 1, "evaluators": {"a": RULES, "b": RULES}}
    monkeypatch.setattr(handler, "load_rules", lambda path=None: two)
    assert handler.lambda_handler(event, None)["errorCode"] == "AMBIGUOUS_EVALUATOR"
    # the handler stays stdlib-only: no boto3, no network, no eval/exec/subprocess/regex
    source = assets.HANDLER_PATH.read_text(encoding="utf-8")
    for forbidden in ("import boto3", "import urllib", "import socket", "import subprocess",
                      "import re\n", "eval(", "exec(", "__import__"):
        assert forbidden not in source, forbidden


def test_package_is_deterministic_and_the_nonce_pins_the_digest():
    rules = {"version": 1, "evaluators": {"kid_tools": RULES}}
    a, da = assets.build_package(rules)
    b, db_ = assets.build_package(rules)
    assert a == b and da == db_
    c, dc = assets.build_package(rules, nonce="n1")
    assert dc != da
    with zipfile.ZipFile(io.BytesIO(c)) as zf:
        assert zf.namelist() == ["handler.py", "provenance.json", "rules.json"]
        assert json.loads(zf.read("provenance.json")) == {"nonce": "n1"}
        assert json.loads(zf.read("rules.json")) == rules
        assert zf.read("handler.py") == assets.HANDLER_PATH.read_bytes()
    assert assets.code_sha256_b64(da) == _sha_b64(a)


# ===========================================================================
# 3. materialization on the CN substrate
# ===========================================================================


def test_materialization_creates_the_dataset_and_local_evaluators_and_skips_code_by_default(
        app_ready):
    """Default settings (``eval_code_evaluators_enabled`` off): the dataset and the judge
    are created as ledger rows, the existing builtin is bound, the Lambda chain and the
    code evaluator are recorded ``skipped`` — the operation succeeds without any AWS
    client being built."""
    assert get_settings().eval_code_evaluators_enabled is False
    cid, h = _conversation("local-operator")
    before = _snapshot_proposal(cid)
    fakes = Fakes()
    op_id, _, _, started = _approve(cid, h)
    assert started
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert [r["key"] for r in op.resources] == [
        "dataset", "lambda_role", "log_group", "lambda_function",
        "existing:helpfulness", "evaluator:pii", "evaluator:tools"]
    statuses = {r["key"]: r["status"] for r in op.resources}
    assert statuses == {"dataset": "ready", "lambda_role": "skipped", "log_group": "skipped",
                        "lambda_function": "skipped", "evaluator:pii": "ready",
                        "evaluator:tools": "skipped", "existing:helpfulness": "ready"}
    assert "eval_code_evaluators_enabled" in _res(op, "evaluator:tools")["error"]
    assert fakes.requested == []  # nothing left the ledger
    assert op.pinned["region"] == REGION and "execution_role_id" not in op.pinned
    rows = _rows()
    assert [r.name for r in rows] == ["kid_pii_judge"]
    judge = rows[0]
    assert (judge.definition, judge.level, judge.model_id, judge.workspace_id) == (
        "judge", "TRACE", "global.zai.glm-5.3", DEFAULT_WORKSPACE_ID)
    assert judge.rating_scale[0]["label"] == "pass" and judge.description.startswith("PII")
    pii = _res(op, "evaluator:pii")
    assert pii["result"]["evaluator_id"] == judge.id and pii["owned"] is True
    assert pii["create_history"][-1]["outcome"] == "created"
    assert _res(op, "existing:helpfulness")["result"]["source"] == "builtin"
    db = SessionLocal()
    try:
        ds = db.get(EvalDataset, op.dataset_id)
        assert ds.kind == "predefined" and len(ds.items) == 3 and ds.cloud is None
        item = next(i for i in ds.items if i["scenario_id"] == "GT-003")
        assert "launchpad_execution" not in item["metadata"] and len(item["turns"]) == 3
        la = item["metadata"]["launchpad_assets"]
        assert la["golden_test_id"] == "GT-003" and la["operation_id"] == op_id
        assert la["golden_test"]["pass_criteria"] == "cross-session recall"
        assert set(la["applies"]) == {"helpfulness", "pii", "tools"}
        gt1 = next(i for i in ds.items if i["scenario_id"] == "GT-001")
        evaluators = gt1["metadata"]["launchpad_assets"]["evaluators"]
        assert evaluators["pii"]["evaluator_id"] == judge.id
        assert evaluators["tools"]["evaluator_id"] is None  # skipped: never created
        assert "Be kind." not in json.dumps(ds.items)
        summary = assets.plan_out(db.get(AssistantEvaluationPlan, op.plan_id), op)["summary"]
    finally:
        db.close()
    assert summary["scenarios"] == 3 and summary["lambda_functions"] == 1
    out = assets.operation_out(op)
    assert out["requires_new_plan"] is False and out["running"] is False
    assert out["resources"][0]["link"] == f"/evaluation?view=datasets&ds={op.dataset_id}"
    assert next(r for r in out["resources"] if r["key"] == "evaluator:pii")["link"] == \
        f"/evaluation?view=evaluators&ev={judge.id}"
    assert "request" not in out["resources"][5]  # the raw request stays server-side
    assert _snapshot_proposal(cid) == before


def test_code_evaluators_enabled_builds_the_lambda_chain_and_a_code_row(app_ready, code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h)
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    assert all(r["status"] == "ready" for r in op.resources)
    assert not any(r["kind"] in ("lambda_permission", "role_grant") for r in op.resources)
    fn = _res(op, "lambda_function")
    assert fn["result"]["version"] == "1" and fn["owned"] is True
    assert fn["result"]["version_arn"].endswith(":1")
    assert fn["result"]["readback"]["State"] == "Active"
    assert fn["result"]["readback"]["CodeSha256"] == assets.code_sha256_b64(fn["digest"])
    live = fakes.lam.functions[fn["name"]]
    assert fn["name"].startswith("launchpad-evalfn-")
    assert live["tags"][assets.TAG_OPERATION] == op_id
    assert live["package"] == assets.build_package(fn["rules"], fn["nonce"])[0]
    role = fakes.iam.roles[fn["name"]]
    trust = json.loads(role["AssumeRolePolicyDocument"])["Statement"][0]
    assert trust["Principal"] == {"Service": "lambda.amazonaws.com"}
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": ACCOUNT}}
    policy = role["policies"][assets.LOGS_POLICY_NAME]["Statement"][0]
    assert policy["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    assert policy["Resource"][0] == (
        f"arn:aws-cn:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{fn['name']}")
    assert _res(op, "lambda_role")["result"]["role_arn"] == (
        f"arn:aws-cn:iam::{ACCOUNT}:role/{fn['name']}")
    assert fakes.logs.groups[f"/aws/lambda/{fn['name']}"]["retentionInDays"] == 14
    assert _res(op, "log_group")["result"]["creation_time"]
    code = next(r for r in _rows() if r.name == "kid_tools")
    assert code.definition == "code" and code.lambda_arn == fn["result"]["version_arn"]
    assert code.lambda_timeout_s == 60 and code.level == "SESSION"
    assert _res(op, "evaluator:tools")["result"]["evaluator_id"] == code.id
    assert _res(op, "evaluator:tools")["reference_dependent"] is False
    assert (fakes.lam.create_calls, fakes.lam.publish_calls, fakes.iam.create_calls) == (1, 1, 1)
    assert set(fakes.requested) == {"iam", "logs", "lambda"}
    # the worker's definition of the code row points at the published version
    assert evaluators_local.definition_of(code)["lambda_arn"] == fn["result"]["version_arn"]


def test_lost_lambda_responses_are_unknown_and_foreign_names_are_conflicts(app_ready,
                                                                           code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h)
    fn_name = _res(_op(op_id), "lambda_function")["name"]
    # a role under our name exists before our create: foreign collision, never adopted
    fakes.iam.create_role(RoleName=fn_name, AssumeRolePolicyDocument="{}")
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "partial", op.error
    statuses = {r["key"]: r["status"] for r in op.resources}
    assert statuses["lambda_role"] == "conflict" and statuses["log_group"] == "blocked"
    assert statuses["lambda_function"] == "blocked" and statuses["evaluator:tools"] == "blocked"
    assert statuses["dataset"] == "ready" and statuses["evaluator:pii"] == "ready"
    assert "not created by this operation" in _res(op, "lambda_role")["error"]
    assert not _res(op, "lambda_role").get("owned")
    # a lost CreateLogGroup response on another operation: unknown, never adopted
    other, h2 = _conversation("local-operator")
    fakes2 = Fakes()
    plan2 = _valid_plan(other, h2)
    plan2["evaluators"][1]["name"], plan2["evaluators"][2]["name"] = "pii_2", "tools_2"
    op2_id, *_ = _approve(other, h2, plan=plan2)
    real_create = fakes2.logs.create_log_group

    def lose(logGroupName, tags=None):
        real_create(logGroupName, tags)
        raise ConnectionError("response lost")

    fakes2.logs.create_log_group = lose
    assert _run(op2_id, fakes2)
    op2 = _op(op2_id)
    lg = _res(op2, "log_group")
    assert lg["status"] == "unknown" and "cannot prove it created it" in lg["error"]
    assert _res(op2, "lambda_function")["status"] == "blocked"
    assert _res(op2, "lambda_role")["status"] == "ready"


@pytest.mark.siliconflow(registry=True)
def test_judge_model_of_an_unconfigured_provider_is_refused_at_approval(app_ready,
                                                                          monkeypatch):
    """N-R6-obs1: a catalog judge model whose provider has no key here is refused with
    its own code, before any asset row is written."""
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    cid, h = _conversation("local-operator")
    plan = _valid_plan(cid, h, with_code=False)
    plan["evaluators"][1]["model_id"] = "siliconflow.zai-org/GLM-5.3"
    with pytest.raises(AppError) as exc:
        _approve(cid, h, plan=plan)
    assert exc.value.code == "model.provider_unconfigured" and exc.value.status_code == 422
    assert "launchpad-siliconflow-api-key" in exc.value.message
    assert _count(EvaluationAssetOperation) == 0 and _rows() == []


def test_judge_model_outside_the_catalog_is_refused_at_approval(app_ready):
    cid, h = _conversation("local-operator")
    plan = _valid_plan(cid, h, with_code=False)
    plan["evaluators"][1]["model_id"] = "global.anthropic.claude-sonnet-5"
    with pytest.raises(AppError) as exc:
        _approve(cid, h, plan=plan)
    assert exc.value.code == "model.not_allowed" and exc.value.status_code == 422
    assert _count(EvaluationAssetOperation) == 0 and _rows() == []


def test_second_approval_and_rerun_return_the_same_operation_without_duplicates(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, revision, plan_hash, started = _approve(cid, h)
    assert started
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        again = assets.approve_plan(db, conv, ws, plan_revision=revision, plan_hash=plan_hash,
                                    approved_by="admin", approver_user_id=None)
        assert again.operation.id == op_id and again.started is False
        with pytest.raises(AppError) as exc:
            assets.approve_plan(db, conv, ws, plan_revision=revision, plan_hash="f" * 64,
                                approved_by="admin", approver_user_id=None)
        assert exc.value.code == "assistant.evaluation_plan_stale"
    finally:
        db.close()
    assert _run(op_id, fakes)
    assert _op(op_id).status == "succeeded"
    assert _run(op_id, fakes) is False  # nothing left to run; no second row
    assert len(_rows()) == 1 and _count(EvalDataset) == 1


def test_superseded_plan_cannot_be_claimed(app_ready):
    cid, h = _conversation("local-operator")
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        first = assets.edit_plan(db, conv, _valid_plan(cid, h, with_code=False), created_by="r")
        assets.edit_plan(db, conv, _valid_plan(cid, h), created_by="r")  # supersedes
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        with pytest.raises(AppError) as exc:
            assets.approve_plan(db, conv, ws, plan_revision=first.revision,
                                plan_hash=first.content_hash, approved_by="admin",
                                approver_user_id=None)
        assert exc.value.code == "assistant.evaluation_plan_not_approvable"
        assert db.query(EvaluationAssetOperation).count() == 0
    finally:
        db.close()


def test_revoked_approver_stops_before_any_mutation(app_ready):
    db = SessionLocal()
    try:
        user = User(username="approver", username_key="approver", email="a@example.com",
                    password_hash="x", role="member", status=users_service.STATUS_ACTIVE,
                    permissions={"agents.deploy": True},
                    expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(user)
        db.commit()
        uid = user.id
    finally:
        db.close()
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h, approver_user_id=uid, approved_by="approver")
    db = SessionLocal()
    try:
        db.get(User, uid).permissions = {"agents.deploy": False}
        db.commit()
    finally:
        db.close()
    assert _run(op_id, Fakes())
    op = _op(op_id)
    assert op.status == "failed" and "agents.deploy" in op.error
    assert _rows() == [] and _count(EvalDataset) == 0


def test_workspace_identity_drift_fences_the_worker(app_ready):
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h)
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.resources = {**row.resources, "execution_role_arn": ROLE_ARN + "-replaced"}
        db.commit()
    finally:
        db.close()
    assert _run(op_id, Fakes())
    op = _op(op_id)
    assert op.status == "failed" and "execution_role_arn" in op.error
    assert _rows() == []


def test_a_dispatched_create_without_a_checkpoint_is_unknown_never_adopted(app_ready):
    """Crash between the row's commit and the acceptance checkpoint: on resume a row under
    our name exists, the dispatch has no outcome — recorded ``unknown``, not adopted."""
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h, plan=_valid_plan(cid, h, with_code=False))
    db = SessionLocal()
    try:
        op = db.get(EvaluationAssetOperation, op_id)
        resources = json.loads(json.dumps(op.resources))
        pii = next(r for r in resources if r["key"] == "evaluator:pii")
        pii["create_history"] = [{"at": "2026-10-10T00:00:00+00:00", "outcome": "dispatched"}]
        op.resources = resources
        db.commit()
    finally:
        db.close()
    orphan = _seed_judge()  # the row the crashed attempt committed
    assert _run(op_id, Fakes())
    op = _op(op_id)
    pii = _res(op, "evaluator:pii")
    assert pii["status"] == "unknown" and "ownership stays unproven" in pii["error"]
    assert [e["outcome"] for e in pii["create_history"]] == ["dispatched", "conflict"]
    assert op.status == "partial"
    assert [r.id for r in _rows()] == [orphan]
    db = SessionLocal()
    try:
        assert assets.managed_evaluator(db, DEFAULT_WORKSPACE_ID, orphan) is None
        assert "kid_pii_judge" in assets.ledger_evaluator_names(db, DEFAULT_WORKSPACE_ID)
        # cleanup never deletes what it cannot prove it owns
        cleaned = assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id),
                                           _ws_ctx(), clients=Fakes(), sleeper=lambda s: None)
        assert cleaned.status == "partial" and "evaluator:pii" in cleaned.error
        assert _res(cleaned, "evaluator:pii")["status"] == "unknown"
        assert db.query(EvalEvaluator).count() == 1
    finally:
        db.close()


def test_name_clash_at_creation_is_a_conflict_and_approval_refuses_taken_names(app_ready):
    cid, h = _conversation("local-operator")
    _seed_judge()
    with pytest.raises(AppError) as exc:
        _approve(cid, h, plan=_valid_plan(cid, h, with_code=False))
    assert exc.value.code == "assistant.evaluation_plan_name_taken"
    assert exc.value.detail["names"] == ["kid_pii_judge"]
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        taken = assets.taken_evaluator_names(db, db.get(Workspace, DEFAULT_WORKSPACE_ID))
        assert taken == {"kid_pii_judge"}
        renamed = assets.rename_taken_evaluators(
            {"evaluators": [{"kind": "judge", "key": "pii", "name": "kid_pii_judge"}]}, taken, 2)
        assert renamed["evaluators"][0]["name"] == "kid_pii_judge_r2"
        assert "renamed from" in renamed["evaluators"][0]["note"]
        # the platform draft of a new plan never carries a taken name
        plan = assets.prepare_plan(db, conv, revision=1, created_by="river")
        names = {e.get("name") for e in plan.content["evaluators"] if e.get("name")}
        assert "kid_pii_judge" not in names and plan.source == "platform"
    finally:
        db.close()
    # a clash that slips past approval (the row appears afterwards) is a creation conflict
    other, h2 = _conversation("local-operator")
    plan = _valid_plan(other, h2, with_code=False)
    plan["evaluators"][1]["name"] = "late_clash"
    op_id, *_ = _approve(other, h2, plan=plan)
    _seed_judge("late_clash")
    assert _run(op_id, Fakes())
    res = _res(_op(op_id), "evaluator:pii")
    assert res["status"] == "conflict" and "cannot prove it created" in res["error"]
    assert res["create_history"][-1]["outcome"] == "conflict" and not res.get("owned")


def test_deleted_evaluators_free_their_names_after_cleanup(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, plan=_valid_plan(cid, h, with_code=False))
    assert _run(op_id, fakes)
    db = SessionLocal()
    try:
        ws = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert "kid_pii_judge" in assets.taken_evaluator_names(db, ws)
        op = assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id),
                                      workspace_context(ws), clients=fakes,
                                      sleeper=lambda s: None)
        assert op.status == "cleaned", op.error
        assert assets.taken_evaluator_names(db, ws) == set()
    finally:
        db.close()


def test_startup_resume_wakes_only_interrupted_operations(app_ready, monkeypatch):
    cid, h = _conversation("local-operator")
    queued_id, *_ = _approve(cid, h)
    other, h2 = _conversation("local-operator")
    done_id, *_ = _approve(other, h2, plan=_valid_plan(other, h2, with_code=False))
    assert _run(done_id, Fakes())
    woken: list[str] = []
    monkeypatch.setattr(assets, "start_async", lambda op_id, **kw: woken.append(op_id) or True)
    assert assets.resume_operations() == [queued_id]
    assert woken == [queued_id]


def test_retry_is_bounded_and_refuses_a_live_worker(app_ready):
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h)
    db = SessionLocal()
    try:
        op = db.get(EvaluationAssetOperation, op_id)
        op.status, op.attempts = "failed", assets.MAX_ATTEMPTS
        db.commit()
        with pytest.raises(AppError) as exc:
            assets.retry_operation(db, op)
        assert exc.value.code == "assistant.evaluation_assets_exhausted"
        op.attempts = 1
        db.commit()
        with assets._flock(op_id) as held:  # another process holds the host lock
            assert held
            with pytest.raises(AppError) as exc:
                assets.retry_operation(db, op)
            assert exc.value.code == "assistant.evaluation_assets_running"
    finally:
        db.close()


# ===========================================================================
# 4. cleanup
# ===========================================================================


def test_cleanup_deletes_rows_and_the_lambda_chain_but_keeps_the_dataset(app_ready, code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h)
    assert _run(op_id, fakes)
    assert len(_rows()) == 2
    fn_name = _res(_op(op_id), "lambda_function")["name"]
    db = SessionLocal()
    try:
        op = assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id), _ws_ctx(),
                                      clients=fakes, sleeper=lambda s: None)
        assert op.status == "cleaned", op.error
        statuses = {r["key"]: r["status"] for r in op.resources}
        assert statuses["dataset"] == "ready" and statuses["existing:helpfulness"] == "ready"
        assert {statuses[k] for k in ("evaluator:pii", "evaluator:tools", "lambda_function",
                                      "log_group", "lambda_role")} == {"deleted"}
        assert db.query(EvalDataset).count() == 1  # the member's Dataset stays
        assert db.query(EvalEvaluator).count() == 0
        # idempotent: a second cleanup is a no-op on a cleaned operation
        assert assets.cleanup_operation(db, op, _ws_ctx(), clients=fakes).status == "cleaned"
    finally:
        db.close()
    assert fn_name not in fakes.lam.functions and fn_name not in fakes.iam.roles
    assert f"/aws/lambda/{fn_name}" not in fakes.logs.groups


def test_cleanup_leaves_changed_rows_and_replaced_functions_as_conflicts(app_ready, code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h)
    assert _run(op_id, fakes)
    fn = _res(_op(op_id), "lambda_function")
    db = SessionLocal()
    try:
        judge = next(r for r in db.query(EvalEvaluator).all() if r.name == "kid_pii_judge")
        judge.level = "SESSION"  # an operator changed the owned row afterwards
        db.commit()
        fakes.lam.functions[fn["name"]]["CodeSha256"] = "replaced"  # someone redeployed the code
        op = assets.cleanup_operation(db, db.get(EvaluationAssetOperation, op_id), _ws_ctx(),
                                      clients=fakes, sleeper=lambda s: None)
        assert op.status == "partial" and "evaluator:pii" in op.error
        statuses = {r["key"]: r["status"] for r in op.resources}
        assert statuses["evaluator:pii"] == "conflict" and statuses["evaluator:tools"] == "deleted"
        assert statuses["lambda_function"] == "conflict"
        # dependents of an unresolved function are retained, never deleted
        assert statuses["log_group"] == "retained" and statuses["lambda_role"] == "retained"
        assert db.query(EvalEvaluator).count() == 1
    finally:
        db.close()
    assert fn["name"] in fakes.lam.functions and fn["name"] in fakes.iam.roles


def test_cleanup_refuses_while_the_operation_is_queued_or_running(app_ready):
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h)
    db = SessionLocal()
    try:
        op = db.get(EvaluationAssetOperation, op_id)
        with pytest.raises(AppError) as exc:  # queued: not cleanable yet
            assets.cleanup_operation(db, op, _ws_ctx(), clients=Fakes())
        assert exc.value.code == "assistant.evaluation_assets_running"
    finally:
        db.close()


# ===========================================================================
# 5. the ordinary Evaluation surfaces honour managed evaluators
# ===========================================================================


def test_ordinary_evaluator_delete_refuses_operation_owned_records(app_ready):
    cid, h = _conversation("local-operator")
    op_id, *_ = _approve(cid, h, plan=_valid_plan(cid, h, with_code=False))
    _run(op_id, Fakes())
    eid = _res(_op(op_id), "evaluator:pii")["result"]["evaluator_id"]
    with TestClient(app_ready) as client:
        res = client.delete(f"/api/eval/evaluators/{eid}")
        assert res.status_code == 409 and res.json()["code"] == "evaluator.managed_by_operation"
        assert res.json()["detail"]["operation_id"] == op_id
        assert client.get(f"/api/eval/evaluators/{eid}").status_code == 200
    assert len(_rows()) == 1


def test_managed_code_evaluators_expose_their_rules_and_reference_gap(app_ready, code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, plan=_valid_plan(cid, h, reference=True))
    assert _run(op_id, fakes)
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    code_id = _res(op, "evaluator:tools")["result"]["evaluator_id"]
    db = SessionLocal()
    try:
        owner = assets.managed_evaluator(db, DEFAULT_WORKSPACE_ID, code_id)
        assert owner["operation_id"] == op_id and owner["definition"] == "code"
        assert owner["reference_dependent"] is True and owner["level"] == "SESSION"
        rules = assets.managed_rules(db, owner)
        assert [c["type"] for c in rules] == ["reference_trajectory", "tool_set"]
        assert assets.managed_package_evaluator_count(db, owner) == 1
        # a live session carries no ground truth: SCORE NOW / online configs refuse it
        assert assets.managed_reference_gap(db, DEFAULT_WORKSPACE_ID, [code_id], set()) == {
            code_id: ["expected_tool_trajectory"]}
        assert assets.managed_reference_gap(
            db, DEFAULT_WORKSPACE_ID, [code_id], {"expected_tool_trajectory"}) == {}
        # the lookups the evaluation package routes through resolve to the same owner
        from app.evaluation import managed

        assert managed.managed_evaluator(db, DEFAULT_WORKSPACE_ID, code_id) == owner
        assert managed.managed_evaluator(db, "other-ws", code_id) is None
    finally:
        db.close()


def test_a_second_plan_binds_the_managed_code_evaluator_as_existing(app_ready, code_enabled):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h)
    assert _run(op_id, fakes)
    code_id = _res(_op(op_id), "evaluator:tools")["result"]["evaluator_id"]
    other, h2 = _conversation("local-operator")
    plan = _valid_plan(other, h2, with_code=False)
    plan["evaluators"][1]["name"] = "kid_pii_judge_2"
    plan["evaluators"].append({"kind": "existing", "key": "tools", "title": "managed rules",
                               "evaluator_id": code_id, "golden_test_ids": []})
    plan["recommendations"][2] = {**plan["recommendations"][2], "status": "mapped",
                                  "mapped_to": ["tools"]}
    op2_id, *_ = _approve(other, h2, plan=plan)
    assert _run(op2_id, fakes)
    op2 = _op(op2_id)
    assert op2.status == "succeeded", op2.error
    bound = _res(op2, "existing:tools")["result"]
    assert bound["source"] == "managed" and bound["definition"] == "code"
    assert bound["reference_needs"] == [] and op_id in bound["note"]
    # an unknown custom id is a plain failure of that reference, never a create
    third, h3 = _conversation("local-operator")
    plan3 = _valid_plan(third, h3, with_code=False)
    plan3["evaluators"][1]["name"] = "kid_pii_judge_3"
    plan3["evaluators"][0] = {**plan3["evaluators"][0], "evaluator_id": "cust_missing00"}
    op3_id, *_ = _approve(third, h3, plan=plan3)
    assert _run(op3_id, fakes)
    missing = _res(_op(op3_id), "existing:helpfulness")
    assert missing["status"] == "failed" and "does not exist in this workspace" in missing["error"]


# ===========================================================================
# 6. routes (login gate on) and the conversation purge
# ===========================================================================


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    app = create_app()
    _ready()
    _install_preset()
    with (
        TestClient(app, client=("127.0.0.1", 4321)) as admin,
        TestClient(app, client=("127.0.0.1", 4321)) as member,
    ):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        member_id = _activate(MEMBER_CREDS, member)
        yield admin, member, member_id
    get_settings.cache_clear()


def _url(cid: str, tail: str = "") -> str:
    return f"{BASE}/conversations/{cid}/evaluation-plan{tail}"


def _set_member_permissions(user_id: str, permissions: dict[str, bool] | None) -> None:
    db = SessionLocal()
    try:
        db.get(User, user_id).permissions = permissions
        db.commit()
    finally:
        db.close()


def test_member_without_deploy_permission_prepares_and_edits_but_cannot_materialize(
        gated, monkeypatch):
    admin, member, member_id = gated
    _set_member_permissions(member_id, {"agents.deploy": False})
    cid, h = _conversation(f"user:{member_id}", owner="member")
    monkeypatch.setattr(assets, "start_async", lambda *a, **k: None)
    res = member.post(_url(cid, "/prepare"), json={"revision": 1})
    assert res.status_code == 201, res.text
    plan = res.json()["plan"]
    assert plan["status"] == "invalid"  # legacy draft: every scenario needs review first
    assert any("need review" in e for e in plan["validation_errors"])
    assert plan["summary"] is None
    assert member.get(BASE).json()["can_materialize_evaluation_assets"] is False
    edited = _valid_plan(cid, h, with_code=False)
    res = member.put(_url(cid), json={"content": edited})
    assert res.status_code == 200 and res.json()["plan"]["revision"] == 2
    assert res.json()["plans"][0]["status"] == "superseded"
    assert "disclosure" in res.json()
    res = member.put(_url(cid), json={"content": {**edited, "evaluators": {"x": 1}}})
    assert res.status_code == 200 and res.json()["plan"]["status"] == "invalid"
    body = {"plan_revision": 2, "plan_hash": res.json()["plans"][1]["content_hash"],
            "acknowledge_disclosure": True}
    res = member.post(_url(cid, "/materialize"), json=body)
    assert res.status_code == 403 and res.json()["code"] == "auth.permission_required"
    assert _count(EvaluationAssetOperation) == 0
    # an administrator reads the member's plan (read-only) but cannot materialize it
    assert admin.get(_url(cid)).status_code == 200
    assert admin.post(_url(cid, "/materialize"), json=body).status_code == 404
    snap = _snapshot_proposal(cid)
    assert snap["status"] == "approved" and snap["hash"] == h


def test_admin_owner_materializes_own_plan_with_disclosure_and_exact_hash(gated, monkeypatch):
    admin, member, _ = gated
    cid, h = _conversation("config-admin", owner="admin")
    launched: list[str] = []
    monkeypatch.setattr(assets, "start_async", lambda op_id, **kw: launched.append(op_id))
    res = admin.put(_url(cid), json={"content": _valid_plan(cid, h, with_code=False)})
    assert res.status_code == 200, res.text
    plan = res.json()["plan"]
    assert admin.get(BASE).json()["can_materialize_evaluation_assets"] is True
    res = admin.post(_url(cid, "/materialize"), json={
        "plan_revision": plan["revision"], "plan_hash": plan["content_hash"],
        "acknowledge_disclosure": False})
    assert res.status_code == 422 and res.json()["code"] == "assistant.disclosure_required"
    res = admin.post(_url(cid, "/materialize"), json={
        "plan_revision": plan["revision"], "plan_hash": "f" * 64, "acknowledge_disclosure": True})
    assert res.status_code == 409 and res.json()["code"] == "assistant.evaluation_plan_stale"
    body = {"plan_revision": plan["revision"], "plan_hash": plan["content_hash"],
            "acknowledge_disclosure": True}
    res = admin.post(_url(cid, "/materialize"), json=body)
    assert res.status_code == 202, res.text
    op = res.json()["operation"]
    assert op["status"] == "queued" and launched == [op["id"]]
    assert op["pinned"]["account_id"] and "external_id" not in op["pinned"]
    res = admin.post(_url(cid, "/materialize"), json=body)
    assert res.status_code == 200 and res.json()["operation"]["id"] == op["id"]
    res = admin.get(_url(cid, f"/operations/{op['id']}"))
    assert res.status_code == 200 and res.json()["operation"]["plan_hash"] == plan["content_hash"]
    assert admin.get(_url(cid)).json()["plans"][0]["status"] == "approved"
    assert member.get(_url(cid, f"/operations/{op['id']}")).status_code == 404
    assert member.delete(_url(cid, f"/operations/{op['id']}/assets")).status_code == 404
    # the operation runs with the real worker (ledger only: no code evaluators); retry and
    # cleanup then answer through the routes
    assert _run(op["id"], Fakes())
    res = admin.post(_url(cid, f"/operations/{op['id']}/retry"))
    assert res.status_code == 200 and res.json()["started"] is False
    res = admin.delete(_url(cid, f"/operations/{op['id']}/assets"))
    assert res.status_code == 200 and res.json()["operation"]["status"] == "cleaned"
    snap = _snapshot_proposal(cid)
    assert snap["status"] == "approved" and snap["hash"] == h and snap["agent_id"] == "agent-1"


def test_member_with_deploy_permission_materializes_own_plan(gated, monkeypatch):
    _, member, member_id = gated
    cid, h = _conversation(f"user:{member_id}", owner="member")
    launched: list[str] = []
    monkeypatch.setattr(assets, "start_async", lambda op_id, **kw: launched.append(op_id))
    assert member.get(BASE).json()["can_materialize_evaluation_assets"] is True
    res = member.put(_url(cid), json={"content": _valid_plan(cid, h, with_code=False)})
    assert res.status_code == 200, res.text
    plan = res.json()["plan"]
    body = {"plan_revision": plan["revision"], "plan_hash": plan["content_hash"],
            "acknowledge_disclosure": True}
    res = member.post(_url(cid, "/materialize"), json=body)
    assert res.status_code == 202, res.text
    op = res.json()["operation"]
    assert op["status"] == "queued" and launched == [op["id"]]
    db = SessionLocal()
    try:
        row = db.get(EvaluationAssetOperation, op["id"])
        assert row.approver_user_id == member_id
        assert row.approved_by == MEMBER_CREDS["username"]
    finally:
        db.close()
    _set_member_permissions(member_id, {"agents.deploy": False})
    db = SessionLocal()
    try:
        reason = assets.approver_authorized(db, db.get(EvaluationAssetOperation, op["id"]))
    finally:
        db.close()
    assert reason and "agents.deploy" in reason
    for res in (member.post(_url(cid, f"/operations/{op['id']}/retry")),
                member.delete(_url(cid, f"/operations/{op['id']}/assets"))):
        assert res.status_code == 403 and res.json()["code"] == "auth.permission_required"


def test_plan_prepare_refuses_invalid_revision_and_foreign_conversation(app_ready):
    cid, h = _conversation("local-operator", proposal={"name": 5}, status="invalid")
    with TestClient(app_ready) as client:
        res = client.post(_url(cid, "/prepare"), json={"revision": 1})
        assert res.status_code == 409
        assert res.json()["code"] == "assistant.evaluation_plan_source_invalid"
        res = client.post(_url(cid, "/prepare"), json={"revision": 7})
        assert res.status_code == 409 and res.json()["code"] == "assistant.proposal_stale"
    other, _ = _conversation("user:someone-else")
    with TestClient(app_ready) as client:
        # the open console's operator is an administrator: it reads another
        # principal's plan, read-only, and still cannot prepare one there
        assert client.get(_url(other)).status_code == 200
        assert client.post(_url(other, "/prepare"), json={"revision": 1}).status_code == 404


def test_plan_revisions_are_append_only_and_hash_bound(app_ready):
    cid, h = _conversation("local-operator")
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, cid)
        first = assets.prepare_plan(db, conv, revision=1, created_by="river")
        second = assets.edit_plan(db, conv, _valid_plan(cid, h), created_by="river")
        assert (first.revision, second.revision) == (1, 2)
        db.expire_all()
        assert db.get(AssistantEvaluationPlan, first.id).status == "superseded"
        assert second.content_hash == plan_contract.canonical_hash(second.content)
        assert second.source_content_hash == h
    finally:
        db.close()


def test_the_lambda_revision_review_route_is_not_part_of_the_cn_build(app_ready):
    """CA-5: the SE-049 CloudTrail recovery route has no CN twin (evaluators are rows)."""
    paths = set(app_ready.openapi()["paths"])
    assert not any("lambda-revision-review" in p for p in paths)
    assert f"{BASE}/conversations/{{conversation_id}}/evaluation-plan/materialize" in paths


def test_conversation_purge_cleans_operations_deletes_datasets_then_rows(app_ready):
    cid, h = _conversation("local-operator")
    fakes = Fakes()
    op_id, *_ = _approve(cid, h, plan=_valid_plan(cid, h, with_code=False))
    assert _run(op_id, fakes)
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        fp = purge.footprint(db, row)
        assert fp["operations"][0]["status"] == "succeeded" and len(fp["datasets"]) == 1
        assert fp["required_permissions"] == ["agents.deploy"]  # no deployed Agent row here
        assert fp["blockers"] == []
        with pytest.raises(AppError) as exc:
            purge.purge(db, row, _ws_ctx(), can=lambda p: p != "agents.deploy", clients=fakes)
        assert exc.value.code == "auth.permission_required"
        result = purge.purge(db, row, _ws_ctx(), can=lambda p: True, clients=fakes)
        assert result["operations_cleaned"] == [op_id] and len(result["datasets"]) == 1
        assert db.query(EvaluationAssetOperation).count() == 0
        assert db.query(AssistantEvaluationPlan).count() == 0
        assert db.query(EvalDataset).count() == 0 and db.query(EvalEvaluator).count() == 0
        assert db.get(AssistantConversation, cid) is None
    finally:
        db.close()


def test_conversation_purge_refuses_while_an_operation_runs(app_ready):
    cid, h = _conversation("local-operator")
    _approve(cid, h)  # queued, never run
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        fp = purge.footprint(db, row)
        assert [b["kind"] for b in fp["blockers"]] == ["operation"]
        with pytest.raises(AppError) as exc:
            purge.purge(db, row, _ws_ctx(), can=lambda p: True, clients=Fakes())
        assert exc.value.code == "assistant.conversation_busy"
        assert db.get(AssistantConversation, cid) is not None
    finally:
        db.close()


def test_materialized_trajectories_carry_the_agents_runtime_tool_names(app_ready):
    """N17-obs375 (DIFF-GLOBAL-EVAL B4): the catalogue lists a Gateway tool by its declared
    ``<record>___<tool>`` name, but the Harness mounts the shared Gateway under the
    ``launchpad_gw`` alias and the agent calls ``launchpad_gw_<record>___<tool>`` — a dataset
    that kept the declared name failed every trajectory matcher, on Global and on ours."""
    cid, h = _conversation("local-operator")
    op_id, _, _, started = _approve(cid, h)
    assert started and _run(op_id, Fakes())
    op = _op(op_id)
    assert op.status == "succeeded", op.error
    db = SessionLocal()
    try:
        items = {i["scenario_id"]: i for i in db.get(EvalDataset, op.dataset_id).items}
    finally:
        db.close()
    assert items["GT-002"]["expected_trajectory"] == [f"launchpad_gw_{WEATHER_TOOL}"]
