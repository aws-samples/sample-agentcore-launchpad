"""Deployment mode must agree between runtime discovery, CDK and cleanup."""
from types import SimpleNamespace

import pytest

from app.core import naming
from app.services import bootstrap, memory_bootstrap


@pytest.fixture
def dedicated(monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "env_name", "r28")
    monkeypatch.setattr(settings, "memory_domain_mode", "dedicated")
    return settings


def test_runtime_names_and_cdk_mode_agree(dedicated):
    assert memory_bootstrap.memory_domain_stack_name() == "launchpad-memory-domain-r28"
    assert memory_bootstrap.memory_admin_role_name("cn-northwest-1") == (
        "launchpad-memory-admin-cn-northwest-1-r28"
    )
    assert bootstrap.cdk_context_args({}) == [
        "--context", "env_name=r28", "--context", "memory_domain_mode=dedicated",
    ]
    with pytest.raises(RuntimeError, match="memory_domain_mode"):
        bootstrap.cdk_context_args({"LAUNCHPAD_CDK_CONTEXT": "memory_domain_mode=shared"})


def test_dedicated_cannot_select_default_domain(dedicated, monkeypatch):
    monkeypatch.setattr(dedicated, "env_name", "")
    with pytest.raises(ValueError, match="nonempty"):
        naming.memory_domain_env()


def test_legacy_shared_names_stay_unchanged(dedicated, monkeypatch):
    monkeypatch.setattr(dedicated, "memory_domain_mode", "shared")
    assert memory_bootstrap.memory_domain_stack_name() == "launchpad-memory-domain"
    assert memory_bootstrap.memory_admin_role_name("cn-northwest-1") == (
        "launchpad-memory-admin-cn-northwest-1"
    )


def test_dedicated_cleanup_never_discovers_shared_domain(dedicated, monkeypatch):
    from tests.test_teardown_script import teardown

    seen = []
    monkeypatch.setattr(
        teardown.bs, "get_stack_outputs", lambda region, name: seen.append(name) or {},
    )
    monkeypatch.setattr(teardown, "other_environment_memory_stacks", lambda _: pytest.fail(
        "dedicated cleanup must not depend on unrelated environments"
    ))
    target = teardown.shared_domain_target("cn-northwest-1", delete_shared_domain=False)
    assert seen == ["launchpad-memory-domain-r28"]
    assert target[:2] == ("cdk-stack", "launchpad-memory-domain-r28")


def test_dedicated_missing_stack_is_not_legacy_fallback(dedicated, monkeypatch):
    from tests.test_bootstrap_script import bootstrap_script

    monkeypatch.setattr(bootstrap_script, "ensure_agentcore_cli", lambda: "test")
    monkeypatch.setattr(bootstrap_script, "missing_stacks",
                        lambda _: ["launchpad-memory-domain-r28"])
    monkeypatch.setattr(bootstrap_script, "stack_exists", lambda *a: True)
    monkeypatch.setattr(bootstrap_script, "argparse", SimpleNamespace(
        ArgumentParser=lambda **kw: SimpleNamespace(
            add_argument=lambda *a, **k: None,
            parse_args=lambda: SimpleNamespace(
                region="cn-northwest-1", check_memory_admin=False, skip_cdk=True,
            ),
        ),
    ))
    assert bootstrap_script.main() == 1
