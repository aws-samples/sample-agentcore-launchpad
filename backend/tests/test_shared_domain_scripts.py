"""D17 — the shared OpenSearch domain stack in `scripts/bootstrap.py` and
`scripts/teardown.py`.

bootstrap: `launchpad-memory-domain` is part of the "deploy the CDK app when a stack is
missing" check, so the first environment creates it and every later one (whose base and
memory stacks are missing but whose domain already exists) still runs `cdk deploy --all`
— which finds the domain stack unchanged (idempotent) and only creates the two
per-environment stacks.

teardown: per-environment stacks first (memory, then base — R21), the domain stack last,
opt-in (`--delete-shared-domain`) and only when no other `launchpad-memory[-env]` stack
remains, since every environment's indices live in that one domain.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from app.core import naming
from tests.test_teardown_script import StubCloudFormation, StubControl, StubIam

SCRIPTS = Path(__file__).parents[2] / "scripts"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"launchpad_{name}_script_d17", SCRIPTS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env_default(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "")


@pytest.fixture
def env_t1(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")


# ── bootstrap ────────────────────────────────────────────────────────────────


def test_bootstrap_expects_base_domain_and_memory_in_deploy_order(env_t1):
    script = _load_script("bootstrap")
    assert script.expected_stacks() == (
        "launchpad-base-t1", "launchpad-memory-domain", "launchpad-memory-t1",
    )


def test_bootstrap_deploys_when_only_the_domain_stack_is_missing(
    env_default, monkeypatch, capsys
):
    """First environment ever: base + memory may have been pre-provisioned (deploy.md §2)
    but the domain stack is new with D17 — it must still trigger `cdk deploy --all`."""
    script = _load_script("bootstrap")
    present = {"launchpad-base", "launchpad-memory"}
    monkeypatch.setattr(
        script, "stack_exists", lambda region, name=None: (name or "launchpad-base") in present
    )
    assert script.missing_stacks("cn-northwest-1") == ["launchpad-memory-domain"]
    calls = []
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    monkeypatch.delenv("LAUNCHPAD_CDK_CONTEXT", raising=False)
    script.deploy_cdk(script.missing_stacks("cn-northwest-1"))
    assert calls == [["uv", "run", "cdk", "deploy", "--all", "--require-approval", "never"]]
    assert "launchpad-memory-domain not found" in capsys.readouterr().out


def test_bootstrap_second_environment_finds_the_domain_and_deploys_its_own_stacks(
    env_t1, monkeypatch
):
    script = _load_script("bootstrap")
    present = {"launchpad-base", "launchpad-memory", "launchpad-memory-domain"}  # env "" exists
    monkeypatch.setattr(
        script, "stack_exists",
        lambda region, name=None: (name or "launchpad-base-t1") in present,
    )
    assert script.missing_stacks("cn-northwest-1") == ["launchpad-base-t1", "launchpad-memory-t1"]


def test_bootstrap_nothing_missing_skips_cdk(env_default, monkeypatch):
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "stack_exists", lambda region, name=None: True)
    assert script.missing_stacks("cn-northwest-1") == []


# ── N11-obs44b: the --with-cdk precondition in scripts/bootstrap.py ─────────


def _wire_precheck(script, monkeypatch, *, status: str, detail: str = "d", domain_exists=True):
    """Stub the AWS clients the precondition reads and the check's verdict; returns the
    recorder of what `check_memory_admin_assume` was asked."""
    asked: list[dict] = []
    sts = type("Sts", (), {"get_caller_identity": staticmethod(lambda: {
        "Account": "111",
        "Arn": "arn:aws-cn:sts::111:assumed-role/launchpad-cn-ops-cn-northwest-1-t1/i-0",
    })})()
    iam = object()
    clients = {"sts": sts, "iam": iam}
    monkeypatch.setattr(script.bs, "_client", lambda service, region: clients[service])
    monkeypatch.setattr(
        script, "stack_exists",
        lambda region, name=None: domain_exists if name == "launchpad-memory-domain" else True,
    )

    def fake_check(sts_, iam_, **kwargs):
        assert sts_ is sts and iam_ is iam
        asked.append(kwargs)
        return status, detail

    monkeypatch.setattr(script.mb, "check_memory_admin_assume", fake_check)
    return asked


def test_bootstrap_check_memory_admin_flag_denied_exits_1_with_the_reason(
    env_t1, monkeypatch, capsys
):
    """`host-update.sh --with-cdk` runs exactly this before `cdk deploy`."""
    script = _load_script("bootstrap")
    reason = "cannot assume the memory admin role arn:aws-cn:iam::111:role/x (AccessDenied …)"
    asked = _wire_precheck(script, monkeypatch, status="denied", detail=reason)
    monkeypatch.setattr(script, "ensure_agentcore_cli",
                        lambda: pytest.fail("the flag must not install the CLI"))
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: pytest.fail("no cdk"))
    monkeypatch.setattr(script.sys, "argv",
                        ["bootstrap.py", "--region", "cn-northwest-1", "--check-memory-admin"])
    assert script.main() == 1
    out = capsys.readouterr().out
    assert f"memory-admin precondition: denied — {reason}" in out
    assert "aborting before cdk deploy" in out and "N11-obs44b" in out
    assert asked == [{
        "admin_role_arn": "arn:aws-cn:iam::111:role/launchpad-memory-admin-cn-northwest-1",
        "caller_arn": "arn:aws-cn:sts::111:assumed-role/launchpad-cn-ops-cn-northwest-1-t1/i-0",
        "domain_exists": True,
    }]


def test_bootstrap_check_memory_admin_flag_ok_exits_0(env_default, monkeypatch, capsys):
    script = _load_script("bootstrap")
    _wire_precheck(script, monkeypatch, status="ok", detail="assumed arn:x", domain_exists=False)
    monkeypatch.setattr(script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1",
                                             "--check-memory-admin"])
    assert script.main() == 0
    assert "memory-admin precondition: ok — assumed arn:x" in capsys.readouterr().out


def test_bootstrap_aborts_before_cdk_deploy_when_the_precondition_is_denied(
    env_t1, monkeypatch, capsys
):
    """The D17 migration path (domain stack missing, no --skip-cdk): the deploy that would
    end in a degraded memory stage 16 min later is not started."""
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    _wire_precheck(script, monkeypatch, status="denied", detail="reason", domain_exists=False)
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: pytest.fail("cdk must not run"))
    monkeypatch.setattr(script.bs, "run_bootstrap",
                        lambda region: pytest.fail("the stages must not run"))
    monkeypatch.setattr(script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1"])
    assert script.main() == 1
    out = capsys.readouterr().out
    assert "memory-admin precondition: denied — reason" in out
    assert "aborting before cdk deploy" in out


@pytest.mark.parametrize("status", ["ok", "unknown"])
def test_bootstrap_deploys_after_an_ok_or_undecided_precondition(
    env_t1, monkeypatch, capsys, status
):
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    _wire_precheck(script, monkeypatch, status=status, detail="d", domain_exists=False)
    ran: list[list[str]] = []
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: ran.append(a[0]))
    monkeypatch.delenv("LAUNCHPAD_CDK_CONTEXT", raising=False)
    monkeypatch.setattr(script.bs, "run_bootstrap", lambda region: _minimal_summary())
    monkeypatch.setattr(script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1"])
    assert script.main() == 0
    out = capsys.readouterr().out
    assert ran and ran[0][:4] == ["uv", "run", "cdk", "deploy"]
    assert out.index("memory-admin precondition") < out.index("running cdk deploy --all")
    assert ("WARNING: memory-admin precondition undecided" in out) == (status == "unknown")


def test_bootstrap_skip_cdk_never_runs_the_precondition(env_default, monkeypatch):
    """Code-only updates (host-update step 10) deploy nothing, so nothing to pre-check."""
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    monkeypatch.setattr(script, "stack_exists", lambda region, name=None: True)
    monkeypatch.setattr(script, "precheck_memory_admin",
                        lambda region: pytest.fail("no precheck with --skip-cdk"))
    monkeypatch.setattr(script.bs, "run_bootstrap", lambda region: _minimal_summary())
    monkeypatch.setattr(script.sys, "argv",
                        ["bootstrap.py", "--region", "cn-northwest-1", "--skip-cdk"])
    assert script.main() == 0


def _minimal_summary():
    return {
        "account_id": "111", "region": "cn-northwest-1",
        "stack_outputs": {
            "ArtifactsBucketName": "b", "EcrRepoUri": "e", "CodeBuildProjectName": "c",
        },
    }


def test_bootstrap_skip_cdk_continues_on_a_legacy_memory_layout(
    env_default, monkeypatch, capsys
):
    """Pre-D17 environment (live default/t1 on 2026-10-10): only the shared domain stack is
    missing while launchpad-memory still owns its domain — a code-only update with
    --skip-cdk must warn and run the stages, never abort or deploy."""
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    monkeypatch.setattr(
        script, "stack_exists",
        lambda region, name=None: (name or "launchpad-base") != "launchpad-memory-domain",
    )
    ran = []
    monkeypatch.setattr(
        script.bs, "run_bootstrap", lambda region: ran.append(region) or _minimal_summary()
    )
    monkeypatch.setattr(
        script.subprocess, "run", lambda *a, **kw: pytest.fail("cdk must not run")
    )
    monkeypatch.setattr(
        script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1", "--skip-cdk"]
    )
    assert script.main() == 0
    out = capsys.readouterr().out
    assert "legacy memory layout (pre-D17)" in out
    assert ran == ["cn-northwest-1"]


def _run_main_with_summary(script, monkeypatch, summary):
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    monkeypatch.setattr(script, "stack_exists", lambda region, name=None: True)
    monkeypatch.setattr(script.bs, "run_bootstrap", lambda region: summary)
    monkeypatch.setattr(
        script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1", "--skip-cdk"]
    )
    assert script.main() == 0


def test_bootstrap_summary_lists_a_reused_eval_worker(env_default, monkeypatch, capsys):
    """N12-obs32: on the "already installed on the current release" branch the stage result
    reached launchpad.yaml but not the CLI table — an operator could not see the worker ARN
    or release from a healthy re-run."""
    script = _load_script("bootstrap")
    summary = _minimal_summary() | {"evaluation": {
        "installed": True, "created": False, "changed": False, "agent_id": "a1",
        "runtime_arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/w-1",
        "runtime_id": "w-1", "release": "2026.10.1", "unavailable_reason": "",
    }}
    _run_main_with_summary(script, monkeypatch, summary)
    out = capsys.readouterr().out
    assert "eval worker          arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/w-1" in out
    assert "eval worker release  2026.10.1" in out
    assert "eval worker state    reused" in out


def test_bootstrap_summary_states_why_the_eval_worker_is_unavailable(
    env_default, monkeypatch, capsys
):
    script = _load_script("bootstrap")
    summary = _minimal_summary() | {"evaluation": {
        "installed": False, "created": False, "changed": False,
        "unavailable_reason": "ValueError: no deploy method registered for 'zip_runtime'",
    }}
    _run_main_with_summary(script, monkeypatch, summary)
    out = capsys.readouterr().out
    assert re.search(
        r"eval worker\s+unavailable · ValueError: no deploy method registered", out
    )
    assert "eval worker release" not in out


def test_bootstrap_summary_surfaces_the_memory_stage_warning(env_default, monkeypatch, capsys):
    """N-SCN-obs6 (e): when the memory stage skipped a provisioning step on a healthy
    substrate, the operator sees it in the CLI table next to the (empty) reason — not
    only in the stage's stdout scroll-back."""
    script = _load_script("bootstrap")
    summary = _minimal_summary() | {"memory": {
        "endpoint": "search-lp.cn-northwest-1.es.example", "created": [], "reused": [],
        "unavailable_reason": "",
        "warning": "substrate healthy, provisioning step skipped: ReadTimeoutError",
    }}
    _run_main_with_summary(script, monkeypatch, summary)
    out = capsys.readouterr().out
    assert re.search(r"memory domain\s+search-lp.cn-northwest-1.es.example", out)
    assert re.search(r"memory stage\s+0 created · 0 reused", out)
    assert re.search(
        r"memory stage WARN\s+substrate healthy, provisioning step skipped: ReadTimeoutError",
        out,
    )


