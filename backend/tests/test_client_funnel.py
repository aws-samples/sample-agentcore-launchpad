"""Every AWS client is constructed in one place.

The workspace model (multi-account/multi-region) depends on no code path baking
in the hub's ambient credentials or region, so construction outside
`app/services/aws_clients.py` is a regression, not a style choice.
"""

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import botocore.session
import pytest
from botocore.exceptions import ClientError

from app.services import aws_clients
from app.services import workspace as ws

APP_DIR = Path(__file__).resolve().parents[1] / "app"
# The botocore alternatives count too: the cross-account branch builds a
# `botocore.session.Session` and `create_client` on one bypasses boto3 entirely.
CONSTRUCTOR_RE = re.compile(
    r"boto3\.(client|Session|resource)\(|botocore\.session\.|\.create_client\("
)

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


@pytest.mark.parametrize(
    "kwarg", ["region_name", "aws_access_key_id", "aws_secret_access_key", "aws_session_token"]
)
def test_cfg_cannot_override_the_workspace_target(monkeypatch, kwarg):
    """`cfg` is forwarded to botocore verbatim, so a region/credential kwarg would
    silently point a 'workspace' client somewhere else — and the cache key would
    not reflect it."""
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: _StubSession())
    ctx = ws.WorkspaceContext(account_id="111122223333", region="us-west-2")

    with pytest.raises(ValueError) as exc:
        ctx.client("s3", **{kwarg: "value"})
    assert kwarg in str(exc.value)
    # endpoint_url stays legal — it retargets the host, not the account/region
    assert ctx.client("s3", endpoint_url="http://localhost:4566") is not None


ROLE_ARN = "arn:aws:iam::444455556666:role/LaunchpadWorkspaceRole"


class _StubSTS:
    """Enough of an STS client for `AssumeRoleCredentialFetcher`."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": f"AK{len(self.calls)}",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(hours=1),
            }
        }


@pytest.fixture
def stub_sts(monkeypatch):
    """A hub with static credentials and an STS that never leaves the process."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "hub-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "hub-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    # An explicit profile drops the env provider out of botocore's chain, which
    # would send the source credentials to the operator's real ~/.aws (or IMDS).
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    sts = _StubSTS()
    monkeypatch.setattr(
        aws_clients, "_get_client_creator", lambda session, region: lambda *a, **k: sts
    )
    aws_clients.reset_cache()
    yield sts
    aws_clients.reset_cache()


def test_a_cross_account_session_assumes_the_role_lazily(stub_sts):
    """Construction must stay call-free: sessions are built for every workspace
    the console lists, and only the ones actually used should reach STS."""
    session = aws_clients.get_session(
        "444455556666", "us-east-2", role_arn=ROLE_ARN, external_id="launchpad-abc"
    )
    assert stub_sts.calls == []
    assert session.client("s3").meta.endpoint_url == "https://s3.us-east-2.amazonaws.com"
    assert stub_sts.calls == []

    frozen = session.get_credentials().get_frozen_credentials()
    assert frozen.access_key == "AK1"
    assert stub_sts.calls == [
        {
            "RoleArn": ROLE_ARN,
            "RoleSessionName": "launchpad-444455556666-us-east-2",
            "ExternalId": "launchpad-abc",
            "DurationSeconds": 3600,
        }
    ]
    # unexpired credentials are reused rather than re-assumed on every request
    session.get_credentials().get_frozen_credentials()
    assert len(stub_sts.calls) == 1


def test_one_credentials_object_serves_every_client_of_a_session(stub_sts):
    """The refresh lock lives on the credentials object, so a session that handed
    out two of them would refresh the same role twice in parallel."""
    session = aws_clients.get_session(
        "444455556666", "us-east-2", role_arn=ROLE_ARN, external_id="launchpad-abc"
    )
    assert session.get_credentials() is session.get_credentials()
    assert aws_clients.get_session(
        "444455556666", "us-east-2", role_arn=ROLE_ARN, external_id="launchpad-abc"
    ) is session


def test_the_role_is_part_of_the_session_key(stub_sts):
    """Same account and region, different credentials: the hub's own session must
    never be served to a workspace that names a role (or vice versa)."""
    hub = aws_clients.get_session("444455556666", "us-east-2")
    spoke = aws_clients.get_session(
        "444455556666", "us-east-2", role_arn=ROLE_ARN, external_id="launchpad-abc"
    )
    other_secret = aws_clients.get_session(
        "444455556666", "us-east-2", role_arn=ROLE_ARN, external_id="launchpad-xyz"
    )
    assert hub is not spoke and spoke is not other_secret


def test_an_external_id_is_optional_at_the_session_level(stub_sts):
    """The router requires the pair; STS does not, and sending an empty ExternalId
    would be rejected — so the argument is omitted rather than passed as None."""
    session = aws_clients.get_session("444455556666", "us-east-2", role_arn=ROLE_ARN)
    session.get_credentials().get_frozen_credentials()
    assert "ExternalId" not in stub_sts.calls[0]


def test_a_hub_without_credentials_says_so(stub_sts, monkeypatch):
    """Without this the fetcher keeps a None source and the first signed request
    dies on `'NoneType' has no attribute get_frozen_credentials`, naming neither
    the hub nor the role."""
    monkeypatch.setattr(botocore.session.Session, "get_credentials", lambda self: None)
    with pytest.raises(RuntimeError, match="no AWS credentials of its own"):
        aws_clients.get_session("444455556666", "us-east-2", role_arn=ROLE_ARN)


def test_assume_role_failures_are_recognised_and_explained():
    denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "not authorized"}}, "AssumeRole"
    )
    assert aws_clients.is_assume_role_failure(denied)
    diagnostic = aws_clients.assume_role_diagnostic(denied)
    assert "trust policy" in diagnostic and "ExternalId" in diagnostic
    assert "not authorized" in diagnostic

    # any other operation is somebody else's problem — mapping it as a credential
    # failure would send the operator after the wrong permission
    assert not aws_clients.is_assume_role_failure(
        ClientError({"Error": {"Code": "AccessDenied"}}, "CreateGateway")
    )


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
