from types import SimpleNamespace

from app.services import aws_clients
from app.services import workspace as ws
from app.services.agentcore import client as client_mod


class FakeSession:
    def __init__(self):
        self.created: list[tuple[str, dict]] = []

    def client(self, service_name, **kwargs):
        self.created.append((service_name, kwargs))
        return SimpleNamespace(service_name=service_name, **kwargs)


def _stub_workspace(monkeypatch, region="us-east-1"):
    """Point the default workspace at `region` and record session lookups."""
    monkeypatch.setattr(
        ws,
        "get_settings",
        lambda: SimpleNamespace(region=region, account_id="111122223333", resources={}),
    )
    session = FakeSession()
    lookups: list[tuple] = []

    def fake_get_session(*args, **kwargs):
        lookups.append(args)
        return session

    monkeypatch.setattr(aws_clients, "get_session", fake_get_session)
    return session, lookups


def test_data_client_uses_configured_read_timeout(monkeypatch):
    session, lookups = _stub_workspace(monkeypatch)
    monkeypatch.setattr(
        client_mod,
        "get_settings",
        lambda: SimpleNamespace(agentcore_read_timeout_s=1200),
    )

    created = client_mod.data_client(ws.default_workspace_context())

    assert created.service_name == "bedrock-agentcore"
    assert created.config.read_timeout == 1200
    assert lookups == [("111122223333", "us-east-1", None, None)]


def test_clients_are_built_in_the_workspace_region(monkeypatch):
    session, lookups = _stub_workspace(monkeypatch, region="eu-central-1")

    ctx = ws.default_workspace_context()
    assert client_mod.control_client(ctx).service_name == "bedrock-agentcore-control"
    assert client_mod.registry_control_client(ctx).service_name == "agent-registry-control"
    assert client_mod.iam_client(ctx).service_name == "iam"
    assert {region for _account, region, _role, _ext in lookups} == {"eu-central-1"}
