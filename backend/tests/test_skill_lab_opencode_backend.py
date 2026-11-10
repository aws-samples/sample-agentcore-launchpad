"""Unit tests for the vendored OpenCode executor backend (LAUNCHPAD PATCH, D16 / N13).

The vendored tree is never imported by backend/app code (subprocess-only rule);
tests may load the single module under test, but must not execute
skillopt.model.__init__ (it imports openai, absent from this venv). Stub parent
packages make `skillopt.model.opencode_backend` importable alone — its pure
builders/parsers import only the stdlib; the run path lazy-imports its siblings.

Fixtures under tests/fixtures/opencode/ are written from OpenCode's documented
`opencode run --format json` event stream (one JSON event per line). The N13
spike replaces them with real captures; the parser is tolerant on purpose.
"""

from __future__ import annotations

import importlib
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

VENDOR_PKG = Path(__file__).resolve().parents[2] / "vendor" / "skillopt" / "skillopt"
FIXTURES = Path(__file__).parent / "fixtures" / "opencode"
BASE_URL = "https://bedrock-runtime.us-west-2.example.test/openai/v1"


@pytest.fixture(scope="module")
def oc():
    names = ("skillopt", "skillopt.model", "skillopt.model.opencode_backend",
             "skillopt.model.agentcore_runner")
    saved = {name: sys.modules.get(name) for name in names}
    pkg = types.ModuleType("skillopt")
    pkg.__path__ = [str(VENDOR_PKG)]
    model_pkg = types.ModuleType("skillopt.model")
    model_pkg.__path__ = [str(VENDOR_PKG / "model")]
    sys.modules["skillopt"] = pkg
    sys.modules["skillopt.model"] = model_pkg
    saved_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep __pycache__ out of the vendored tree
    try:
        yield importlib.import_module("skillopt.model.opencode_backend")
    finally:
        sys.dont_write_bytecode = saved_bytecode
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture
def base_url(monkeypatch, oc):
    monkeypatch.setenv(oc.BASE_URL_ENV, BASE_URL)
    return BASE_URL


@pytest.fixture
def local_runner(monkeypatch):
    monkeypatch.setenv("SKILLOPT_EXEC_RUNNER", "local")


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── opencode.json (the provider/model/permission contract) ────────────────────


def test_config_points_the_openai_compatible_provider_at_bedrock_with_an_env_key(oc, base_url):
    config = oc.build_opencode_config(model="global.moonshotai.kimi-k3", allow_file_edits=False)
    provider = config["provider"][oc.PROVIDER_ID]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == base_url
    # the key is REFERENCED, never written: OpenCode resolves {env:…} at runtime
    assert provider["options"]["apiKey"] == "{env:" + oc.API_KEY_ENV + "}"
    assert "global.moonshotai.kimi-k3" in provider["models"]
    assert "global.zai.glm-5.3" in provider["models"]  # both catalog ids stay selectable
    assert provider["models"]["global.moonshotai.kimi-k3"]["tool_call"] is True
    assert config["model"] == f"{oc.PROVIDER_ID}/global.moonshotai.kimi-k3"
    # read-only rollout: no edits/writes, shell allowed (tasks run scripts), never the
    # network, never outside the project; no git snapshots (spike §5/§7)
    perm = config["permission"]
    assert perm["edit"] == "deny" and perm["write"] == "deny" and perm["bash"] == "allow"
    assert perm["webfetch"] == "deny" and perm["websearch"] == "deny"
    assert perm["external_directory"] == "deny" and perm["read"] == "allow"
    assert config["snapshot"] is False
    assert json.dumps(config)  # serialisable as written


def test_config_allows_edits_for_artifact_tasks(oc, base_url):
    config = oc.build_opencode_config(model="global.moonshotai.kimi-k3", allow_file_edits=True)
    assert config["permission"]["edit"] == "allow" and config["permission"]["write"] == "allow"