def test_bootstrap_summary_has_no_warning_row_on_a_clean_memory_stage(
    env_default, monkeypatch, capsys
):
    script = _load_script("bootstrap")
    summary = _minimal_summary() | {"memory": {
        "endpoint": "search-lp.cn-northwest-1.es.example", "created": [],
        "reused": ["index launchpad-memory"], "unavailable_reason": "",
    }}
    _run_main_with_summary(script, monkeypatch, summary)
    assert "memory stage WARN" not in capsys.readouterr().out


def test_bootstrap_summary_marks_a_degraded_memory_stage_and_still_exits_0(
    env_t1, monkeypatch, capsys
):
    """N11-obs44c: a degraded memory stage (unhealthy probe, or N11-obs44a's refused
    AssumeRole) is a `memory stage DEGRADED` row and exit code 0 — host-update's step 10
    does not abort before the restart; the row says what to do."""
    script = _load_script("bootstrap")
    reason = ("cannot assume the memory admin role arn:aws-cn:iam::111:role/"
              "launchpad-memory-admin-cn-northwest-1 (AccessDenied on sts:AssumeRole) — …")
    summary = _minimal_summary() | {"memory": {
        "endpoint": "", "created": [], "reused": [], "unavailable_reason": reason,
    }}
    _run_main_with_summary(script, monkeypatch, summary)   # asserts main() == 0
    out = capsys.readouterr().out
    assert re.search(r"memory domain\s+cannot assume the memory admin role", out)
    assert re.search(r"memory stage DEGRADED\s+cannot assume the memory admin role", out)
    assert "the stages after memory ran; fix the cause and re-run `make bootstrap`" in out
    assert "memory stage WARN" not in out


