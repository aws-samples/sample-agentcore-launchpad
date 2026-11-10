"""Host acceptance uses live process settings and fails closed without disclosing secrets."""
import json
import sys
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tests.test_cn_distribution import REPO, module


@pytest.fixture
def verify():
    return module(REPO / "scripts/verify_deployment_host.py")


def settings(verify, **overrides):
    values = dict.fromkeys(verify.SETTING_KEYS, "")
    values.update(
        account_id="123456789012", region="cn-northwest-1", env_name="n42",
        memory_domain_mode="dedicated", public_base_url="https://192.0.2.43:8443",
        auth_password_policy="relaxed", auth_password_min_length=8,
    )
    return SimpleNamespace(**dict(values, **overrides))


def test_actual_uvicorn_environment_excludes_uv_wrapper(tmp_path, verify):
    for pid, children, args in (
        (11, "12", b"/usr/local/bin/uv\0run\0uvicorn\0app.main:app\0"),
        (12, "13", b"/venv/bin/python\0/venv/bin/uvicorn\0app.main:app\0"),
        (13, "", b"/bin/helper\0"),
    ):
        base = tmp_path / str(pid)
        (base / "task" / str(pid)).mkdir(parents=True)
        (base / "task" / str(pid) / "children").write_text(children)
        (base / "cmdline").write_bytes(args)
        (base / "environ").write_bytes(b"LAUNCHPAD_AUTH_PASSWORD_POLICY=strict\0")
    (tmp_path / "12" / "environ").write_bytes(
        b"LAUNCHPAD_AUTH_PASSWORD_POLICY=relaxed\0LAUNCHPAD_AUTH_PASSWORD=private-test-secret\0",
    )
    pid = verify.backend_process(11, tmp_path)
    assert pid == 12
    assert verify.process_environment(pid, tmp_path)["LAUNCHPAD_AUTH_PASSWORD_POLICY"] == "relaxed"


@pytest.mark.parametrize("mode,code,message,fallback", [
    ("shared", "ValidationError", "Stack does not exist", True),
    ("dedicated", "ValidationError", "Stack does not exist", False),
    ("shared", "AccessDenied", "Stack does not exist", False),
    ("shared", "ValidationError", "invalid name", False),
])
def test_host_legacy_fallback_is_strict(verify, mode, code, message, fallback):
    calls = []

    def read(region, name):
        calls.append(name)
        if name == "launchpad-memory-n42":
            return {"MemoryDomainEndpoint": "legacy"}
        raise ClientError({"Error": {"Code": code, "Message": message}}, "DescribeStacks")

    configured = settings(verify, memory_domain_mode=mode)
    if fallback:
        assert verify.selected_domain_outputs(configured, read)["MemoryDomainEndpoint"] == "legacy"
        assert len(calls) == 2
    else:
        with pytest.raises(ClientError):
            verify.selected_domain_outputs(configured, read)
        assert len(calls) == 1


def test_network_error_never_uses_legacy_fallback(verify):
    calls = []

    def read(*args):
        calls.append(args)
        raise ConnectionError("network unavailable")

    with pytest.raises(ConnectionError):
        verify.selected_domain_outputs(settings(verify, memory_domain_mode="shared"), read)
    assert len(calls) == 1


@pytest.mark.parametrize("key,value", [
    ("memory_domain_mode", "shared"), ("env_name", "old"),
    ("auth_password_policy", "strict"), ("auth_password_min_length", 12),
    ("public_base_url", "https://192.0.2.99:8443"),
    ("oauth_return_url", "https://192.0.2.99:8443/auth/return"),
    ("policy_gateway_interceptor", True), ("policy_principal_kms_key_id", "expected-key"),
    ("policy_hub_role_arn", "expected-role"), ("policy_engine_endpoints", ["expected-endpoint"]),
])
def test_process_settings_mismatch_is_not_ready(verify, key, value):
    with pytest.raises(verify.VerificationError, match=key):
        verify.verify_settings(settings(verify), {key: value})


