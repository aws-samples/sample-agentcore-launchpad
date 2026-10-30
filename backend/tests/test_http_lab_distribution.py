"""HTTP demo entry is explicit; existing HTTPS/state ownership stays intact."""
import json
import subprocess
from types import SimpleNamespace

import pytest

from tests.test_cn_distribution import REPO, config, module


@pytest.mark.parametrize("override", [
    {"console_http_lab": "true"},
    {"console_http_lab": True, "console_entry": "direct"},
    {"console_http_lab": True, "http_port": 8080},
    {"console_http_lab": True, "http_port": 8443},
    {"console_http_lab": True, "http_port": 443},
    {"console_http_lab": True, "http_port": 65536},
    {"console_http_lab": True, "http_port": "oops"},
    {"http_port": 8088},
])
def test_http_requires_explicit_compatible_configuration(tmp_path, override):
    path, cfg = config(tmp_path)
    cfg.update(override)
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError):
        module(REPO / "scripts/deploy_cn.py").read_config(path)


def test_http_primary_propagates_to_installer_and_fresh_verifier(tmp_path):
    path, cfg = config(tmp_path)
    cfg.update(console_http_lab=True, http_port=8088)
    path.write_text(json.dumps(cfg))
    deploy = module(REPO / "scripts/deploy_cn.py")
    read = deploy.read_config(path)
    outputs = {
        "ConsoleUrl": "http://192.0.2.11:8088", "DirectConsoleUrl": "https://192.0.2.10:8443",
        "PolicyPrincipalKeyArn": "key", "OpsRoleName": "ops", "PrivateIp": "10.0.0.1",
    }
    settings = deploy.policy_settings(read, outputs)
    assert settings["console_http_lab"] is True
    assert settings["oauth_return_url"] == ""
    assert settings["public_base_url"] == outputs["ConsoleUrl"]
    command = deploy.verification_script(read, outputs, {"commit": "a" * 40})
    assert '"console_http_lab": true' in command
    assert '"public_base_url": "http://192.0.2.11:8088"' in command


def test_fresh_settings_reject_unexpected_http_opt_in():
    verify = module(REPO / "scripts/verify_deployment_host.py")
    with pytest.raises(verify.VerificationError, match="HTTP lab"):
        verify.verify_settings(SimpleNamespace(console_http_lab=True), {})


def test_new_host_helper_and_nlb_helper_parse():
    for name in ("configure_http_lab_host.sh", "ec2-console-nlb.sh"):
        subprocess.run(["bash", "-n", str(REPO / "scripts" / name)], check=True)


def test_workshop_requires_explicit_http_and_one_primary_origin(tmp_path, monkeypatch):
    import boto3

    cfg = json.loads((REPO / "samples/itsm-cn/config.example.json").read_text())
    cfg.update(
        account_id="123456789012", tools_host="i-1234567890abcdef0",
        console="http://192.0.2.11:8088", api_base="http://192.0.2.11:8088",
        console_http_lab=True, state_dir="state",
    )
    path = tmp_path / "workshop.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", str(path))
    monkeypatch.setattr(boto3, "Session", lambda **kw: SimpleNamespace(client=lambda n: None))
    lab = module(REPO / "samples/itsm-cn/scripts/lab.py")
    assert lab.CONFIG["console"] == cfg["console"]
    cfg.pop("console_http_lab")
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="HTTPS"):
        lab.read_config(path)
    cfg.update(console_http_lab=True, api_base="http://192.0.2.12:8088")
    path.write_text(json.dumps(cfg))
    with pytest.raises(ValueError, match="same primary"):
        lab.read_config(path)
