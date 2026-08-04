"""Studio local-debug execution: SSE framing, env builder, execute endpoint
(stub interpreter), timeout kill, missing-interpreter guard."""

import asyncio
import os
import sys
import tempfile

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services import local_exec  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def exec_python(monkeypatch):
    """Point the exec interpreter at this test's own python (no strands needed
    for the print-only scripts these tests run)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "studio_exec_python", sys.executable)
    return sys.executable


# --- chunk_to_sse framing -------------------------------------------------

def test_chunk_to_sse_single_line():
    assert local_exec.chunk_to_sse("hello") == "data: hello\n\n"


def test_chunk_to_sse_multiline_each_newline_is_empty_data_line():
    # "a\nb" -> segment a, newline (empty data), segment b
    assert local_exec.chunk_to_sse("a\nb") == "data: a\ndata: \ndata: b\n\n"


def test_chunk_to_sse_trailing_newline():
    # a chunk that ends on a newline (e.g. a read() boundary split a line)
    assert local_exec.chunk_to_sse("a\n") == "data: a\ndata: \n\n"


def test_chunk_to_sse_lone_newline():
    assert local_exec.chunk_to_sse("\n") == "data: \n\n"


def test_chunk_to_sse_empty_is_empty():
    assert local_exec.chunk_to_sse("") == ""


def test_chunk_to_sse_roundtrip_preserves_text():
    # Decode the SSE the way the frontend does (empty `data: ` line = newline)
    # and confirm the original chunk survives regardless of embedded newlines.
    chunk = "line one\nline two\n\nline four"
    sse = local_exec.chunk_to_sse(chunk)
    event = sse[: -len("\n\n")]  # strip the event terminator
    decoded = "".join(
        "\n" if line == "data: " else line[len("data: "):]
        for line in event.split("\n")
    )
    assert decoded == chunk


# --- build_execution_env --------------------------------------------------

def test_build_execution_env_sets_consent_and_region():
    env = local_exec.build_execution_env()
    assert env["BYPASS_TOOL_CONSENT"] == "true"
    assert env["STRANDS_NON_INTERACTIVE"] == "true"
    assert env["AWS_REGION"]  # region always present
    assert env["AWS_DEFAULT_REGION"] == env["AWS_REGION"]


def test_build_execution_env_injects_keys_when_given():
    env = local_exec.build_execution_env(openai_api_key="sk-x", bedrock_api_key="bk-y")
    assert env["OPENAI_API_KEY"] == "sk-x"
    assert env["BEDROCK_API_KEY"] == "bk-y"


def test_build_execution_env_omits_keys_when_absent(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("BEDROCK_API_KEY", raising=False)
    env = local_exec.build_execution_env()
    assert "OPENAI_API_KEY" not in env
    assert "BEDROCK_API_KEY" not in env


# --- environment allowlist (T2) -------------------------------------------

def test_build_execution_env_drops_unrelated_host_variables(monkeypatch):
    """The child gets an allowlist, not os.environ.copy() — the backend's own
    environment carries the ledger URL, LAUNCHPAD_* settings and whatever the
    operator's shell holds."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "console-password")
    monkeypatch.setenv("LAUNCHPAD_DATABASE_URL", "sqlite:///ledger.db")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    env = local_exec.build_execution_env()
    for leaked in (
        "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "LAUNCHPAD_AUTH_PASSWORD",
        "LAUNCHPAD_DATABASE_URL", "SSH_AUTH_SOCK",
    ):
        assert leaked not in env, f"{leaked} reached the execution subprocess"