def test_nlb_marker_required_for_public_entry(verify, monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_CONSOLE_ENTRY", raising=False)
    with pytest.raises(verify.VerificationError, match="marker"):
        verify.verify_settings(settings(verify), {"console_entry": "public_nlb"})
    monkeypatch.setenv("LAUNCHPAD_CONSOLE_ENTRY", "public_nlb")
    assert verify.verify_settings(settings(verify), {"console_entry": "public_nlb"})


@pytest.mark.parametrize("policy,auth,healthy,passes", [
    ({"mode": "relaxed", "min_length": 8}, True, True, True),
    ({"mode": "strict", "min_length": 12}, True, True, False),
    ({"mode": "relaxed", "min_length": 8}, False, True, False),
    ({"mode": "relaxed", "min_length": 8}, True, False, False),
])
def test_api_confirms_actual_password_policy(verify, policy, auth, healthy, passes):
    client = SimpleNamespace(get=lambda url: SimpleNamespace(
        status_code=200, json=lambda: {"status": "ok" if healthy else "error"}
        if url.endswith("/health") else {"auth_required": auth, "password_policy": policy},
    ))
    if passes:
        assert verify.verify_http(settings(verify), client) == policy
    else:
        with pytest.raises(verify.VerificationError):
            verify.verify_http(settings(verify), client)


@pytest.mark.parametrize("healthy,bad_shard", [(False, None), (True, 0), (True, 1), (True, None)])
def test_fresh_opensearch_and_every_policy_shard_are_checked(
    verify, monkeypatch, healthy, bad_shard,
):
    from app.policy import dogwood_compiler
    from app.services import aws_clients, bootstrap, memory_bootstrap, policy_engine_control

    configured = settings(verify)
    configured.resources = {
        "memory_os_endpoint": "new-domain", "memory_os_domain_arn": "new-domain-arn",
        "memory_admin_role_arn": "new-role",
    }
    monkeypatch.setattr(bootstrap, "get_stack_outputs", lambda *_: {
        "MemoryDomainEndpoint": "new-domain", "MemoryDomainArn": "new-domain-arn",
        "MemoryAdminRoleArn": "new-role",
    })
    seen = []
    monkeypatch.setattr(aws_clients, "opensearch_http",
                        lambda ctx, endpoint: seen.append((ctx.role_arn, endpoint)) or object())
    monkeypatch.setattr(memory_bootstrap, "probe_memory_substrate", lambda _: (healthy, "private"))
    monkeypatch.setattr(dogwood_compiler, "engine_bin", lambda: "engine")
    monkeypatch.setattr(policy_engine_control, "shard_count", lambda: 2)
    monkeypatch.setattr(policy_engine_control, "call",
                        lambda shard, _: seen.append(shard) or {"ok": shard != bad_shard})
    if not healthy or bad_shard is not None:
        with pytest.raises(verify.VerificationError):
            verify.verify_resources(configured)
    else:
        assert verify.verify_resources(configured) == {"opensearch": "healthy", "policy_shards": 2}
        assert seen == [("new-role", "new-domain"), 0, 1]


def test_unexpected_failure_never_prints_secret_payload(verify, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["verify", "--worker", "--expected", json.dumps({})])
    monkeypatch.setattr(
        verify, "verify_worker",
        lambda _: (_ for _ in ()).throw(ValueError("private-test-secret")),
    )
    assert verify.main() == 1
    output = capsys.readouterr()
    assert "private-test-secret" not in output.err + output.out
    assert "ValueError" in output.err


def test_root_passes_only_live_environment_to_service_user(verify, monkeypatch, capsys):
    monkeypatch.setattr(verify.os, "geteuid", lambda: 0)
    monkeypatch.setattr(verify, "service_pid", lambda: 11)
    monkeypatch.setattr(verify, "backend_process", lambda _: 12)
    live_env = {
        "LAUNCHPAD_MEMORY_DOMAIN_MODE": "dedicated",
        "LAUNCHPAD_AUTH_PASSWORD": "private-test-secret",
    }
    monkeypatch.setattr(verify, "process_environment", lambda _: live_env)
    original_resolve = verify.Path.resolve
    monkeypatch.setattr(
        verify.Path, "resolve",
        lambda path, *a, **kw: verify.REPO / "backend" if str(path) == "/proc/12/cwd"
        else original_resolve(path, *a, **kw),
    )
    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if args[0] == "git":
            return SimpleNamespace(stdout="release\n")
        assert args[:5] == ["/usr/sbin/runuser", "-u", "ubuntu", "-m", "--"]
        assert "-B" in args
        assert kwargs["env"] == live_env
        assert "private-test-secret" not in " ".join(args)
        return SimpleNamespace(returncode=0, stdout='{"opensearch": "healthy"}', stderr="")

    monkeypatch.setattr(verify.subprocess, "run", run)
    verify.verify_host({"commit": "release"})
    output = capsys.readouterr()
    assert "private-test-secret" not in output.out + output.err
    assert json.loads(output.out)["backend_pid"] == 12
    assert len(commands) == 2