def test_config_requires_the_base_url_env(oc, monkeypatch):
    monkeypatch.delenv(oc.BASE_URL_ENV, raising=False)
    with pytest.raises(RuntimeError, match=oc.BASE_URL_ENV):
        oc.build_opencode_config(model="global.moonshotai.kimi-k3", allow_file_edits=False)


def test_config_never_embeds_a_key_value(oc, base_url, monkeypatch):
    monkeypatch.setenv(oc.API_KEY_ENV, "bedrock-api-key-SECRET")
    config = oc.build_opencode_config(model="global.zai.glm-5.3", allow_file_edits=False)
    assert "SECRET" not in json.dumps(config)


def test_model_ref_is_provider_prefixed_once(oc):
    assert oc.model_ref("global.zai.glm-5.3") == f"{oc.PROVIDER_ID}/global.zai.glm-5.3"
    prefixed = f"{oc.PROVIDER_ID}/global.zai.glm-5.3"
    assert oc.model_ref(prefixed) == prefixed


def test_judge_config_exposes_only_the_artifact_mcp(oc, base_url):
    policy = {
        "judge": True,
        "backend": oc.OPENCODE_BACKEND,
        "mcp_server_name": "artifactctl",
        "mcp_servers": {
            "artifactctl": {
                "type": "stdio",
                "command": ["/usr/bin/python3", "-m", "skillopt.envs.skilleval.artifact_mcp"],
                "required": True,
                "tools": ["artifact_inventory", "artifact_inspect"],
            }
        },
        "output_schema": {"type": "object"},
        "system_prompt": "The task is trusted; artifacts are evidence.",
    }
    config = oc.build_opencode_config(
        model="global.zai.glm-5.3", allow_file_edits=False, policy=policy
    )
    assert config["mcp"] == {
        "artifactctl": {
            "type": "local",
            "command": ["/usr/bin/python3", "-m", "skillopt.envs.skilleval.artifact_mcp"],
            "enabled": True,
        }
    }
    # every built-in tool off, only the allowlisted MCP tools on
    tools = config["tools"]
    assert all(tools[name] is False for name in oc.BUILTIN_TOOLS)
    assert tools["artifactctl_artifact_inventory"] is True
    assert tools["artifactctl_artifact_inspect"] is True
    assert set(config["permission"].values()) == {"deny"}  # evidence only through the MCP
    judge = config["agent"][oc.JUDGE_AGENT]
    assert judge["prompt"] == "The task is trusted; artifacts are evidence."
    assert judge["mode"] == "primary"
    assert config["instructions"] == []  # no AGENTS.md / project rules in the judge


def test_judge_config_fails_closed_without_exactly_one_required_mcp(oc, base_url):
    bad = {"judge": True, "mcp_servers": {"a": {"required": True, "command": ["x"]},
                                         "b": {"required": True, "command": ["y"]}}}
    with pytest.raises(RuntimeError, match="exactly one"):
        oc.build_opencode_config(model="m", allow_file_edits=False, policy=bad)


