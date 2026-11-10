"""N17-obs393 / obs394 / obs395 — switching ``eval_code_evaluators_enabled`` on actually
enables code evaluators end to end (deploy-ops-42, 2026-10-23).

* obs393: the deployed eval worker reads ``LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED`` from its
  runtime environment, but the preset spec env carried only the release tag. The flag was in
  neither the desired spec nor the release digest, so turning it on changed nothing: bootstrap
  step 10 answered "already installed", and the worker role never got
  ``EvalWorkerCodeEvaluators``. Now the flag rides in the preset spec env: switching it on makes
  the desired spec differ, and the repair redeploys the worker with the env var and the grant.
  With the flag off the spec is byte-for-byte what it was (no churn on existing installs).
* obs394: the worker grant named ``function:launchpad-eval-*`` while the materializer creates
  ``launchpad-evalfn-…``. Both now use one shared prefix.
* obs395: the least-privilege hub role (``infra/ec2/launchpad-cn-ec2.yaml``) must allow every
  call the evaluation-assets materializer makes, read from its source, plus the hub's own
  ``lambda:InvokeFunction`` (SCORE NOW / in-process executor) — and nothing outside the prefix.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.assistant import evaluation_assets as assets
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, Workspace
from app.services import agent_iam
from app.system_agents import presets, service
from app.system_agents.presets import EVAL_WORKER
from app.templates import eval_worker as ew
from tests.consistency import iam_policy_eval as ev
from tests.test_system_agents_auto_repair import (  # noqa: F401 — autouse fixtures
    _install,
    _mark_ready,
    _settle,
    _stamp,
    fixture_template,
    fresh_scheduler_slot,
    no_aws_clients,
    no_real_deploy,
)

ACCOUNT, REGION, PARTITION = "123456789012", "cn-northwest-1", "aws-cn"
FLAG = "LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED"


@pytest.fixture
def code_flag(monkeypatch):
    def set_flag(on: bool) -> None:
        monkeypatch.setenv(FLAG, "1" if on else "0")
        get_settings.cache_clear()

    yield set_flag
    get_settings.cache_clear()


CTX = agent_iam.RoleContext(account_id=ACCOUNT, region=REGION,
                            artifacts_bucket="launchpad-artifacts",
                            ecr_repo_arn=f"arn:{PARTITION}:ecr:{REGION}:{ACCOUNT}:repository/r",
                            partition=PARTITION, dns_suffix="amazonaws.com.cn")


def _arn(service_name: str, resource: str, region: str = REGION) -> str:
    return f"arn:{PARTITION}:{service_name}:{region}:{ACCOUNT}:{resource}"


# ─── obs394: one prefix ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("op_id", "group"), [
    ("op_0123456789", None), ("op_abcdefabcd", "kid_tools"), ("x", "a" * 40)])
def test_every_materialized_function_name_matches_the_worker_grant(op_id, group):
    name = assets.function_name(op_id, group)
    [grant] = [s for s in agent_iam.eval_worker_statements(CTX, code_evaluators=True)
               if s["Sid"] == "EvalWorkerCodeEvaluators"]
    for arn in (_arn("lambda", f"function:{name}"), _arn("lambda", f"function:{name}:1")):
        assert any(ev.resource_matches(pattern, arn) for pattern in grant["Resource"]), arn
    assert name.startswith(agent_iam.CODE_EVALUATOR_FUNCTION_PREFIX)
    # narrow: never wider than launchpad-eval* (no other function of the account)
    assert agent_iam.CODE_EVALUATOR_FUNCTION_PREFIX.startswith("launchpad-eval")
    assert not any(ev.resource_matches(p, _arn("lambda", "function:launchpad-other"))
                   for p in grant["Resource"])


# ─── obs393: the flag reaches the deployed worker ───────────────────────────────────


def test_the_flag_rides_in_the_worker_spec_env_only_when_on(code_flag):
    code_flag(False)
    off = presets.build_spec(EVAL_WORKER, "bucket", EVAL_WORKER.default_options())
    assert set(off.env) == {ew.RELEASE_ENV}  # unchanged: no churn on existing installs
    code_flag(True)
    on = presets.build_spec(EVAL_WORKER, "bucket", EVAL_WORKER.default_options())
    assert on.env == {**off.env, ew.CODE_EVALUATORS_ENV: "1"}
    assert ew.CODE_EVALUATORS_ENV == FLAG  # the name main.py reads
    assert f'os.environ.get("{ew.CODE_EVALUATORS_ENV}"' in (
        ew.TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")


def _repair_default_worker() -> service.InstallOutcome:
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        outcome = service.install_preset(db, row, EVAL_WORKER, None)
        db.commit()
        return outcome
    finally:
        db.close()


def test_switching_the_flag_on_redeploys_the_worker_with_the_env_and_the_grant(
        client, code_flag):
    code_flag(False)
    _mark_ready()
    agent_id = _install(client, EVAL_WORKER.key)
    _settle(agent_id, pin=_stamp())
    assert not _repair_default_worker().changed  # flag off: "already installed"

    code_flag(True)
    outcome = _repair_default_worker()
    assert outcome.changed and outcome.job is not None  # the spec differs → a repair job
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        spec = presets.AgentSpec(**agent.spec)
    assert spec.env[FLAG] == "1"
    sids = {s["Sid"] for s in agent_iam.preset_statements(agent, spec, CTX)}
    assert "EvalWorkerCodeEvaluators" in sids  # ensure_role re-puts the caps with the Sid


# ─── obs395: the hub role allows what the materializer calls ────────────────────────

_SERVICE = {"iam": "iam", "lam": "lambda", "logs": "logs"}
# calls whose request carries more than the operation itself (IAM authorises them apart)
_IMPLIED = {
    ("lambda", "CreateFunction"): [("iam", "PassRole"), ("lambda", "TagResource")],
    ("iam", "CreateRole"): [("iam", "TagRole")],
    ("logs", "CreateLogGroup"): [("logs", "TagResource"), ("logs", "TagLogGroup")],
}


def _camel(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def materializer_actions() -> set[tuple[str, str]]:
    source = Path(assets.__file__).read_text(encoding="utf-8")
    calls = {(_SERVICE[c], _camel(op))
             for c, op in re.findall(r"\b(iam|lam|logs)\.([a-z_]+)\b", source)}
    assert ("lambda", "CreateFunction") in calls and ("iam", "CreateRole") in calls
    for call in list(calls):
        calls.update(_IMPLIED.get(call, []))
    # the hub invokes the published version itself (SCORE NOW, in-process executor)
    calls.add(("lambda", "InvokeFunction"))
    return calls


def _resource(service_name: str, name: str) -> str:
    if service_name == "iam":
        return f"arn:{PARTITION}:iam::{ACCOUNT}:role/{name}"
    if service_name == "logs":
        return _arn("logs", f"log-group:/aws/lambda/{name}")
    return _arn("lambda", f"function:{name}")


def _hub_statements() -> list[ev.Statement]:
    template = ev.load_template()
    scope = ev.template_scope(template, region=REGION, account=ACCOUNT, partition=PARTITION,
                              parameters={"HubAdminAccess": "false"})
    return ev.instance_role_statements(template, scope)


CHAIN_SIDS = ("LambdaForCodeEvaluators", "LogsForCodeEvaluators", "IamForLaunchpadRoles")


@pytest.mark.parametrize("only_chain", [False, True])
def test_the_hub_role_allows_every_call_of_the_code_evaluator_chain(only_chain):
    statements = _hub_statements()
    if only_chain:  # not resting on ObservabilityForBootstrap's logs:* (or anything else)
        statements = [s for s in statements if s.sid in CHAIN_SIDS]
    name = assets.function_name("op_0123456789", "kid_tools")
    denied = [(svc, action) for svc, action in sorted(materializer_actions())
              if not ev.evaluate(statements, f"{svc}:{action}", _resource(svc, name)).allowed]
    assert denied == []
    version = _arn("lambda", f"function:{name}:3")
    assert ev.evaluate(statements, "lambda:InvokeFunction", version).allowed


def test_the_hub_lambda_grant_stays_inside_the_prefix():
    statements = _hub_statements()
    [lam] = [s for s in statements if s.sid == "LambdaForCodeEvaluators"]
    assert lam.resources == (
        f"arn:{PARTITION}:lambda:*:{ACCOUNT}:function:"
        f"{agent_iam.CODE_EVALUATOR_FUNCTION_PREFIX}*",)
    for action in ("lambda:CreateFunction", "lambda:InvokeFunction", "lambda:DeleteFunction"):
        assert not ev.evaluate(statements, action, _arn("lambda", "function:prod-app")).allowed
        assert not ev.evaluate(statements, action,
                               _arn("lambda", "function:launchpad-memory-extractor")).allowed
