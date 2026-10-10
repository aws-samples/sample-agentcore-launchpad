"""Pre-restart in-flight inventory (app/services/inflight.py + scripts/inflight.py).

Seeds one in-flight row of every kind, plus a terminal twin of each that must not
be listed, then checks the outcome per row, the CLI exit codes, that the CLI is
read-only and AWS-free, and that the inventory cannot drift from the startup hooks
`create_app` runs or the job types `resume_pending_jobs` resumes.
"""

import ast
import hashlib
import importlib
import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import botocore.session
import pytest
from sqlalchemy import create_engine, select

from app.core import config as config_mod
from app.core.db import Base, SessionLocal, engine
from app.evaluation.models import EvalPipeline, EvalRecommendation, EvalRun
from app.evaluation.recommendations import PROVIDER_JOB_PREFIX
from app.models.assistant import AssistantConversation, EvaluationAssetOperation
from app.models.ledger import Job, PolicyChange
from app.optimization.models import Experiment, RuntimeCanary
from app.services import inflight
from app.services.inflight import (
    CLEARED_RETRY,
    FAILS_INTERRUPTED,
    FAILS_ON_READ,
    RECONCILES,
    RESTART_SAFE,
    RESUMES,
    UNHANDLED,
    collect_inflight,
)
from app.skill_lab.models import SkillLabJob

