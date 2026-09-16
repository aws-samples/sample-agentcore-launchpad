import os
import subprocess
import sys
from pathlib import Path

import app.core.config as config_mod
from app.core.config import Settings, load_yaml_config


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "missing.yaml")
    for name in (
        "LAUNCHPAD_REGION",
        "LAUNCHPAD_AUTH_USERNAME",
        "LAUNCHPAD_AUTH_PASSWORD",
        "LAUNCHPAD_AUTH_COOKIE_SECURE",
        "LAUNCHPAD_AGENTCORE_READ_TIMEOUT_S",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings()
    assert s.region == "us-west-2"
    assert s.database_url.startswith("sqlite:///")
    assert s.app_name == "AgentCore Launchpad"
    assert s.auth_username == "admin"
    assert s.auth_password is None
    assert s.auth_cookie_secure is False
    assert s.agentcore_read_timeout_s == 1000


def test_yaml_source_feeds_settings(tmp_path, monkeypatch):
    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text("region: eu-central-1\naccount_id: '123456789012'\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg)
    s = Settings()
    assert s.region == "eu-central-1"
    assert s.account_id == "123456789012"


def test_missing_yaml_is_empty(tmp_path):
    assert load_yaml_config(tmp_path / "nope.yaml") == {}


def test_env_overrides_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "launchpad.yaml"
    cfg.write_text("region: eu-central-1\n", encoding="utf-8")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", cfg)
    monkeypatch.setenv("LAUNCHPAD_REGION", "ap-southeast-1")
    assert Settings().region == "ap-southeast-1"


def test_auth_settings_from_environment(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    monkeypatch.setenv("LAUNCHPAD_AUTH_COOKIE_SECURE", "true")
    settings = Settings()
    assert settings.auth_username == "operator"
    assert settings.auth_password is not None
    assert settings.auth_password.get_secret_value() == "s3cret-pass"
    assert settings.auth_cookie_secure is True


def test_agentcore_read_timeout_from_environment(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AGENTCORE_READ_TIMEOUT_S", "1200")
    assert Settings().agentcore_read_timeout_s == 1200


def test_test_bootstrap_does_not_inherit_host_yaml_or_cached_settings(tmp_path):
    host = tmp_path / "host-launchpad.yaml"
    content = ("region: us-east-1\nstudio_exec_backend: docker\n"
               "resources:\n  artifacts_bucket: host-production-bucket\n")
    host.write_text(content)
    script = """
import sys
from pathlib import Path
from app.core import config

host = Path(sys.argv[1])
config.CONFIG_FILE = host
assert config.get_settings().region == "us-east-1"
assert config.get_settings().studio_exec_backend == "docker"

import tests.conftest
from app.core.db import engine

assert config.CONFIG_FILE != host
assert config.get_settings().region == "us-west-2"
assert config.get_settings().studio_exec_backend == "subprocess"
assert config.get_settings().resources["artifacts_bucket"] == "launchpad-artifacts-test"
assert str(engine.url) == config.get_settings().database_url
assert "launchpad-test-" in str(engine.url)
"""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("LAUNCHPAD_") and key not in ("AWS_REGION", "AWS_DEFAULT_REGION")}
    result = subprocess.run(
        [sys.executable, "-c", script, str(host)],
        cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert host.read_text() == content