def test_build_execution_env_keeps_what_generated_code_needs(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("STUDIO_SKILLS_DIR", "/srv/skills")
    env = local_exec.build_execution_env()
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/operator"
    assert env["STUDIO_SKILLS_DIR"] == "/srv/skills"


def test_aws_credentials_are_forwarded_by_default(monkeypatch):
    """The default Bedrock Mantle path mints its bearer token from the ambient
    credentials, so local debug works off the operator's AWS profile."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    monkeypatch.setenv("AWS_PROFILE", "dev")
    env = local_exec.build_execution_env()
    assert env["AWS_ACCESS_KEY_ID"] == "AKIAEXAMPLE"
    assert env["AWS_SESSION_TOKEN"] == "session-token"
    assert env["AWS_PROFILE"] == "dev"
    assert "AWS_EC2_METADATA_DISABLED" not in env


def test_hardened_mode_withholds_aws_credentials(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "session-token")
    monkeypatch.setenv("AWS_PROFILE", "dev")
    env = local_exec.build_execution_env(
        settings=Settings(studio_exec_forward_aws_credentials=False)
    )
    for name in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        assert name not in env
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    # The region is still needed to build a model client at all.
    assert env["AWS_REGION"]


# --- resource ceilings and uid drop (T2) ----------------------------------

def test_spawn_kwargs_carry_the_isolation():
    from app.core.config import Settings

    kwargs = local_exec.build_spawn_kwargs(Settings())
    assert kwargs["start_new_session"] is True  # kill_process_group depends on it
    assert callable(kwargs["preexec_fn"])  # resource ceilings


def test_spawn_kwargs_omit_the_uid_drop_when_unconfigured():
    from app.core.config import Settings

    kwargs = local_exec.build_spawn_kwargs(Settings(studio_exec_user=""))
    assert "user" not in kwargs
    assert "group" not in kwargs


def test_the_uid_drop_does_not_use_preexec_fn():
    """`preexec_fn` runs arbitrary Python after fork and is deadlock-prone in a
    threaded process — and this backend runs deploy jobs on threads. The uid drop
    must therefore go through subprocess's C-level `user`/`group` arguments."""
    import inspect

    source = inspect.getsource(local_exec.build_spawn_kwargs)
    body = source.split("kwargs: dict")[1]
    assert '"user"' in body and '"group"' in body
    assert "setuid" not in source and "setgid" not in source


def test_the_resource_ceilings_are_actually_applied():
    """Runs the limit hook in a real child and reads the limits back."""
    import json
    import subprocess as sp
    import sys

    from app.core.config import Settings

    settings = Settings(studio_exec_memory_mb=512, studio_exec_cpu_seconds=7)
    kwargs = local_exec.build_spawn_kwargs(settings)
    probe = (
        "import json,resource;"
        "print(json.dumps([resource.getrlimit(resource.RLIMIT_AS),"
        "resource.getrlimit(resource.RLIMIT_CPU)]))"
    )
    out = sp.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, check=True,
        preexec_fn=kwargs["preexec_fn"],
    )
    rlimit_as, rlimit_cpu = json.loads(out.stdout)
    assert rlimit_as == [512 * 1024 * 1024, 512 * 1024 * 1024]
    assert rlimit_cpu == [7, 7]


def test_no_uid_drop_without_a_configured_user():
    from app.core.config import Settings

    assert local_exec._exec_user_ids(Settings(studio_exec_user="")) is None


def test_a_missing_exec_user_is_reported_clearly():
    from app.core.config import Settings

    with pytest.raises(local_exec.ExecUserUnavailable, match="does not exist"):
        local_exec._exec_user_ids(
            Settings(studio_exec_user="launchpad-exec-does-not-exist")
        )


def test_a_non_root_backend_cannot_use_the_exec_user(monkeypatch):
    """Verified on a real host: `subprocess(user=…)` and chowning the workdir both
    raise EPERM for a non-root parent, and `make dev` runs the backend as the
    operator. The precondition has to be stated, not discovered mid-spawn."""
    from app.core.config import Settings

    monkeypatch.setattr(local_exec.os, "geteuid", lambda: 1000)
    settings = Settings(studio_exec_user="root")  # exists on every host
    with pytest.raises(local_exec.ExecUserUnavailable, match="only root can switch"):
        local_exec._exec_user_ids(settings)


