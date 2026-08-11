"""Repository-managed AgentCore CLI bootstrap behavior."""

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import app.services.harness_convert as harness_convert

SCRIPT = Path(__file__).parents[2] / "scripts" / "bootstrap.py"


def _load_bootstrap_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("launchpad_bootstrap_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap_script = _load_bootstrap_script()


def _proc(stdout: str = "", returncode: int = 0) -> SimpleNamespace:
    return SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def test_managed_cli_path_matches_conversion_service():
    assert bootstrap_script.AGENTCORE_CLI == harness_convert.MANAGED_AGENTCORE_CLI


def test_managed_cli_current_version_skips_install(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bootstrap_script.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or _proc(f"{bootstrap_script.AGENTCORE_CLI_VERSION}\n"),
    )

    version = bootstrap_script.ensure_agentcore_cli()

    assert version == bootstrap_script.AGENTCORE_CLI_VERSION
    assert calls == [
        (
            ([str(bootstrap_script.AGENTCORE_CLI), "--version"],),
            {
                "cwd": bootstrap_script.REPO_ROOT,
                "capture_output": True,
                "text": True,
                "check": False,
            },
        )
    ]


def test_managed_cli_probe_returns_none_when_binary_is_absent(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(bootstrap_script.subprocess, "run", missing)

    assert bootstrap_script._managed_agentcore_cli_version() is None


def test_managed_cli_install_is_pinned_and_verified(monkeypatch):
    calls = []
    responses = iter(
        [
            _proc("0.20.0\n"),
            _proc(),
            _proc(f"{bootstrap_script.AGENTCORE_CLI_VERSION}\n"),
        ]
    )

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return next(responses)

    monkeypatch.setattr(bootstrap_script.subprocess, "run", fake_run)

    assert (
        bootstrap_script.ensure_agentcore_cli()
        == bootstrap_script.AGENTCORE_CLI_VERSION
    )
    assert calls[1] == (
        (
            [
                "npm",
                "install",
                "--prefix",
                str(bootstrap_script.AGENTCORE_CLI_PREFIX),
                "--no-save",
                "--package-lock=false",
                "@aws/agentcore@0.21.1",
            ],
        ),
        {"cwd": bootstrap_script.REPO_ROOT, "check": True},
    )


def test_managed_cli_failed_verification_aborts(monkeypatch):
    responses = iter([_proc("", returncode=1), _proc(), _proc("0.22.0\n")])
    monkeypatch.setattr(
        bootstrap_script.subprocess,
        "run",
        lambda *args, **kwargs: next(responses),
    )

    with pytest.raises(RuntimeError, match=r"expected 0\.21\.1, got 0\.22\.0"):
        bootstrap_script.ensure_agentcore_cli()


def test_managed_cli_npm_failure_is_not_suppressed(monkeypatch):
    failure = subprocess.CalledProcessError(1, ["npm", "install"])
    responses = iter([_proc("", returncode=1), failure])

    def fake_run(*args, **kwargs):
        result = next(responses)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(bootstrap_script.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        bootstrap_script.ensure_agentcore_cli()


def test_bootstrap_summary_omits_policy_resources(monkeypatch, capsys):
    monkeypatch.setattr(bootstrap_script, "ensure_agentcore_cli", lambda: "0.21.1")
    monkeypatch.setattr(bootstrap_script, "stack_exists", lambda _region: True)
    monkeypatch.setattr(
        bootstrap_script.bs,
        "run_bootstrap",
        lambda _region: {
            "account_id": "111",
            "region": "us-west-2",
            "registry": {
                "available": True,
                "arn": "arn:registry",
                "created": False,
                "reason": None,
            },
            "memory": {"arn": "arn:memory", "created": False},
            "gateway": {
                "gateway": {"url": "https://gateway.example/mcp", "created": True},
                "api_key_provider": {"created": True},
                "targets": {},
            },
            "observability": {
                "enabled": True,
                "changed": False,
                "status": "ACTIVE",
            },
            "demo_passwords_set": False,
            "stack_outputs": {
                "ArtifactsBucketName": "bucket",
                "EcrRepoUri": "repo",
                "CodeBuildProjectName": "build",
                "UserPoolId": "pool",
            },
        },
    )
    monkeypatch.setattr(
        bootstrap_script.sys,
        "argv",
        ["bootstrap.py", "--region", "us-west-2"],
    )

    assert bootstrap_script.main() == 0

    output = capsys.readouterr().out
    assert "gateway state" in output
    assert "transaction search" in output
    assert "policy engine" not in output
    assert "gateway policy" not in output
    assert "gateway traces" not in output