def test_bootstrap_skip_cdk_still_aborts_when_a_non_legacy_stack_is_missing(
    env_default, monkeypatch, capsys
):
    script = _load_script("bootstrap")
    monkeypatch.setattr(script, "ensure_agentcore_cli", lambda: "0.21.1")
    present = {"launchpad-memory-domain"}  # base and memory both missing
    monkeypatch.setattr(
        script, "stack_exists", lambda region, name=None: (name or "launchpad-base") in present
    )
    monkeypatch.setattr(
        script.sys, "argv", ["bootstrap.py", "--region", "cn-northwest-1", "--skip-cdk"]
    )
    assert script.main() == 1
    assert "missing and --skip-cdk given" in capsys.readouterr().out


# ── teardown ─────────────────────────────────────────────────────────────────


class StubCloudFormationWithList(StubCloudFormation):
    def __init__(self, *, stacks: list[str], exists=True):
        super().__init__(exists=exists)
        self.stacks = stacks
        self.list_filters: list[list[str]] = []

    def get_paginator(self, op):
        assert op == "list_stacks"
        stub = self

        class _Paginator:
            def paginate(_self, **kwargs):
                stub.list_filters.append(kwargs["StackStatusFilter"])
                # two pages, to prove pagination is walked
                half = max(1, len(stub.stacks) // 2)
                yield {"StackSummaries": [{"StackName": n} for n in stub.stacks[:half]]}
                yield {"StackSummaries": [{"StackName": n} for n in stub.stacks[half:]]}

        return _Paginator()


def _wire(monkeypatch, teardown, cfn, *, existing: set[str]):
    clients = {"bedrock-agentcore-control": StubControl(), "iam": StubIam(), "cloudformation": cfn}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])

    def get_stack_outputs(region, name=None):
        name = name or teardown.bs.base_stack_name()
        if name not in existing:
            raise Exception("ValidationError: Stack does not exist")
        return {}

    monkeypatch.setattr(teardown.bs, "get_stack_outputs", get_stack_outputs)
    monkeypatch.delenv(teardown.EC2_STACK_NAME_ENV, raising=False)