BACKEND = Path(__file__).resolve().parents[1]
MAIN_PY = BACKEND / "app" / "main.py"
PIPELINE_PY = BACKEND / "app" / "deployer" / "pipeline.py"

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _load_cli():
    spec = importlib.util.spec_from_file_location("inflight_cli", BACKEND / "scripts" /
                                                  "inflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    """The CLI module, reading the suite's ledger.

    Several test modules repoint LAUNCHPAD_DATABASE_URL at import time, so under
    xdist `get_settings()` may name another file than the bound engine; pin the CLI's
    settings to the engine the seeds were written through.
    """
    module = _load_cli()
    url = engine.url.render_as_string(hide_password=False)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(database_url=url))
    return module


def _add(*rows) -> None:
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _conversation(cid: str, **fields) -> AssistantConversation:
    return AssistantConversation(id=cid, workspace_id="default", owner="alice",
                                 title=f"conv {cid}", **fields)


def _asset_op(oid: str, status: str) -> EvaluationAssetOperation:
    return EvaluationAssetOperation(
        id=oid, workspace_id="default", conversation_id="conv-idle", plan_id=f"plan-{oid}",
        plan_hash="h", owner_principal="alice", approved_by="alice",
        account_id="111122223333", region="us-west-2", status=status,
    )


def _run(rid: str, status: str, batch: str | None = None) -> EvalRun:
    return EvalRun(id=rid, workspace_id="default", agent_id="a1", agent_name="agent",
                   status=status, batch_eval_id=batch)


def _rec(rid: str, rec_id: str, status: str) -> EvalRecommendation:
    return EvalRecommendation(id=rid, workspace_id="default", run_id="r", kind="system_prompt",
                              recommendation_id=rec_id, name=rid, status=status)


def _policy(pid: str, status: str) -> PolicyChange:
    return PolicyChange(id=pid, workspace_id="default", gateway_id="gw", gateway_arn="arn:gw",
                        gateway_name="gw", operation="update", operator="alice", status=status)


def _canary(cid: str, action: str | None) -> RuntimeCanary:
    return RuntimeCanary(id=cid, workspace_id="default", name=cid, champion_agent_id="a1",
                         champion_agent_name="a", challenger_agent_id="a2",
                         challenger_agent_name="b", running_action=action)


def _seed_every_kind() -> dict[str, str]:
    """One in-flight row per kind (id → expected outcome) + a terminal twin each."""
    _add(_conversation("conv-idle"))  # owns the asset operations; holds no claim
    _add(
        Job(id="job-deploy", workspace_id="default", type="deploy_agent", status="running"),
        Job(id="job-boot", workspace_id="default", type="bootstrap_workspace", status="queued"),
        Job(id="job-uninst", workspace_id="default", type="uninstall_system_agent",
            status="running"),
        Job(id="job-purge", workspace_id="default", type="purge_assistant_conversation",
            status="queued"),
        Job(id="job-unknown", workspace_id="default", type="delete_agent", status="queued"),
        Job(id="job-done", workspace_id="default", type="deploy_agent", status="succeeded"),
        Job(id="job-unknown-done", workspace_id="default", type="delete_agent", status="failed"),
        _asset_op("op-run", "running"),
        _asset_op("op-done", "succeeded"),
        _run("run-batch", "evaluating", batch="batch-1"),
        _run("run-nobatch", "evaluating"),
        _run("run-invoking", "invoking"),
        _run("run-done", "completed", batch="batch-2"),
        Experiment(id="exp-act", workspace_id="default", name="e", agent_id="a1",
                   agent_name="a", running_action="promote"),
        Experiment(id="exp-idle", workspace_id="default", name="e2", agent_id="a1",
                   agent_name="a"),
        _canary("can-act", "advance"),
        _canary("can-idle", None),
        SkillLabJob(id="sl-train", workspace_id="default", type="train", taskset_id="t",
                    status="running"),
        SkillLabJob(id="sl-eval", workspace_id="default", type="eval", taskset_id="t",
                    status="queued"),
        SkillLabJob(id="sl-done", workspace_id="default", type="eval", taskset_id="t",
                    status="interrupted"),
        _policy("pc-run", "running"),
        _policy("pc-pend", "pending"),
        _policy("pc-done", "succeeded"),
        EvalPipeline(id="pipe-run", workspace_id="default", name="p", status="running"),
        EvalPipeline(id="pipe-idle", workspace_id="default", name="p2", status="succeeded"),
        _rec("rec-provider", f"{PROVIDER_JOB_PREFIX}abc", "PENDING"),
        _rec("rec-agentcore", "rec-123", "IN_PROGRESS"),
        _rec("rec-done", "rec-456", "COMPLETED"),
        _rec("rec-provider-done", f"{PROVIDER_JOB_PREFIX}def", "FAILED"),
        _conversation("conv-turn", active_turn=3,
                      active_turn_started_at=NOW - timedelta(minutes=5)),
        _conversation("conv-prep", preparation_token="tok"),
    )
    return {
        "job-deploy": RESUMES, "job-boot": RESUMES, "job-uninst": RESUMES,
        "job-purge": RESUMES, "job-unknown": UNHANDLED, "op-run": RESUMES,
        "run-batch": RECONCILES, "run-nobatch": FAILS_INTERRUPTED,
        "run-invoking": FAILS_INTERRUPTED, "exp-act": CLEARED_RETRY,
        "can-act": CLEARED_RETRY, "sl-train": FAILS_INTERRUPTED,
        "sl-eval": FAILS_INTERRUPTED, "pc-run": RECONCILES, "pc-pend": RECONCILES,
        "pipe-run": FAILS_ON_READ, "rec-provider": FAILS_ON_READ,
        "rec-agentcore": RESTART_SAFE, "conv-turn": FAILS_INTERRUPTED,
        "conv-prep": CLEARED_RETRY,
    }


def _collect(**kwargs):
    db = SessionLocal()
    try:
        return collect_inflight(db, now=NOW, **kwargs)
    finally:
        db.close()


# --- the inventory ---------------------------------------------------------------


def test_every_kind_lists_once_with_its_outcome_and_terminal_rows_are_absent():
    expected = _seed_every_kind()
    items = _collect()
    got = {i.id: i.restart_outcome for i in items}
    assert len(items) == len(got), "a row was listed twice"
    assert got == expected
    by_id = {i.id: i for i in items}
    assert by_id["job-unknown"].handled_by is None
    assert by_id["conv-turn"].age_seconds == pytest.approx(300)
    assert by_id["conv-turn"].status == "turn 3"
    assert "resumable on request" in by_id["sl-train"].label
    assert by_id["run-batch"].handled_by == "resume_interrupted_runs"
    assert by_id["pipe-run"].handled_by == inflight.READ_TIME_REAPERS[inflight.EVAL_PIPELINE]
    assert {i.workspace_id for i in items} == {"default"}


def test_every_outcome_is_in_the_closed_set():
    _seed_every_kind()
    assert {i.restart_outcome for i in _collect()} <= set(inflight.OUTCOMES)


def test_one_conversation_holding_both_claims_is_two_items():
    _add(_conversation("conv-both", active_turn=1, preparation_token="t"))
    assert sorted((i.kind, i.restart_outcome) for i in _collect()) == [
        (inflight.ASSISTANT_PREPARATION, CLEARED_RETRY),
        (inflight.ASSISTANT_TURN, FAILS_INTERRUPTED),
    ]


def test_a_ledger_behind_the_models_is_skipped_not_an_error():
    """After `git merge` and before the restart migrates, a new table/column is absent."""
    _add(Job(id="job-q", workspace_id="default", type="deploy_agent", status="queued"))
    with engine.begin() as conn:
        conn.exec_driver_sql("ALTER TABLE eval_pipelines RENAME TO eval_pipelines_old")
        conn.exec_driver_sql("ALTER TABLE experiments RENAME COLUMN running_action TO ra_old")
    try:
        skipped: list[str] = []
        items = _collect(skipped=skipped)
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE eval_pipelines_old RENAME TO eval_pipelines")
            conn.exec_driver_sql("ALTER TABLE experiments RENAME COLUMN ra_old TO running_action")
    assert [i.id for i in items] == ["job-q"]
    assert any(s.startswith("eval_pipelines: table") for s in skipped)
    assert any(s.startswith("experiments: column") and "running_action" in s for s in skipped)


@pytest.mark.parametrize(("outcomes", "code"), [
    ((), 0),
    ((RESTART_SAFE,), 0),
    ((RESUMES, RECONCILES, RESTART_SAFE), 2),
    ((RESUMES, FAILS_ON_READ), 3),
    ((CLEARED_RETRY,), 3),
    ((UNHANDLED,), 3),
    ((FAILS_INTERRUPTED,), 3),
])
def test_exit_code_rule(outcomes, code):
    items = [inflight.InflightItem("k", str(n), None, "s", "l", None, None, o, None)
             for n, o in enumerate(outcomes)]
    assert inflight.exit_code(items) == code


# --- the CLI ---------------------------------------------------------------------


def _ledger_fingerprint() -> dict[str, tuple[int, str]]:
    out: dict[str, tuple[int, str]] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            rows = conn.execute(select(table).order_by(*table.primary_key.columns)).all()
            out[table.name] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest())
    return out


@pytest.fixture
def no_aws(monkeypatch):
    """Every boto3/botocore client goes through Session.create_client."""
    def refuse(*_a, **_k):
        raise AssertionError("the in-flight probe must not construct an AWS client")

    monkeypatch.setattr(botocore.session.Session, "create_client", refuse)


def test_cli_empty_ledger_exits_0(cli, no_aws, capsys):
    assert cli.main([]) == 0
    assert "nothing in flight" in capsys.readouterr().out


def test_cli_only_resumable_exits_2(cli, no_aws, capsys):
    _add(Job(id="job-q", workspace_id="default", type="deploy_agent", status="queued"),
         _policy("pc-run", "running"),
         _rec("rec-agentcore", "rec-1", "PENDING"))
    assert cli.main([]) == 2
    out = capsys.readouterr().out
    assert "job-q" in out and "pc-run" in out and "exit 2" in out


@pytest.mark.parametrize("row", [
    lambda: Job(id="x", workspace_id="default", type="delete_agent", status="queued"),
    lambda: _canary("x", "advance"),
    lambda: EvalPipeline(id="x", workspace_id="default", name="p", status="running"),
])
def test_cli_anything_disruptive_exits_3(cli, no_aws, row, capsys):
    _add(Job(id="job-q", workspace_id="default", type="deploy_agent", status="queued"), row())
    assert cli.main([]) == 3


def test_cli_json_is_parseable_and_carries_kind_id_outcome_age(cli, no_aws, capsys):
    expected = _seed_every_kind()
    assert cli.main(["--json"]) == 3
    doc = json.loads(capsys.readouterr().out)
    assert doc["exit_code"] == 3
    assert {i["id"]: i["restart_outcome"] for i in doc["items"]} == expected
    for item in doc["items"]:
        assert {"kind", "id", "restart_outcome", "age_seconds", "since",
                "handled_by", "workspace_id"} <= set(item)
    assert sum(doc["counts"].values()) == len(expected)
    # disruptive outcomes sort first
    benign = [i["restart_outcome"] in inflight.BENIGN_OUTCOMES for i in doc["items"]]
    assert benign == sorted(benign)


def test_cli_leaves_every_table_byte_identical(cli, no_aws, capsys):
    _seed_every_kind()
    before = _ledger_fingerprint()
    cli.main([])
    cli.main(["--json"])
    assert _ledger_fingerprint() == before


def test_cli_connection_refuses_writes(cli):
    url = engine.url.render_as_string(hide_password=False)
    probe = cli._read_only_engine(url)
    try:
        with probe.connect() as conn, pytest.raises(Exception, match="readonly"):
            conn.exec_driver_sql("DELETE FROM jobs")
    finally:
        probe.dispose()


def test_cli_refuses_a_missing_ledger_file_instead_of_creating_it(cli, tmp_path):
    missing = tmp_path / "absent.db"
    with pytest.raises(SystemExit) as exc:
        cli._read_only_engine(f"sqlite:///{missing}")
    assert exc.value.code == 1
    assert not missing.exists()


def test_cli_usage_error_does_not_masquerade_as_exit_2(cli):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--nope"])
    assert exc.value.code == 64


def test_cli_honors_launchpad_database_url(no_aws, monkeypatch, tmp_path, capsys):
    """The probe reads whichever ledger settings name — e.g. a copy of prod's."""
    copy = tmp_path / "copy.db"
    other = create_engine(f"sqlite:///{copy}")
    Base.metadata.create_all(other)
    with other.begin() as conn:
        conn.execute(Job.__table__.insert().values(
            id="job-copy", workspace_id="default", type="deploy_agent", status="queued",
            payload={}, log="", created_at=NOW, updated_at=NOW))
    other.dispose()
    monkeypatch.setenv("LAUNCHPAD_DATABASE_URL", f"sqlite:///{copy}")
    config_mod.get_settings.cache_clear()
    try:
        assert _load_cli().main(["--json"]) == 2
    finally:
        monkeypatch.undo()
        config_mod.get_settings.cache_clear()
    doc = json.loads(capsys.readouterr().out)
    assert [i["id"] for i in doc["items"]] == ["job-copy"]
    assert doc["database"].endswith("copy.db")


def test_cli_does_not_build_the_app():
    """No create_app/init_db: a probe must not migrate or seed the ledger."""
    tree = ast.parse((BACKEND / "scripts" / "inflight.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                for a in n.names} | {n.module for n in ast.walk(tree)
                                     if isinstance(n, ast.ImportFrom)}
    assert not {"create_app", "init_db", "app.main", "aws_clients"} & (names | imported)


# --- drift guard -----------------------------------------------------------------


def startup_hook_calls(source: str) -> set[str]:
    """Statement-level calls in the `if resume_jobs:` block of `create_app`."""
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "create_app")
    block = next(n for n in ast.walk(fn) if isinstance(n, ast.If)
                 and isinstance(n.test, ast.Name) and n.test.id == "resume_jobs")
    calls: set[str] = set()
    for stmt in block.body:
        value = getattr(stmt, "value", None)
        if isinstance(stmt, ast.Expr | ast.Assign | ast.AnnAssign) and isinstance(value, ast.Call):
            calls.add(ast.unparse(value.func))
    return calls


def starter_job_types(source: str) -> set[str]:
    """The resolved keys of the `starters` dict in `resume_pending_jobs`."""
    fn = next(n for n in ast.walk(ast.parse(source))
              if isinstance(n, ast.FunctionDef) and n.name == "resume_pending_jobs")
    starters = next(n.value for n in ast.walk(fn)
                    if isinstance(n, ast.AnnAssign | ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "starters"
                            for t in getattr(n, "targets", [getattr(n, "target", None)])))
    pipeline = importlib.import_module("app.deployer.pipeline")
    # the function imports some modules lazily; resolve those aliases too
    aliases = {a.asname or a.name: f"{n.module}.{a.name}" for n in ast.walk(fn)
               if isinstance(n, ast.ImportFrom) for a in n.names}
    keys: set[str] = set()
    for key in starters.keys:
        if isinstance(key, ast.Constant):
            keys.add(key.value)
            continue
        assert isinstance(key, ast.Attribute) and isinstance(key.value, ast.Name), (
            f"unrecognised starters key {ast.unparse(key)} — teach this guard to read it")
        owner = (importlib.import_module(aliases[key.value.id]) if key.value.id in aliases
                 else getattr(pipeline, key.value.id))
        keys.add(getattr(owner, key.attr))
    return keys


def _uncovered_hooks(source: str) -> set[str]:
    return startup_hook_calls(source) - set(inflight.STARTUP_HOOKS)


def test_every_startup_hook_is_known_to_the_inventory():
    hooks = startup_hook_calls(MAIN_PY.read_text(encoding="utf-8"))
    assert len(hooks) >= 9  # the parser still finds the block
    assert hooks == set(inflight.STARTUP_HOOKS), (
        "create_app's `if resume_jobs:` block and inflight.STARTUP_HOOKS disagree — "
        "classify the new hook's rows in app/services/inflight.py")


def test_every_hook_kind_is_collected():
    """A hook mapped to kinds must name kinds collect_inflight can emit."""
    expected = _seed_every_kind()
    emitted = {i.kind for i in _collect()}
    assert len(expected) == len(_collect())
    for hook, kinds in inflight.STARTUP_HOOKS.items():
        if isinstance(kinds, tuple):
            assert set(kinds) <= emitted, hook


def test_every_resume_starter_is_resumable_in_the_inventory():
    types = starter_job_types(PIPELINE_PY.read_text(encoding="utf-8"))
    assert "deploy_agent" in types and len(types) >= 4
    assert types == inflight.resumable_job_types(), (
        "resume_pending_jobs' starters and inflight.resumable_job_types() disagree")


def test_drift_guard_catches_a_new_hook():
    source = MAIN_PY.read_text(encoding="utf-8").replace(
        "        sweep_skill_lab_jobs()\n",
        "        sweep_skill_lab_jobs()\n        resume_new_subsystem()\n"
        "        drained = drain_queue()\n",
    )
    assert _uncovered_hooks(source) == {"resume_new_subsystem", "drain_queue"}


def test_drift_guard_catches_a_new_starter():
    source = PIPELINE_PY.read_text(encoding="utf-8").replace(
        '        "deploy_agent": start_deploy_async,\n',
        '        "deploy_agent": start_deploy_async,\n        "rotate_keys": start_deploy_async,\n',
    )
    assert starter_job_types(source) - inflight.resumable_job_types() == {"rotate_keys"}
