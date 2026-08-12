"""Every AWS client is constructed in one place.

The workspace model (multi-account/multi-region) depends on no code path baking
in the hub's ambient credentials or region, so construction outside
`app/services/aws_clients.py` is a regression, not a style choice.
"""

import re
from pathlib import Path

import pytest

from app.services import aws_clients
from app.services import workspace as ws

APP_DIR = Path(__file__).resolve().parents[1] / "app"
CONSTRUCTOR_RE = re.compile(r"boto3\.(client|Session|resource)\(")

FACTORY = "services/aws_clients.py"
# Hub-scoped ambient-credential use, deliberately outside the workspace funnel:
# a Bedrock credential *availability* probe for the local Claude Code CLI.
EXEMPT = {"codegen/backends/claude_sdk.py"}


def test_no_boto3_construction_outside_the_factory():
    offenders: list[str] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel == FACTORY or rel in EXEMPT:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CONSTRUCTOR_RE.search(line):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "construct AWS clients via app.services.aws_clients:\n" + "\n".join(
        offenders
    )


def test_exemptions_still_exist():
    """A stale allowlist silently re-opens the hole it documents."""
    for rel in EXEMPT:
        text = (APP_DIR / rel).read_text(encoding="utf-8")
        assert CONSTRUCTOR_RE.search(text), f"{rel} no longer constructs a client — drop it"


def test_sessions_are_cached_per_target(monkeypatch):
    aws_clients.reset_cache()
    built: list[str | None] = []

    class FakeSession:
        def __init__(self, region_name=None):
            built.append(region_name)

    monkeypatch.setattr(aws_clients.boto3, "Session", FakeSession)
    first = aws_clients.get_session("111122223333", "us-west-2")
    again = aws_clients.get_session("111122223333", "us-west-2")
    other = aws_clients.get_session("111122223333", "us-east-1")

    assert first is again and first is not other
    assert built == ["us-west-2", "us-east-1"]
    aws_clients.reset_cache()


def test_clients_are_cached_per_service_unless_cfg_makes_them_unique(monkeypatch):
    """`data_client` relies on this: a per-settings Config would otherwise build a
    fresh client on every invoke turn, so it passes a cache_token instead."""
    aws_clients.reset_cache()
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: _StubSession())
    ctx = ws.WorkspaceContext(account_id="111122223333", region="us-west-2")

    assert ctx.client("s3") is ctx.client("s3")
    assert ctx.client("logs") is not ctx.client("s3")
    assert ws.WorkspaceContext("111122223333", "us-east-1").client("s3") is not ctx.client("s3")

    cfg = object()
    assert ctx.client("iam", config=cfg) is not ctx.client("iam", config=cfg)
    tokened = ctx.client("bedrock-agentcore", cache_token="read_timeout=1200", config=cfg)
    assert ctx.client("bedrock-agentcore", cache_token="read_timeout=1200", config=cfg) is tokened
    aws_clients.reset_cache()


class _StubSession:
    def client(self, service_name, **kwargs):
        return object()


def test_cross_account_roles_are_not_supported_yet():
    with pytest.raises(NotImplementedError):
        aws_clients.get_session("111122223333", "us-west-2", role_arn="arn:aws:iam::1:role/x")


def test_default_context_follows_settings(monkeypatch):
    """The context is read live: a settings reload must not serve a stale region."""
    regions = iter(["us-west-2", "eu-west-1"])
    monkeypatch.setattr(
        ws, "get_settings", lambda: _Settings(next(regions))
    )
    assert ws.default_workspace_context().region == "us-west-2"
    assert ws.default_workspace_context().region == "eu-west-1"


class _Settings:
    def __init__(self, region):
        self.region = region
        self.account_id = "111122223333"
        self.resources = {"artifacts_bucket": "bucket"}
