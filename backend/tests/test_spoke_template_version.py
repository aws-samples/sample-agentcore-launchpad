"""Spoke role template drift (`services/spoke_template.py`).

Found on real AWS twice (2026-10-23, 2026-10-28): a spoke stack fell behind the
template in the repo and nothing said so until a feature answered AccessDenied. The
templates now carry `launchpad:template-version`; this suite keeps that number honest
(a permission change without a bump fails here) and pins how the hub reads it.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError, EndpointConnectionError

from app.services import spoke_template, workspace_bootstrap
from app.services.workspace import WorkspaceContext

SPOKE = Path(__file__).resolve().parents[2] / "infra" / "spoke"

# The permission content of each template at spoke_template.EXPECTED_VERSION.
# Changing a statement, the trust policy or the managed-policy attachment changes
# these: bump EXPECTED_VERSION *and* the launchpad:template-version tag in BOTH
# templates, then paste the new values printed by the failing assertion.
FINGERPRINTS = {
    "launchpad-workspace-role.yaml":
        "ab74dea747efa821f1d4c46720ae34a174eafb8ba5b6a1ba6d134b4388a34f2a",
    "launchpad-workspace-role-prod.yaml":
        "c74e626e7bf12e01ede4fd6454b470c6046eb8a8e1f56c3805f6ff3ad6773e4c",
}
ROLE_ARN = "arn:aws:iam::444455556666:role/LaunchpadWorkspaceRole"


def _template(name: str) -> dict[str, Any]:
    return yaml.safe_load((SPOKE / name).read_text(encoding="utf-8"))


def _role(doc: dict[str, Any]) -> dict[str, Any]:
    (role,) = [r["Properties"] for r in doc["Resources"].values()
               if r["Type"] == "AWS::IAM::Role"]
    return role


def _fingerprint(doc: dict[str, Any]) -> str:
    """Only what IAM evaluates: comments, descriptions and tags do not count, so a
    prose edit never forces a version bump."""
    perms: dict[str, Any] = {}
    for logical_id, resource in sorted(doc["Resources"].items()):
        props = resource["Properties"]
        if resource["Type"] == "AWS::IAM::Role":
            perms[logical_id] = {
                "trust": props["AssumeRolePolicyDocument"],
                "managed": props.get("ManagedPolicyArns"),
                "inline": [p["PolicyDocument"] for p in props.get("Policies", [])],
            }
        elif "PolicyDocument" in props:
            perms[logical_id] = props["PolicyDocument"]
    return hashlib.sha256(json.dumps(perms, sort_keys=True).encode()).hexdigest()


@pytest.fixture(autouse=True)
def _fresh_cache():
    spoke_template.clear_cache()
    yield
    spoke_template.clear_cache()


# ── the version number tracks the templates ────────────────────────────────


def test_every_template_is_listed():
    assert set(FINGERPRINTS) == {p.name for p in SPOKE.glob("launchpad-workspace-role*.yaml")}
    assert sorted(Path(t).name for t in spoke_template.TEMPLATES.values()) == sorted(FINGERPRINTS)


@pytest.mark.parametrize("name", sorted(FINGERPRINTS))
def test_the_template_tags_the_version_the_hub_expects(name):
    tags = {t["Key"]: t["Value"] for t in _role(_template(name))["Tags"]}
    assert tags[spoke_template.VERSION_TAG] == str(spoke_template.EXPECTED_VERSION)
    variant = tags[spoke_template.VARIANT_TAG]
    assert Path(spoke_template.TEMPLATES[variant]).name == name


@pytest.mark.parametrize("name", sorted(FINGERPRINTS))
def test_a_permission_change_bumps_the_version(name):
    got = _fingerprint(_template(name))
    assert got == FINGERPRINTS[name], (
        f"{name}'s permissions changed: bump spoke_template.EXPECTED_VERSION and the "
        f"launchpad:template-version tag in both templates, then set "
        f"FINGERPRINTS[{name!r}] = {got!r}"
    )


# ── reading the deployed version ───────────────────────────────────────────


class _Iam:
    def __init__(self, tags: dict[str, str] | None = None, error: Exception | None = None):
        self.tags, self.error, self.calls = tags or {}, error, []

    def get_role(self, RoleName):  # noqa: N803 — boto3's casing
        self.calls.append(RoleName)
        if self.error:
            raise self.error
        return {"Role": {"RoleName": RoleName,
                         "Tags": [{"Key": k, "Value": v} for k, v in self.tags.items()]}}


def _ctx(monkeypatch, iam: _Iam, role_arn: str | None = ROLE_ARN) -> WorkspaceContext:
    ctx = WorkspaceContext(id="acct-usw1", account_id="444455556666", region="us-west-1",
                           role_arn=role_arn, external_id="x" if role_arn else None)
    monkeypatch.setattr(WorkspaceContext, "client",
                        lambda self, service, **_kw: iam if service == "iam" else None)
    return ctx


def _error(code: str, operation: str = "GetRole") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def _tags(version: str | None, variant: str = "workspace") -> dict[str, str]:
    tags = {spoke_template.VARIANT_TAG: variant}
    if version is not None:
        tags[spoke_template.VERSION_TAG] = version
    return tags


@pytest.mark.parametrize(
    ("version", "status"),
    [
        (str(spoke_template.EXPECTED_VERSION), "current"),
        (str(spoke_template.EXPECTED_VERSION - 1), "outdated"),
        (str(spoke_template.EXPECTED_VERSION + 1), "newer"),
        (None, "outdated"),
        ("v2", "outdated"),
    ],
)
def test_the_deployed_tag_decides_the_status(monkeypatch, version, status):
    iam = _Iam(_tags(version))
    out = spoke_template.check(_ctx(monkeypatch, iam))
    assert out["status"] == status
    assert out["expected"] == spoke_template.EXPECTED_VERSION
    assert iam.calls == ["LaunchpadWorkspaceRole"]


def test_a_stack_that_cannot_read_itself_predates_the_tag(monkeypatch):
    """Every template since the tag grants GetRole on the role itself, so a denial
    is the answer rather than an error: the stack is older than that."""
    out = spoke_template.check(_ctx(monkeypatch, _Iam(error=_error("AccessDenied"))))
    assert (out["status"], out["deployed"], out["reason"]) == ("outdated", None, "unreadable")
    assert out["template"] == "infra/spoke/launchpad-workspace-role.yaml"


def test_the_prod_variant_gets_the_prod_template_and_command(monkeypatch):
    out = spoke_template.check(_ctx(monkeypatch, _Iam(_tags("0", "workspace-prod"))))
    assert out["variant"] == "workspace-prod"
    assert out["template"].endswith("launchpad-workspace-role-prod.yaml")
    assert "--template-file infra/spoke/launchpad-workspace-role-prod.yaml" in out[
        "stack_command"]


def test_the_command_never_carries_the_external_id(monkeypatch):
    """`deploy` reuses a stack's existing parameter values, so the console shows a
    command without the shared secret."""
    out = spoke_template.check(_ctx(monkeypatch, _Iam(_tags("0"))))
    command = out["stack_command"]
    assert "CAPABILITY_NAMED_IAM" in command and "--stack-name launchpad-workspace-role" in command
    assert "ExternalId" not in command and "parameter-overrides" not in command


def test_a_same_account_workspace_has_nothing_to_check(monkeypatch):
    iam = _Iam(_tags("1"))
    out = spoke_template.check(_ctx(monkeypatch, iam, role_arn=None))
    assert out["status"] == "not_applicable" and iam.calls == []


def test_an_assume_role_failure_propagates_to_the_central_handler(monkeypatch):
    with pytest.raises(ClientError):
        spoke_template.check(_ctx(monkeypatch, _Iam(error=_error("AccessDenied", "AssumeRole"))))


@pytest.mark.parametrize("error", [_error("ServiceFailure"),
                                   EndpointConnectionError(endpoint_url="https://iam")])
def test_any_other_failure_is_unknown_and_not_cached(monkeypatch, error):
    iam = _Iam(error=error)
    ctx = _ctx(monkeypatch, iam)
    assert spoke_template.check(ctx)["status"] == "unknown"
    spoke_template.check(ctx)
    assert len(iam.calls) == 2


def test_the_answer_is_cached_until_refresh(monkeypatch):
    iam = _Iam(_tags("0"))
    ctx = _ctx(monkeypatch, iam)
    assert spoke_template.check(ctx)["status"] == "outdated"
    iam.tags = _tags(str(spoke_template.EXPECTED_VERSION))  # the admin updated the stack
    assert spoke_template.check(ctx)["status"] == "outdated"
    assert spoke_template.check(ctx, refresh=True)["status"] == "current"
    assert len(iam.calls) == 2


# ── bootstrap logs it, never fails on it ───────────────────────────────────


def _bootstrap_ctx(monkeypatch, iam: _Iam, logs: list[str]) -> Any:
    return SimpleNamespace(workspace=_ctx(monkeypatch, iam), log=logs.append)


def test_validate_access_warns_about_an_outdated_stack(monkeypatch):
    logs: list[str] = []
    workspace_bootstrap._log_role_template(
        _bootstrap_ctx(monkeypatch, _Iam(error=_error("AccessDenied")), logs))
    (line,) = logs
    assert line.startswith("WARNING spoke role template is outdated (deployed untagged")
    assert "aws cloudformation deploy" in line


def test_validate_access_survives_a_failing_check(monkeypatch):
    logs: list[str] = []
    workspace_bootstrap._log_role_template(
        _bootstrap_ctx(monkeypatch, _Iam(error=_error("AccessDenied", "AssumeRole")), logs))
    assert logs == ["spoke role template check skipped: ClientError"]