def _stacks(targets):
    return [ident for kind, ident, _ in targets if kind == "cdk-stack"]


def test_teardown_keeps_the_shared_domain_by_default(env_default, monkeypatch):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(stacks=["launchpad-memory", "launchpad-memory-domain"])
    _wire(monkeypatch, teardown, cfn,
          existing={"launchpad-base", "launchpad-memory", "launchpad-memory-domain"})
    assert _stacks(teardown.collect_targets("cn-northwest-1")) == [
        "launchpad-memory", "launchpad-base",
    ]
    assert cfn.list_filters == []  # never even looked


def test_teardown_deletes_the_domain_last_when_asked_and_no_other_environment_remains(
    env_default, monkeypatch
):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(
        stacks=["launchpad-base", "launchpad-memory", "launchpad-memory-domain", "CDKToolkit"]
    )
    _wire(monkeypatch, teardown, cfn,
          existing={"launchpad-base", "launchpad-memory", "launchpad-memory-domain"})
    targets = teardown.collect_targets("cn-northwest-1", delete_shared_domain=True)
    assert _stacks(targets) == ["launchpad-memory", "launchpad-base", "launchpad-memory-domain"]
    assert targets[-1][0] == "cdk-stack" and "shared OpenSearch domain" in targets[-1][2]
    # only live stacks were consulted (a DELETE_COMPLETE sibling does not count)
    assert cfn.list_filters and "DELETE_COMPLETE" not in cfn.list_filters[0]