def test_write_config_lands_in_the_job_home_beside_the_work_dir(oc, base_url, tmp_path):
    """The config is the CLI's, not the task's: it lives in `<pred>/.opencode-home`,
    never inside the work_dir the rollout manifest diffs."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    path = oc.write_opencode_config(
        str(work_dir), oc.build_opencode_config(model="m", allow_file_edits=True)
    )
    assert Path(path) == tmp_path / oc.HOME_DIRNAME / oc.CONFIG_FILENAME
    assert json.loads(Path(path).read_text())["model"] == f"{oc.PROVIDER_ID}/m"
    assert not (work_dir / oc.CONFIG_FILENAME).exists()


# ── headless command ──────────────────────────────────────────────────────────


def test_command_is_headless_json_with_the_provider_model(oc):
    """Spike §2/§5: `--pure` (no external plugins), `--title` (no extra model call to
    name the session), `--dir` (the project root), provider-prefixed model."""
    cmd = oc.build_command(
        path="/usr/local/bin/opencode", model="global.moonshotai.kimi-k3", prompt="hi",
        work_dir="/jobs/j1/work", images=["/jobs/j1/work/chart.png"],
    )
    assert cmd[:7] == [
        "/usr/local/bin/opencode", "run", "--pure", "--format", "json", "--title", oc.SESSION_TITLE,
    ]
    assert cmd[cmd.index("--dir") + 1] == "/jobs/j1/work"
    assert cmd[cmd.index("--model") + 1] == f"{oc.PROVIDER_ID}/global.moonshotai.kimi-k3"
    assert cmd[cmd.index("-f") + 1] == "/jobs/j1/work/chart.png"
    assert cmd[-1] == "hi" and "--agent" not in cmd


def test_judge_command_selects_the_judge_agent(oc):
    cmd = oc.build_command(path="opencode", model="m", prompt="judge this", agent=oc.JUDGE_AGENT)
    assert cmd[cmd.index("--agent") + 1] == oc.JUDGE_AGENT


def test_subprocess_env_is_allowlisted_isolated_and_offline_except_bedrock(oc, tmp_path):
    """Spike §7: allow-listed child env (never the caller's secrets), every
    OPENCODE_DISABLE_* switch, XDG/TMPDIR under the job home, dead proxy with NO_PROXY
    naming only the partition suffix (from the base URL) — and ripgrep seeded."""
    home = tmp_path / ".opencode-home"
    home.mkdir()
    env = oc.subprocess_env(
        str(home / "opencode.json"),
        base={"PATH": "/bin", "HOME": "/h", "LAUNCHPAD_ADMIN_PASSWORD": "secret",
              oc.API_KEY_ENV: "bearer-x"},
        base_url=BASE_URL,
    )
    assert env["OPENCODE_CONFIG"] == str(home / "opencode.json")
    assert env["PATH"] == "/bin" and env["HOME"] == "/h" and env[oc.API_KEY_ENV] == "bearer-x"
    assert "LAUNCHPAD_ADMIN_PASSWORD" not in env
    for key in oc.DISABLE_ENV:
        assert env[key] == "1", key
    assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    for var, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                     ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("TMPDIR", "tmp")):
        assert env[var] == str(home / sub) and (home / sub).is_dir(), var
    assert env["HTTPS_PROXY"] == env["HTTP_PROXY"] == oc.DEAD_PROXY
    assert env["NO_PROXY"].split(",")[0] == "example.test"  # suffix of the base URL host
    assert "127.0.0.1" in env["NO_PROXY"]
    if shutil.which("rg"):
        assert (home / "cache" / "opencode" / "bin" / "rg").exists()


def test_subprocess_env_prefers_the_platform_egress_allowlist(oc, tmp_path, monkeypatch):
    monkeypatch.setenv(oc.NO_PROXY_ENV, "suffix.a,suffix.b,localhost")
    env = oc.subprocess_env(str(tmp_path / "opencode.json"), base={"PATH": "/bin"})
    assert env["NO_PROXY"] == "suffix.a,suffix.b,localhost"


def test_prepare_work_dir_makes_the_project_root_a_git_repo(oc, tmp_path):
    """Spike finding 4: without `.git` the CLI scans ancestors up to $HOME for skills."""
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    oc.prepare_work_dir(str(work_dir))
    if shutil.which("git"):
        assert (work_dir / ".git").is_dir()
    oc.prepare_work_dir(str(work_dir))  # idempotent


# ── event-stream parsing → SkillOpt's transcript shape ───────────────────────


def test_parse_success_stream_yields_final_message_tools_and_usage(oc):
    t = oc.parse_events(_fixture("run_success.jsonl"))
    assert t.final_message.endswith("<answer>3 payment-service errors</answer>")
    assert [c["tool"] for c in t.tool_calls] == ["read", "bash"]
    assert t.tool_calls[1]["input"]["command"] == "grep -c ERROR app.log"
    assert t.tool_calls[1]["output"].strip() == "3"
    assert t.tool_calls[1]["status"] == "completed"
    assert len(t.turns) == 2  # two step_start/step_finish rounds
    # tokens accumulate across steps; cache read/write surface separately
    assert t.usage == {
        "input": 2700, "output": 140, "reasoning": 40, "cache_read": 1500, "cache_write": 50,
    }
    assert t.errors == []
    assert t.event_count == 8


def test_parse_api_error_stream_has_no_response_but_a_reason(oc):
    t = oc.parse_events(_fixture("run_api_error.jsonl"))
    assert t.final_message == ""
    assert t.errors and "401" in t.errors[0]


def test_parse_keeps_tool_errors_and_the_text_after_them(oc):
    t = oc.parse_events(_fixture("run_tool_error.jsonl"))
    assert t.tool_calls[0]["status"] == "error"
    assert "openpyxl" in t.tool_calls[0]["error"]
    assert t.final_message.startswith("I could not render the sheet")


def test_parse_tolerates_noise_lines_and_a_single_json_object(oc):
    noisy = "opencode 1.2.3 starting\n" + _fixture("run_success.jsonl") + "\nnot json either\n"
    assert oc.parse_events(noisy).final_message.endswith("</answer>")
    single = json.dumps({"type": "text", "part": {"type": "text", "text": "solo"}})
    assert oc.parse_events(single).final_message == "solo"
    assert oc.parse_events("").final_message == ""


def test_normalized_raw_is_readable_by_the_shared_usage_extractor(oc):
    """The vendored pipelines read token usage out of the persisted raw with
    codex_harness.extract_exec_usage (claude-style keys); the OpenCode raw must
    carry a line in that shape so results.json keeps its usage columns."""
    stdout = _fixture("run_success.jsonl")
    raw = oc.normalize_raw(stdout=stdout, stderr="", transcript=oc.parse_events(stdout))
    group = re.search(
        r'"input_tokens"\s*:\s*(\d+).{0,400}?"cache_creation_input_tokens"\s*:\s*(\d+).{0,400}?'
        r'"cache_read_input_tokens"\s*:\s*(\d+).{0,400}?"output_tokens"\s*:\s*(\d+)',
        raw, re.DOTALL,
    )
    # reasoning (40) is billed as output → folded into output_tokens (spike §5)
    assert group and [int(g) for g in group.groups()] == [2700, 50, 1500, 180]
    assert "[stderr]" not in raw
    assert "[stderr]" in oc.normalize_raw(stdout="", stderr="boom", transcript=oc.parse_events(""))


def test_trace_summary_names_tools_and_answer_format(oc):
    stdout = _fixture("run_success.jsonl")
    t = oc.parse_events(stdout)
    raw = oc.normalize_raw(stdout=stdout, stderr="", transcript=t)
    summary = oc.build_trace_summary(raw, t.final_message)
    assert summary.startswith("OpenCode Trace Summary")
    assert "tagged" in summary and "read" in summary and "bash" in summary


# ── exec config + backend object ──────────────────────────────────────────────


def test_exec_config_round_trips_through_configure(oc, monkeypatch):
    monkeypatch.delenv("OPENCODE_EXEC_PATH", raising=False)
    oc.configure_opencode_exec(path="/opt/oc/bin/opencode", effort="low")
    cfg = oc.get_opencode_exec_config()
    assert cfg["path"] == "/opt/oc/bin/opencode" and cfg["effort"] == "low"
    assert "empty_response_retries" in cfg
    oc.configure_opencode_exec(path="", effort="")  # blanks fall back to defaults
    assert oc.get_opencode_exec_config()["path"] == "opencode"
    with pytest.raises(ValueError):
        oc.configure_opencode_exec(effort="turbo")


def test_backend_object_satisfies_the_executor_protocol(oc):
    backend = oc.BACKEND
    assert isinstance(backend, oc.ExecutorBackend)
    assert backend.name == oc.OPENCODE_BACKEND == "opencode_exec"
    assert backend.cli_binary() == oc.get_opencode_exec_config()["path"]
    assert backend.exec_config() == oc.get_opencode_exec_config()


# ── the run path (fake CLI) ───────────────────────────────────────────────────


_REAL_RUN = subprocess.run


def _fake_run(stdout: str, returncode: int = 0, stderr: str = ""):
    """A stand-in for the CLI only; `git init` (prepare_work_dir) still really runs."""
    calls: list[dict] = []

    def run(cmd, **kwargs):
        if cmd and str(cmd[0]).endswith("git"):
            return _REAL_RUN(cmd, **kwargs)
        calls.append({"cmd": cmd, **kwargs})
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    return run, calls


def test_run_writes_config_calls_the_cli_and_persists_artifacts(
    oc, base_url, local_runner, tmp_path, monkeypatch
):
    work_dir = tmp_path / "pred" / "work"
    work_dir.mkdir(parents=True)
    run, calls = _fake_run(_fixture("run_success.jsonl"))
    monkeypatch.setattr(oc.subprocess, "run", run)
    oc.configure_opencode_exec(path="opencode", effort="medium")

    response, raw = oc.run_opencode_exec(
        work_dir=str(work_dir), prompt="solve task.md", model="global.moonshotai.kimi-k3",
        timeout=120, allow_file_edits=True,
    )

    assert response.endswith("<answer>3 payment-service errors</answer>")
    call = calls[0]
    assert call["cmd"][:5] == ["opencode", "run", "--pure", "--format", "json"]
    assert call["cmd"][call["cmd"].index("--dir") + 1] == str(work_dir)
    assert call["cwd"] == str(work_dir) and call["timeout"] == 120
    assert call["stdin"] == subprocess.DEVNULL  # spike finding 1
    # the prompt carries the harness wrapper (read task.md + the skill, edits allowed)
    assert "Read task.md" in call["cmd"][-1] and "solve task.md" in call["cmd"][-1]
    config_path = tmp_path / "pred" / oc.HOME_DIRNAME / oc.CONFIG_FILENAME
    assert call["env"]["OPENCODE_CONFIG"] == str(config_path)
    assert call["env"]["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert call["env"]["HTTPS_PROXY"] == oc.DEAD_PROXY
    written = json.loads(config_path.read_text())
    assert written["permission"]["edit"] == "allow"
    assert not (work_dir / oc.CONFIG_FILENAME).exists()
    if shutil.which("git"):
        assert (work_dir / ".git").is_dir()
    # artifacts land next to the work dir, like every other exec backend
    assert (tmp_path / "pred" / "opencode_raw.txt").read_text() == raw
    summary = (tmp_path / "pred" / "opencode_trace_summary.txt").read_text()
    assert summary.startswith("OpenCode Trace Summary")
    assert "ATTEMPT 1" in raw


def test_run_retries_once_on_an_empty_response_and_reports_the_error(
    oc, base_url, local_runner, tmp_path, monkeypatch
):
    work_dir = tmp_path / "pred" / "work"
    work_dir.mkdir(parents=True)
    run, calls = _fake_run(_fixture("run_api_error.jsonl"), returncode=1, stderr="auth failed")
    monkeypatch.setattr(oc.subprocess, "run", run)
    oc.configure_opencode_exec(path="opencode", effort="medium")
    oc.set_empty_response_retries(1)

    response, raw = oc.run_opencode_exec(work_dir=str(work_dir), prompt="p", model="m", timeout=30)

    assert response == ""
    assert len(calls) == 2  # one retry
    assert "ATTEMPT 2" in raw
    markers = [
        json.loads(line) for line in raw.splitlines() if line.startswith('{"type": "exec_error"')
    ]
    assert markers and markers[0]["backend"] == "opencode_exec" and "401" in markers[0]["error"]


def test_run_times_out_cleanly(oc, base_url, local_runner, tmp_path, monkeypatch):
    work_dir = tmp_path / "pred" / "work"
    work_dir.mkdir(parents=True)

    def run(cmd, **kwargs):
        if str(cmd[0]).endswith("git"):
            return _REAL_RUN(cmd, **kwargs)
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], output="partial", stderr="")

    monkeypatch.setattr(oc.subprocess, "run", run)
    oc.set_empty_response_retries(0)
    response, raw = oc.run_opencode_exec(work_dir=str(work_dir), prompt="p", model="m", timeout=7)
    assert response == "" and "timed out after 7 seconds" in raw


def test_run_delegates_to_the_agentcore_runner_when_enabled(oc, base_url, tmp_path, monkeypatch):
    work_dir = tmp_path / "pred" / "work"
    work_dir.mkdir(parents=True)
    seen: dict = {}

    def fake_remote(**kwargs):
        seen.update(kwargs)
        return "remote answer", "remote raw"

    runner = importlib.import_module("skillopt.model.agentcore_runner")  # stdlib-only module
    monkeypatch.setattr(runner, "is_enabled", lambda: True)
    monkeypatch.setattr(runner, "run_remote_exec", fake_remote)
    response, raw = oc.run_opencode_exec(
        work_dir=str(work_dir), prompt="p", model="m", timeout=9, allow_file_edits=True,
    )
    assert (response, raw) == ("remote answer", "remote raw")
    assert seen["backend"] == "opencode_exec" and seen["exec_kwargs"]["allow_file_edits"] is True
    assert seen["exec_config"]["path"]  # the worker re-applies the exec config minus host bits
    assert (tmp_path / "pred" / "opencode_raw.txt").read_text() == "remote raw"


def test_judge_run_uses_the_restricted_agent_and_surfaces_cli_errors(
    oc, base_url, local_runner, tmp_path, monkeypatch
):
    work_dir = tmp_path / "judge"
    work_dir.mkdir()
    policy = {
        "judge": True,
        "mcp_servers": {
            "artifactctl": {
                "required": True,
                "command": ["python3", "-m", "x"],
                "tools": ["artifact_inventory"],
            }
        },
        "system_prompt": "trusted",
        "output_schema": {"type": "object"},
    }
    verdict = json.dumps({"type": "text", "part": {"type": "text", "text": '{"verdict": "pass"}'}})
    run, calls = _fake_run(verdict)
    monkeypatch.setattr(oc.subprocess, "run", run)
    response, _raw = oc.run_opencode_exec(
        work_dir=str(work_dir), prompt="judge", model="m", timeout=30, policy=policy
    )
    assert response == '{"verdict": "pass"}'
    assert "--agent" in calls[0]["cmd"]
    cfg = json.loads((tmp_path / oc.HOME_DIRNAME / oc.CONFIG_FILENAME).read_text())
    assert cfg["tools"]["bash"] is False and cfg["mcp"]["artifactctl"]["enabled"] is True

    run_err, _ = _fake_run(_fixture("run_api_error.jsonl"), returncode=1, stderr="nope")
    monkeypatch.setattr(oc.subprocess, "run", run_err)
    with pytest.raises(RuntimeError, match="401"):
        oc.run_opencode_exec(
            work_dir=str(work_dir), prompt="judge", model="m", timeout=30, policy=policy
        )
    with pytest.raises(ValueError):
        oc.run_opencode_exec(
            work_dir=str(work_dir), prompt="p", model="m", timeout=1, policy={"not": "judge"}
        )


# ─── N-R6: extra OpenAI-compatible providers from the Launchpad table ───────────────

SF_PROVIDERS = {
    "siliconflow": {
        "name": "SiliconFlow (OpenAI-compatible)",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key_env": "LAUNCHPAD_SILICONFLOW_API_KEY",
        "credential_provider": "launchpad-siliconflow",
        "id_prefix": "siliconflow.",
        "models": {"zai-org/GLM-5.3": {"name": "GLM-5.3 (SiliconFlow)", "tool_call": True}},
    }
}


@pytest.fixture
def sf_providers(monkeypatch, oc):
    monkeypatch.setenv(oc.PROVIDERS_ENV, json.dumps(SF_PROVIDERS))


def test_a_siliconflow_target_gets_its_own_provider_block_and_env_key(oc, base_url, sf_providers):
    config = oc.build_opencode_config(model="siliconflow.zai-org/GLM-5.3",
                                      allow_file_edits=False)
    assert set(config["provider"]) == {oc.PROVIDER_ID, "siliconflow"}
    sf = config["provider"]["siliconflow"]
    assert sf["npm"] == "@ai-sdk/openai-compatible"
    assert sf["options"] == {"baseURL": "https://api.siliconflow.cn/v1",
                             "apiKey": "{env:LAUNCHPAD_SILICONFLOW_API_KEY}"}
    assert sf["models"]["zai-org/GLM-5.3"]["tool_call"] is True
    assert config["model"] == "siliconflow/zai-org/GLM-5.3"
    # the Bedrock provider keeps only its own catalog — the SiliconFlow id is not grafted on
    assert "zai-org/GLM-5.3" not in config["provider"][oc.PROVIDER_ID]["models"]
    assert oc.model_ref("siliconflow.zai-org/GLM-5.3") == "siliconflow/zai-org/GLM-5.3"
    assert oc.model_ref("siliconflow/Qwen/Qwen3.8-27B") == "siliconflow/Qwen/Qwen3.8-27B"
    # Bedrock ids are untouched by the table
    assert oc.model_ref("global.zai.glm-5.3") == f"{oc.PROVIDER_ID}/global.zai.glm-5.3"
    bedrock = oc.build_opencode_config(model="global.zai.glm-5.3", allow_file_edits=False)
    assert set(bedrock["provider"]) == {oc.PROVIDER_ID}
    assert "LAUNCHPAD_SILICONFLOW_API_KEY" not in json.dumps(bedrock)
    assert json.dumps(config)


def test_an_unknown_siliconflow_model_is_still_addressed_on_the_provider(oc, base_url,
                                                                        sf_providers):
    config = oc.build_opencode_config(model="siliconflow.Qwen/Qwen3.8-27B", allow_file_edits=False)
    assert config["model"] == "siliconflow/Qwen/Qwen3.8-27B"
    assert config["provider"]["siliconflow"]["models"]["Qwen/Qwen3.8-27B"] == {
        "name": "Qwen/Qwen3.8-27B", "tool_call": True}


def test_subprocess_env_inherits_the_extra_providers_key_and_opens_its_host(oc, tmp_path,
                                                                           sf_providers,
                                                                           monkeypatch):
    monkeypatch.delenv(oc.NO_PROXY_ENV, raising=False)
    env = oc.subprocess_env(
        str(tmp_path / "opencode.json"),
        base={"PATH": "/bin", oc.API_KEY_ENV: "bearer-x", "LAUNCHPAD_SILICONFLOW_API_KEY": "sf-x",
              "LAUNCHPAD_ADMIN_PASSWORD": "secret"},
        base_url=BASE_URL,
    )
    assert env["LAUNCHPAD_SILICONFLOW_API_KEY"] == "sf-x" and env[oc.API_KEY_ENV] == "bearer-x"
    assert "LAUNCHPAD_ADMIN_PASSWORD" not in env
    parts = env["NO_PROXY"].split(",")
    assert parts[0] == "example.test" and "siliconflow.cn" in parts and "127.0.0.1" in parts


def test_no_providers_table_means_the_bedrock_only_behaviour(oc, base_url, monkeypatch):
    monkeypatch.delenv(oc.PROVIDERS_ENV, raising=False)
    assert oc.extra_providers() == {}
    assert oc.provider_for_model("siliconflow.X/Y") == (oc.PROVIDER_ID, None, "siliconflow.X/Y")
    monkeypatch.setenv(oc.PROVIDERS_ENV, "[1]")
    with pytest.raises(ValueError, match=oc.PROVIDERS_ENV):
        oc.extra_providers()