def test_exec_user_error_reports_instead_of_raising(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setattr(local_exec.os, "geteuid", lambda: 1000)
    message = local_exec.exec_user_error(Settings(studio_exec_user="root"))
    assert message and "only root can switch" in message
    assert local_exec.exec_user_error(Settings(studio_exec_user="")) is None


def test_exec_user_is_accepted_when_running_as_root(monkeypatch):
    from app.core.config import Settings

    monkeypatch.setattr(local_exec.os, "geteuid", lambda: 0)
    assert local_exec._exec_user_ids(Settings(studio_exec_user="root")) == (0, 0)


def test_grant_workdir_is_a_noop_without_a_configured_user(tmp_path):
    from app.core.config import Settings

    before = (tmp_path / "code.py")
    before.write_text("print(1)")
    local_exec.grant_workdir_to_exec_user(str(tmp_path), Settings(studio_exec_user=""))
    assert before.read_text() == "print(1)"


# --- /api/execute (stub interpreter) --------------------------------------

def test_execute_runs_via_configured_interpreter(client, exec_python):
    resp = client.post("/api/execute", json={"code": "print(1 + 1)"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["output"].strip() == "2"
    assert body["execution_time_ms"] >= 0


def test_execute_reports_nonzero_exit_as_failure(client, exec_python):
    resp = client.post("/api/execute", json={"code": "raise SystemExit(3)"})
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_execute_passes_user_input(client, exec_python):
    code = (
        "import argparse\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--user-input')\n"
        "a = p.parse_args()\n"
        "print('got:', a.user_input)\n"
    )
    resp = client.post("/api/execute", json={"code": code, "input_data": "hi there"})
    assert resp.json()["output"].strip() == "got: hi there"


def test_execute_stream_frames_multiline_output(client, exec_python):
    code = "print('alpha')\nprint('beta')\n"
    resp = client.post("/api/execute/stream", json={"code": code})
    assert resp.status_code == 200
    text = resp.text
    assert "data: alpha" in text
    assert "data: beta" in text
    assert "[STREAM_COMPLETE:" in text


# --- timeout + missing interpreter ---------------------------------------

def test_execute_times_out_and_kills(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "studio_exec_python", sys.executable)
    monkeypatch.setattr(settings, "execute_timeout_s", 0.5)
    with pytest.raises(RuntimeError, match="timed out"):
        asyncio.run(local_exec.execute_strands_code("import time; time.sleep(5)"))


def test_missing_interpreter_returns_503(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "studio_exec_python", "/no/such/python-xyz")
    resp = client.post("/api/execute", json={"code": "print(1)"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "studio.exec.interpreter_unavailable"
    assert "setup_exec_env.sh" in body["message"]


def test_spawn_raises_when_interpreter_missing(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "studio_exec_python", "/no/such/python-xyz")
    with pytest.raises(local_exec.ExecInterpreterUnavailable, match="setup_exec_env.sh"):
        asyncio.run(local_exec.spawn_execution_subprocess("print(1)", None))


# --- production refuses the surface outright (T2) -------------------------

def test_prod_mode_refuses_execute(client, exec_python, monkeypatch):
    """The actual T2 mitigation: production does not offer this at all."""
    settings = get_settings()
    monkeypatch.setattr(settings, "run_mode", "prod")
    monkeypatch.setattr(settings, "studio_local_exec_enabled", None)
    for path in ("/api/execute", "/api/execute/stream"):
        resp = client.post(path, json={"code": "print(1)"})
        assert resp.status_code == 403, path
        assert resp.json()["code"] == "studio.exec.disabled"
        assert "LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED" in resp.json()["message"]


def test_prod_mode_refuses_the_conversation_entrance_too(client, exec_python, monkeypatch):
    """Closing /api/execute while leaving this open would only move the door —
    conversations spawns the same interpreter on the same caller-supplied code."""
    settings = get_settings()
    monkeypatch.setattr(settings, "run_mode", "prod")
    monkeypatch.setattr(settings, "studio_local_exec_enabled", None)
    resp = client.post("/api/conversations", json={"generated_code": "print(1)"})
    assert resp.status_code == 403
    assert resp.json()["code"] == "studio.exec.disabled"


def test_explicit_opt_in_restores_execute_in_prod(client, exec_python, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "run_mode", "prod")
    monkeypatch.setattr(settings, "studio_local_exec_enabled", True)
    resp = client.post("/api/execute", json={"code": "print(1 + 1)"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