def test_teardown_refuses_the_domain_while_another_environment_uses_it(
    env_default, monkeypatch, capsys
):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(
        stacks=["launchpad-memory", "launchpad-memory-t2", "launchpad-memory-domain"]
    )
    _wire(monkeypatch, teardown, cfn,
          existing={"launchpad-base", "launchpad-memory", "launchpad-memory-domain"})
    targets = teardown.collect_targets("cn-northwest-1", delete_shared_domain=True)
    assert _stacks(targets) == ["launchpad-memory", "launchpad-base"]
    out = capsys.readouterr().out
    assert "keeping shared domain stack launchpad-memory-domain" in out
    assert "launchpad-memory-t2" in out


def test_teardown_of_a_named_environment_ignores_its_own_memory_stack_in_the_check(
    env_t1, monkeypatch
):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(stacks=["launchpad-memory-t1", "launchpad-memory-domain"])
    _wire(monkeypatch, teardown, cfn,
          existing={"launchpad-base-t1", "launchpad-memory-t1", "launchpad-memory-domain"})
    assert teardown.other_environment_memory_stacks(cfn) == []
    targets = teardown.collect_targets("cn-northwest-1", delete_shared_domain=True)
    assert _stacks(targets) == [
        "launchpad-memory-t1", "launchpad-base-t1", "launchpad-memory-domain",
    ]
    # …but the default environment's stack, if present, blocks it
    cfn.stacks.append("launchpad-memory")
    assert teardown.other_environment_memory_stacks(cfn) == ["launchpad-memory"]


def test_teardown_skips_the_domain_when_it_is_not_deployed(env_default, monkeypatch):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(stacks=["launchpad-memory"])
    _wire(monkeypatch, teardown, cfn, existing={"launchpad-base", "launchpad-memory"})
    targets = teardown.collect_targets("cn-northwest-1", delete_shared_domain=True)
    assert _stacks(targets) == ["launchpad-memory", "launchpad-base"]


