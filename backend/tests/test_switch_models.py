"""scripts/switch_models.py — model probing / selection / env-file writing, no network."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import stat
import sys
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "switch_models.py"
_spec = importlib.util.spec_from_file_location("switch_models", SCRIPT)
sm = importlib.util.module_from_spec(_spec)
sys.modules["switch_models"] = sm
_spec.loader.exec_module(sm)

REGION = "us-east-1"
ACCOUNT = "111122223333"
ALL_MODELS = sorted({m for r in sm.ROLES for m in r.candidates} | {"us.openai.gpt-5.6-sol"})


def _denied(op: str, code: str = "AccessDeniedException", msg: str = "no access"):
    return ClientError({"Error": {"Code": code, "Message": msg}}, op)


class FakeRuntime:
    def __init__(self, broken: set[str]):
        self.broken, self.calls = broken, []

    def converse(self, **kw):
        self.calls.append((kw["modelId"], "toolConfig" in kw, kw["inferenceConfig"]["maxTokens"]))
        if kw["modelId"] in self.broken:
            raise _denied("Converse")
        return {"output": {"message": {"content": [{"text": "OK"}]}}, "stopReason": "end_turn"}


class FakeBedrock:
    def __init__(self, offered: list[str]):
        self.offered = offered

    def list_inference_profiles(self, **kw):
        profiles = [m for m in self.offered if sm._PREFIX.match(m)]
        half = len(profiles) // 2
        if "nextToken" not in kw:  # two pages, to exercise pagination
            return {"inferenceProfileSummaries": [{"inferenceProfileId": m}
                                                  for m in profiles[:half]], "nextToken": "p2"}
        return {"inferenceProfileSummaries": [{"inferenceProfileId": m} for m in profiles[half:]]}

    def list_foundation_models(self, **kw):
        assert kw == {"byInferenceType": "ON_DEMAND"}
        return {"modelSummaries": [{"modelId": m} for m in self.offered
                                   if not sm._PREFIX.match(m)]}


class FakeControl:
    def __init__(self, refused: set[str], evaluators=None, timeout_on: set[str] = frozenset(),
                 delete_fails: bool = False):
        self.refused, self.timeout_on, self.delete_fails = refused, timeout_on, delete_fails
        self.live: dict[str, str] = {}  # probe evaluator id -> name
        self.created, self.deleted, self.updates = [], [], []
        self.evaluators = evaluators or {}

    def create_evaluator(self, **kw):
        model = kw["evaluatorConfig"]["llmAsAJudge"]["modelConfig"][
            "bedrockEvaluatorModelConfig"]["modelId"]
        assert sm.re.match(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$", kw["evaluatorName"])
        self.created.append(model)
        if model in self.refused:
            raise _denied("CreateEvaluator", "ValidationException",
                          f"Bedrock inference configuration validation failed for model {model}")
        eid = f"{kw['evaluatorName']}-id"
        self.live[eid] = kw["evaluatorName"]
        if model in self.timeout_on:  # landed, but the response was lost
            raise ReadTimeoutError(endpoint_url="https://x")
        return {"evaluatorId": eid}

    def list_evaluators(self, **kw):
        return {"evaluators": [{"evaluatorId": i, "evaluatorName": n}
                               for i, n in self.live.items()]}

    def delete_evaluator(self, evaluatorId):
        if self.delete_fails:
            raise _denied("DeleteEvaluator")
        self.deleted.append(evaluatorId)
        self.live.pop(evaluatorId, None)

    def get_evaluator(self, evaluatorId):
        return json.loads(json.dumps(self.evaluators[evaluatorId]))

    def update_evaluator(self, **kw):
        self.updates.append(kw)
        return {"evaluatorId": kw["evaluatorId"]}


class FakeSTS:
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


class FakeSession:
    def __init__(self, broken=(), refused=(), offered=None, **control_kw):
        self.runtime = FakeRuntime(set(broken))
        self.bedrock = FakeBedrock(ALL_MODELS if offered is None else offered)
        self.control = FakeControl(set(refused), **control_kw)

    def client(self, name, **kw):
        return {"bedrock-runtime": self.runtime, "bedrock": self.bedrock,
                "bedrock-agentcore-control": self.control, "sts": FakeSTS()}[name]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in [r.env for r in sm.ROLES] + [sm.PROMPT_OPT_MODELS_ENV, "LAUNCHPAD_REGION"]:
        monkeypatch.delenv(key, raising=False)


def run(tmp_path, session, *args, env_file=None, db=None):
    env_file = env_file or tmp_path / "models.env"
    argv = ["--region", REGION, "--env-file", str(env_file), "--json",
            "--database-url", db or f"sqlite:///{tmp_path / 'missing.db'}", *args]
    return sm.main(argv, session=session), env_file


def result_of(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out[out.index("\n{") + 1:])


def role(result, name):
    return next(r for r in result["roles"] if r["role"] == name)


def test_order_default_then_prefer_then_alternatives(tmp_path, capsys):
    session = FakeSession(broken={sm.SONNET55, "global.zai.glm-5.3", sm.SONNET5})
    code, _ = run(tmp_path, session, "--roles", "platform", "--dry-run",
                  "--prefer", "global.zai.glm-5.3")
    probed = [m for m, _, _ in session.runtime.calls]
    assert probed == [sm.SONNET55, "global.zai.glm-5.3", sm.SONNET5, sm.HAIKU55]
    r = role(result_of(capsys), "platform")
    assert code == 0 and r["chosen"] == sm.HAIKU55
    assert "AccessDeniedException" in r["why"] and "first working alternative" in r["why"]


def test_prefer_skipped_when_family_does_not_fit(tmp_path, capsys):
    claude_sdk = next(r for r in sm.ROLES if r.name == "claude_sdk")
    listed = sm.candidates(claude_sdk, sm.SONNET55, ["global.zai.glm-5.3", sm.OPUS55])
    assert listed[:2] == [sm.SONNET55, sm.OPUS55] and "global.zai.glm-5.3" not in listed
    session = FakeSession(broken={sm.SONNET55})
    run(tmp_path, session, "--roles", "claude_sdk", "--dry-run", "--prefer", "global.zai.glm-5.3")
    assert all(sm.family(m) == "anthropic" for m, _, _ in session.runtime.calls)
    assert role(result_of(capsys), "claude_sdk")["chosen"] == sm.SONNET5


def test_default_access_denied_falls_back(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT6_LUNA})
    code, env = run(tmp_path, session, "--roles", "actor")
    r = role(result_of(capsys), "actor")
    assert code == 0 and r["chosen"] == sm.HAIKU55
    # the actor probe forces tool choice with a bigger budget
    assert all(tools and budget == 256 for _, tools, budget in session.runtime.calls)
    assert f"LAUNCHPAD_ACTOR_MODEL_ID={sm.HAIKU55}" in env.read_text().splitlines()


def test_region_filtering(tmp_path, capsys):
    offered = [m for m in ALL_MODELS if m != sm.SONNET55]
    session = FakeSession(offered=offered)
    run(tmp_path, session, "--roles", "platform", "--dry-run")
    r = role(result_of(capsys), "platform")
    assert sm.SONNET55 not in [m for m, _, _ in session.runtime.calls]
    assert r["tried"][0] == {"model": sm.SONNET55, "result": f"not offered in {REGION}"}
    assert r["chosen"] == sm.SONNET5


def test_judge_needs_converse_and_evaluator_probe(tmp_path, capsys):
    session = FakeSession(refused={sm.GPT61_SOL})
    run(tmp_path, session, "--roles", "judge", "--dry-run")
    r = role(result_of(capsys), "judge")
    assert r["chosen"] == sm.SONNET55
    assert session.control.created == [sm.GPT61_SOL, sm.SONNET55]
    assert [m for m, _, _ in session.runtime.calls] == [sm.GPT61_SOL, sm.SONNET55]
    assert "ValidationException" in r["why"]
    assert session.control.live == {}  # every successful probe evaluator deleted


def test_judge_converse_failure_skips_evaluator_probe(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT61_SOL})
    run(tmp_path, session, "--roles", "judge", "--dry-run")
    assert session.control.created == [sm.SONNET55]


def test_probe_evaluator_deleted_even_when_create_response_is_lost(tmp_path, capsys):
    session = FakeSession(timeout_on={sm.GPT61_SOL})
    run(tmp_path, session, "--roles", "judge", "--dry-run")
    r = role(result_of(capsys), "judge")
    assert r["chosen"] == sm.SONNET55 and "ReadTimeoutError" in r["why"]
    assert session.control.live == {} and len(session.control.deleted) == 2


def test_probe_evaluator_delete_failure_is_reported(tmp_path, capsys):
    session = FakeSession(delete_fails=True)
    run(tmp_path, session, "--roles", "judge", "--dry-run")
    out = capsys.readouterr().out
    assert "not deleted" in out and "delete it by hand" in out
    assert json.loads(out[out.index("\n{") + 1:])["leaked_probe_evaluators"] == list(
        session.control.live)


def test_judge_fallback_keeps_current_or_is_empty(tmp_path, capsys):
    run(tmp_path, FakeSession(), "--roles", "judge,judge_fallback", "--dry-run")
    assert role(result_of(capsys), "judge_fallback")["chosen"] == sm.SONNET55

    nothing_else = {m for m in ALL_MODELS if m != sm.GPT61_SOL}
    code, env = run(tmp_path, FakeSession(broken=nothing_else), "--roles",
                    "judge,judge_fallback")
    r = role(result_of(capsys), "judge_fallback")
    assert code == 0 and r["chosen"] == "" and "no fallback" in r["why"]
    assert "LAUNCHPAD_JUDGE_FALLBACK_MODEL_ID=" in env.read_text().splitlines()


def test_judge_fallback_differs_from_chosen_judge(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT61_SOL})
    run(tmp_path, session, "--roles", "judge,judge_fallback", "--dry-run")
    result = result_of(capsys)
    assert role(result, "judge")["chosen"] == sm.SONNET55
    assert role(result, "judge_fallback")["chosen"] == sm.SONNET5


def test_claude_only_role_unavailable_is_not_written(tmp_path, capsys):
    claude = {m for m in ALL_MODELS if sm.family(m) == "anthropic"}
    code, env = run(tmp_path, FakeSession(broken=claude), "--roles", "claude_sdk,platform")
    result = result_of(capsys)
    assert code == 0  # restricted roles do not fail the run
    assert role(result, "claude_sdk")["chosen"] is None
    text = env.read_text()
    assert "LAUNCHPAD_CLAUDE_SDK_MODEL_ID" not in text and "LAUNCHPAD_PLATFORM_MODEL_ID=" in text


def test_unrestricted_role_unavailable_exits_2(tmp_path, capsys):
    code, _ = run(tmp_path, FakeSession(broken=set(ALL_MODELS)), "--roles", "platform",
                  "--dry-run")
    assert code == 2
    assert "UNAVAILABLE" in capsys.readouterr().out


def test_dry_run_writes_nothing(tmp_path, capsys):
    env = tmp_path / "models.env"
    code, _ = run(tmp_path, FakeSession(broken={sm.SONNET5}), "--dry-run", "--restart",
                  env_file=env)
    assert code == 0 and not env.exists()
    assert result_of(capsys)["restart"] is None
    env.write_text("KEEP=1\n")
    run(tmp_path, FakeSession(), "--dry-run", env_file=env)
    assert env.read_text() == "KEEP=1\n"


def test_env_file_content_and_mode(tmp_path, capsys):
    env = tmp_path / "models.env"
    env.write_text("# operator note\nOTHER=keep me\nLAUNCHPAD_ACTOR_MODEL_ID=global.zai.glm-5.3\n")
    env.chmod(0o644)
    code, _ = run(tmp_path, FakeSession(broken={sm.SONNET55}), env_file=env)
    assert code == 0
    lines = env.read_text().splitlines()
    assert lines[:2] == ["# operator note", "OTHER=keep me"]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    values = dict(line.split("=", 1) for line in lines if "=" in line and line[0] != "#")
    # an already-working override stays first -> idempotent
    assert values["LAUNCHPAD_ACTOR_MODEL_ID"] == "global.zai.glm-5.3"
    assert values["LAUNCHPAD_PLATFORM_MODEL_ID"] == sm.SONNET5
    assert values["LAUNCHPAD_JUDGE_MODEL_ID"] == sm.GPT61_SOL
    raw = values[sm.PROMPT_OPT_MODELS_ENV]
    assert " " not in raw and json.loads(raw)[0] == values["LAUNCHPAD_PROMPT_OPT_DEFAULT_MODEL_ID"]
    assert sm.SONNET55 not in json.loads(raw)
    assert sum(line.startswith(sm.HEADER) for line in lines) == 2

    # re-run: same keys replaced, not duplicated; header refreshed
    run(tmp_path, FakeSession(broken={sm.SONNET55}), env_file=env)
    lines = env.read_text().splitlines()
    assert sum(line.startswith("LAUNCHPAD_ACTOR_MODEL_ID=") for line in lines) == 1
    assert sum(line.startswith(sm.HEADER) for line in lines) == 2


def test_simulate_unavailable_never_calls_those_families(tmp_path, capsys):
    session = FakeSession()
    code, _ = run(tmp_path, session, "--dry-run", "--simulate-unavailable", "openai,anthropic")
    result = result_of(capsys)
    called = {sm.family(m) for m, _, _ in session.runtime.calls}
    assert not called & {"openai", "anthropic"}
    assert role(result, "judge")["chosen"] == "global.zai.glm-5.3"
    # the fallback is another model, not GLM-5.3 under its us. profile
    assert role(result, "judge_fallback")["chosen"] == "deepseek.v3.2"
    listed = json.loads(result["values"][sm.PROMPT_OPT_MODELS_ENV])
    assert len({sm.base(m) for m in listed}) == len(listed)
    assert role(result, "skill_lab_judge")["chosen"] is None and code == 0


def _ledger(tmp_path, overrides=()) -> str:
    db = tmp_path / "ledger.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE platform_settings (key TEXT PRIMARY KEY, value TEXT,"
                     " updated_by TEXT, updated_at TEXT)")
        conn.executemany("INSERT INTO platform_settings VALUES (?,?,'admin','now')", overrides)
        conn.execute("CREATE TABLE workspaces (id TEXT, account_id TEXT, region TEXT)")
        conn.execute("CREATE TABLE resource_creators (id TEXT, workspace_id TEXT, kind TEXT,"
                     " resource_id TEXT)")
        conn.executemany("INSERT INTO workspaces VALUES (?,?,?)", [
            ("default", ACCOUNT, REGION), ("other", "999999999999", REGION)])
        conn.executemany("INSERT INTO resource_creators VALUES (?,?,?,?)", [
            ("1", "default", "evaluator", "judge_stale"),
            ("2", "default", "evaluator", "derived_stale"),
            ("3", "default", "evaluator", "judge_fine"),
            ("4", "default", "knowledge_base", "kb1"),
            ("5", "other", "evaluator", "foreign"),
        ])
    return f"sqlite:///{db}"


def _evaluators():
    def model(m, **extra):
        return {"bedrockEvaluatorModelConfig": {"modelId": m, **extra}}

    scale = {"numerical": [{"value": 1.0, "label": "Good", "definition": "good"}]}
    return {
        "judge_stale": {"evaluatorName": "js", "description": "d1", "level": "SESSION",
                        "evaluatorConfig": {"llmAsAJudge": {
                            "instructions": "Rate {context}", "ratingScale": scale,
                            "modelConfig": model(sm.GPT61_SOL,
                                                 inferenceConfig={"maxTokens": 9})}}},
        "derived_stale": {"evaluatorName": "ds", "description": "d2", "level": "TRACE",
                          "evaluatorConfig": {"derived": {
                              "baseEvaluatorId": "Builtin.Helpfulness",
                              "modelConfig": model(sm.GPT61_SOL)}}},
        "judge_fine": {"evaluatorName": "jf", "level": "TRACE", "evaluatorConfig": {
            "llmAsAJudge": {"instructions": "x {context}", "ratingScale": scale,
                            "modelConfig": model(sm.SONNET5)}}},
    }


def test_evaluators_listed_read_only_without_flag(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT61_SOL}, evaluators=_evaluators())
    run(tmp_path, session, "--roles", "judge", db=_ledger(tmp_path))
    rows = {r["id"]: r for r in result_of(capsys)["evaluators"]}
    assert set(rows) == {"judge_stale", "derived_stale", "judge_fine"}
    assert rows["judge_stale"]["status"].startswith("STALE; --update-evaluators would move")
    assert rows["judge_fine"]["status"] == "judge model works"
    assert session.control.updates == []


def test_update_evaluators_full_replace(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT61_SOL}, evaluators=_evaluators())
    code, _ = run(tmp_path, session, "--roles", "judge", "--update-evaluators",
                  db=_ledger(tmp_path))
    rows = {r["id"]: r for r in result_of(capsys)["evaluators"]}
    assert code == 0
    assert rows["judge_stale"]["before"] == sm.GPT61_SOL
    assert rows["judge_stale"]["after"] == sm.SONNET55
    assert rows["judge_stale"]["status"] == "updated"
    assert rows["judge_fine"]["after"] == sm.SONNET5
    updates = {u["evaluatorId"]: u for u in session.control.updates}
    assert set(updates) == {"judge_stale", "derived_stale"}
    js = updates["judge_stale"]
    assert js["level"] == "SESSION" and js["description"] == "d1"
    judge = js["evaluatorConfig"]["llmAsAJudge"]
    assert judge["instructions"] == "Rate {context}" and judge["ratingScale"]["numerical"]
    assert judge["modelConfig"]["bedrockEvaluatorModelConfig"] == {
        "modelId": sm.SONNET55, "inferenceConfig": {"maxTokens": 9}}
    derived = updates["derived_stale"]["evaluatorConfig"]["derived"]
    assert derived["baseEvaluatorId"] == "Builtin.Helpfulness"


def test_update_evaluators_dry_run_does_not_update(tmp_path, capsys):
    session = FakeSession(broken={sm.GPT61_SOL}, evaluators=_evaluators())
    run(tmp_path, session, "--roles", "judge", "--update-evaluators", "--dry-run",
        db=_ledger(tmp_path))
    assert session.control.updates == []


def test_unknown_role_and_missing_credentials_exit_1(tmp_path):
    assert sm.main(["--roles", "nope", "--dry-run"], session=FakeSession()) == 1
    assert sm.main(["--bogus"], session=FakeSession()) == 1

    class NoCreds(FakeSession):
        def client(self, name, **kw):
            if name == "sts":
                raise _denied("GetCallerIdentity", "ExpiredToken", "expired")
            return super().client(name, **kw)

    assert run(tmp_path, NoCreds(), "--dry-run")[0] == 1


def test_unwritable_env_file_suggests_sudo(tmp_path, capsys):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        code, _ = run(tmp_path, FakeSession(), "--roles", "platform",
                      env_file=locked / "models.env")
    finally:
        locked.chmod(0o700)
    assert code == 1 and "sudo" in capsys.readouterr().err


def test_console_override_is_reported_not_modified(tmp_path, capsys):
    url = _ledger(tmp_path, [("judge_model_id", sm.SONNET5), ("actor_model_id", "")])
    code, env = run(tmp_path, FakeSession(broken={sm.GPT61_SOL}, evaluators=_evaluators()),
                    "--roles", "judge,actor", db=url)
    out = capsys.readouterr().out
    result = json.loads(out[out.index("\n{") + 1:])
    assert code == 0
    assert role(result, "judge")["console_override"] == sm.SONNET5
    assert role(result, "actor")["console_override"] is None  # empty value = no override
    assert f"judge: console={sm.SONNET5} (ok)" in out and "模型设置" in out
    # the env file still records the probed choice; the ledger row is untouched
    assert f"LAUNCHPAD_JUDGE_MODEL_ID={sm.SONNET55}" in env.read_text().splitlines()
    with sqlite3.connect(url[len("sqlite:///"):]) as conn:
        assert conn.execute("SELECT value FROM platform_settings WHERE key='judge_model_id'"
                            ).fetchone() == (sm.SONNET5,)


def test_missing_console_table_is_skipped(tmp_path, capsys):
    db = tmp_path / "empty.db"
    sqlite3.connect(db).close()
    code, _ = run(tmp_path, FakeSession(), "--roles", "agent_default", "--dry-run",
                  db=f"sqlite:///{db}")
    out = capsys.readouterr().out
    assert code == 0 and "console overrides not checked" in out
    assert "Ledger evaluators: skipped" in out


def test_env_keys_are_settings_field_names():
    for r in sm.ROLES:
        assert r.env == "LAUNCHPAD_" + r.field.upper()
    agent = next(r for r in sm.ROLES if r.name == "agent_default")
    assert agent.env == "LAUNCHPAD_AGENT_DEFAULT_MODEL_ID"
    assert agent.default == "global.moonshotai.kimi-k3"


TOOL_ROLES = {"agent_default", "assistant", "architect", "studio_fallback", "actor"}


def test_deepseek_and_kimi_in_every_unrestricted_role():
    for r in sm.ROLES:
        if r.restricted:
            # a family-bound role offers only its own families (the codex
            # target's Kimi K3 is one of them)
            assert {sm.family(m) for m in r.candidates} <= r.families, r.name
            continue
        assert set(sm.DS_KIMI) <= set(r.candidates), r.name
        assert (sm.R1 in r.candidates) == (r.name not in TOOL_ROLES), r.name
        assert (r.kind in sm.TOOL_KINDS) == (r.name in TOOL_ROLES), r.name


def test_tool_roles_probe_with_tool_config(tmp_path, capsys):
    session = FakeSession()
    run(tmp_path, session, "--roles", "assistant", "--dry-run")
    assert session.runtime.calls == [(sm.GPT61_SOL, True, 64)]


def test_r1_never_probed_or_chosen_for_tool_roles(tmp_path, capsys):
    only_r1 = {m for m in ALL_MODELS if m != sm.R1}
    session = FakeSession(broken=only_r1)
    code, env = run(tmp_path, session, "--prefer", sm.R1)
    result = result_of(capsys)
    tool_probes = [m for m, tools, _ in session.runtime.calls if tools]
    assert sm.R1 not in tool_probes
    for name in TOOL_ROLES:
        assert role(result, name)["chosen"] is None
        assert sm.R1 not in [t["model"] for t in role(result, name)["tried"]]
    for name in ("platform", "prompt_opt"):
        assert role(result, name)["chosen"] == sm.R1
    assert code == 2