@pytest.mark.parametrize("name,is_env_stack", [
    ("launchpad-memory", True),
    ("launchpad-memory-t1", True),
    ("launchpad-memory-abc", True),
    ("launchpad-memory-domain", False),
    ("launchpad-memory-abcd", False),       # longer than ENV_NAME_PATTERN allows
    ("launchpad-memory-extract-x", False),  # a queue name, not a stack
    ("launchpad-base-t1", False),
    ("other-launchpad-memory", False),
])
def test_environment_memory_stack_pattern(name, is_env_stack):
    teardown = _load_script("teardown")
    assert bool(teardown._ENV_MEMORY_STACK_RE.match(name)) is is_env_stack


def test_delete_domain_stack_uses_the_waited_cloudformation_path(env_default, monkeypatch):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(stacks=[])
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: cfn)
    calls = []
    monkeypatch.setattr(teardown.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    teardown.delete_target("cdk-stack", "launchpad-memory-domain", "cn-northwest-1")
    assert cfn.ops("delete_stack") == [{"StackName": "launchpad-memory-domain"}]
    assert cfn.waiters == [("stack_delete_complete", {
        "StackName": "launchpad-memory-domain",
        "WaiterConfig": teardown.MEMORY_STACK_DELETE_WAITER_CONFIG,
    })]
    assert calls == []  # not `cdk destroy`


def test_teardown_main_says_the_domain_is_kept_unless_asked(env_default, monkeypatch, capsys):
    teardown = _load_script("teardown")
    cfn = StubCloudFormationWithList(stacks=["launchpad-memory", "launchpad-memory-domain"])
    _wire(monkeypatch, teardown, cfn,
          existing={"launchpad-base", "launchpad-memory", "launchpad-memory-domain"})
    monkeypatch.setattr(
        teardown.sys, "argv", ["teardown.py", "--dry-run", "--region", "cn-northwest-1"]
    )
    assert teardown.main() == 0
    out = capsys.readouterr().out
    assert "is kept — pass --delete-shared-domain" in out
    assert "[cdk-stack] launchpad-memory-domain" not in out
    monkeypatch.setattr(
        teardown.sys, "argv",
        ["teardown.py", "--dry-run", "--delete-shared-domain", "--region", "cn-northwest-1"],
    )
    assert teardown.main() == 0
    out = capsys.readouterr().out
    assert "[cdk-stack] launchpad-memory-domain" in out
    assert out.index("[cdk-stack] launchpad-base") < out.index(
        "[cdk-stack] launchpad-memory-domain"
    )


def test_bootstrap_abort_line_for_a_non_host_caller_names_the_grant(
    env_t1, monkeypatch, capsys
):
    """r8-nits (reviewer nit A): an off-host operator running `--check-memory-admin` as an
    IAM user has no EC2 stack to update — the abort line names the AssumeRole grant."""
    script = _load_script("bootstrap")
    user = "arn:aws-cn:iam::111:user/operator"
    sts = type("Sts", (), {"get_caller_identity": staticmethod(
        lambda: {"Account": "111", "Arn": user})})()
    clients = {"sts": sts, "iam": object()}
    monkeypatch.setattr(script.bs, "_client", lambda service, region: clients[service])
    monkeypatch.setattr(script, "stack_exists", lambda region, name=None: True)
    monkeypatch.setattr(script.mb, "check_memory_admin_assume",
                        lambda *_a, **_kw: ("denied", "reason"))
    monkeypatch.setattr(script.sys, "argv",
                        ["bootstrap.py", "--region", "cn-northwest-1", "--check-memory-admin"])
    assert script.main() == 1
    out = capsys.readouterr().out
    assert (
        "aborting before cdk deploy: grant sts:AssumeRole on "
        f"role/launchpad-memory-admin-cn-northwest-1 to {user} (deploy.md §8.3)"
    ) in out
    assert "update the EC2 stack" not in out
