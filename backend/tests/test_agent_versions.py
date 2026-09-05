"""GET /api/agents/{id}/versions — the read-only VERSIONS & ENDPOINTS view.

Hermetic: the control client is a stub that records calls and serves scripted
pages. The properties asserted: the resource kind picks the Runtime or Harness
list pair, every ``nextToken`` page is followed, the projection is allow-listed
(nothing sensitive from the AWS summaries survives), and rows without an AWS
resource or from another workspace are refused the right way.
"""

from datetime import UTC, datetime

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.services.agentcore import harness as harness_api
from app.services.agentcore import runtime as runtime_api

RUNTIME_ID = "hr_assistant-AbCdEf1234"
HARNESS_ID = "hr-harness-XyZ987"
SENSITIVE = {
    "environmentVariables": {"PRIVATE_TOKEN": "do-not-leak"},
    "roleArn": "arn:aws:iam::111122223333:role/private-role",
    "agentRuntimeArtifact": {"codeConfiguration": {"code": {"s3": {"bucket": "b"}}}},
    "authorizerConfiguration": {"customJWTAuthorizer": {"allowedClients": ["c"]}},
    "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/{RUNTIME_ID}",
}
FORBIDDEN_KEYS = {
    "environmentVariables",
    "roleArn",
    "artifact",
    "agentRuntimeArtifact",
    "authorizerConfiguration",
    "agentRuntimeArn",
    "arn",
    "agentRuntimeEndpointArn",
}


def _runtime_version(v: str, **extra) -> dict:
    return {
        "agentRuntimeId": RUNTIME_ID,
        "agentRuntimeName": "hr_assistant",
        "agentRuntimeVersion": v,
        "description": f"v{v}",
        "lastUpdatedAt": datetime(2026, 9, 1, 12, int(v), tzinfo=UTC),
        "status": "READY",
        **SENSITIVE,
        **extra,
    }


def _runtime_endpoint(name: str, live: str, target: str | None = None, **extra) -> dict:
    return {
        "name": name,
        "id": f"{name}-id",
        "liveVersion": live,
        "targetVersion": target or live,
        "status": "READY",
        "description": None,
        "createdAt": datetime(2026, 8, 1, tzinfo=UTC),
        "lastUpdatedAt": datetime(2026, 9, 1, tzinfo=UTC),
        **SENSITIVE,
        **extra,
    }


class StubControl:
    """Serves scripted pages per operation and records every call's kwargs."""

    def __init__(self, pages: dict[str, list[dict]]):
        self.pages = {op: list(p) for op, p in pages.items()}
        self.calls: list[tuple[str, dict]] = []

    def _serve(self, op: str, kw: dict) -> dict:
        self.calls.append((op, kw))
        remaining = self.pages.get(op) or [{}]
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    def list_agent_runtime_versions(self, **kw):
        return self._serve("list_agent_runtime_versions", kw)

    def list_agent_runtime_endpoints(self, **kw):
        return self._serve("list_agent_runtime_endpoints", kw)

    def list_harness_versions(self, **kw):
        return self._serve("list_harness_versions", kw)

    def list_harness_endpoints(self, **kw):
        return self._serve("list_harness_endpoints", kw)

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _seed(
    *,
    method: str,
    resource_id: str | None,
    status: str = "active",
    version: str | None = "2",
    spec: dict | None = None,
    workspace_id: str = DEFAULT_WORKSPACE_ID,
) -> str:
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=workspace_id,
            name=f"a-{method}",
            method=method,
            status=status,
            resource_id=resource_id,
            arn=f"arn:aws:bedrock-agentcore:us-west-2:111122223333:x/{resource_id}"
            if resource_id
            else None,
            version=version,
            spec=spec or {},
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


@pytest.fixture
def stub(monkeypatch):
    def install(pages: dict[str, list[dict]]) -> StubControl:
        control = StubControl(pages)
        monkeypatch.setattr(agents_router, "control_client", lambda _ws=None: control)
        return control

    return install


def _walk(value) -> set[str]:
    keys: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            keys.add(k)
            keys |= _walk(v)
    elif isinstance(value, list):
        for v in value:
            keys |= _walk(v)
    return keys


# ─── wrappers ────────────────────────────────────────────────────────────────
def test_runtime_list_wrappers_follow_every_page():
    control = StubControl(
        {
            "list_agent_runtime_versions": [
                {"agentRuntimes": [_runtime_version("1")], "nextToken": "p2"},
                {"agentRuntimes": [_runtime_version("2")]},
            ],
            "list_agent_runtime_endpoints": [
                {"runtimeEndpoints": [_runtime_endpoint("DEFAULT", "2")], "nextToken": "e2"},
                {"runtimeEndpoints": [_runtime_endpoint("stable", "1")]},
            ],
        }
    )
    versions = runtime_api.list_runtime_versions(control, RUNTIME_ID)
    endpoints = runtime_api.list_runtime_endpoints(control, RUNTIME_ID)
    assert [v["agentRuntimeVersion"] for v in versions] == ["1", "2"]
    assert [e["name"] for e in endpoints] == ["DEFAULT", "stable"]
    assert control.calls[0] == (
        "list_agent_runtime_versions",
        {"agentRuntimeId": RUNTIME_ID, "maxResults": 100},
    )
    assert control.calls[1][1]["nextToken"] == "p2"
    assert control.calls[2] == (
        "list_agent_runtime_endpoints",
        {"agentRuntimeId": RUNTIME_ID, "maxResults": 100},
    )
    assert control.calls[3][1]["nextToken"] == "e2"


def test_harness_list_wrappers_follow_every_page():
    control = StubControl(
        {
            "list_harness_versions": [
                {"harnessVersions": [{"harnessVersion": "1"}], "nextToken": "p2"},
                {"harnessVersions": [{"harnessVersion": "2"}]},
            ],
            "list_harness_endpoints": [
                {"endpoints": [{"endpointName": "DEFAULT"}], "nextToken": "e2"},
                {"endpoints": [{"endpointName": "custom"}]},
            ],
        }
    )
    assert [
        v["harnessVersion"] for v in harness_api.list_harness_versions(control, HARNESS_ID)
    ] == [
        "1",
        "2",
    ]
    assert [e["endpointName"] for e in harness_api.list_harness_endpoints(control, HARNESS_ID)] == [
        "DEFAULT",
        "custom",
    ]
    assert control.calls[0] == (
        "list_harness_versions",
        {"harnessId": HARNESS_ID, "maxResults": 100},
    )
    assert control.calls[1][1]["nextToken"] == "p2"
    assert control.calls[2] == (
        "list_harness_endpoints",
        {"harnessId": HARNESS_ID, "maxResults": 100},
    )
    assert control.calls[3][1]["nextToken"] == "e2"


# ─── route ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("method", ["zip_runtime", "studio", "container"])
def test_runtime_agent_versions_projects_allow_listed_shape(client, stub, method):
    control = stub(
        {
            "list_agent_runtime_versions": [
                {"agentRuntimes": [_runtime_version("1")], "nextToken": "p2"},
                {"agentRuntimes": [_runtime_version("3"), _runtime_version("2")]},
            ],
            "list_agent_runtime_endpoints": [
                {
                    "runtimeEndpoints": [
                        _runtime_endpoint("treatment", "3", status="CREATING"),
                        _runtime_endpoint("DEFAULT", "3"),
                    ],
                    "nextToken": "e2",
                },
                {
                    "runtimeEndpoints": [
                        _runtime_endpoint(
                            "stable", "2", "3", status="UPDATE_FAILED", failureReason="boom"
                        )
                    ]
                },
            ],
        }
    )
    agent_id = _seed(method=method, resource_id=RUNTIME_ID, version="2")

    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 200, res.text
    body = res.json()

    # both runtime list ops, on the runtime id, every page followed
    assert control.ops() == [
        "list_agent_runtime_versions",
        "list_agent_runtime_versions",
        "list_agent_runtime_endpoints",
        "list_agent_runtime_endpoints",
    ]
    assert all(kw["agentRuntimeId"] == RUNTIME_ID for _, kw in control.calls)

    assert body["kind"] == "runtime"
    assert body["resource_id"] == RUNTIME_ID
    assert body["ledger_version"] == "2"
    assert body["latest_version"] == "3"  # newest first, numerically
    assert [v["version"] for v in body["versions"]] == ["3", "2", "1"]
    assert body["versions"][0] == {
        "version": "3",
        "status": "READY",
        "description": "v3",
        "last_updated_at": "2026-09-01T12:03:00+00:00",
    }
    # DEFAULT first, then named endpoints alphabetically; canary names flagged
    assert [e["name"] for e in body["endpoints"]] == ["DEFAULT", "stable", "treatment"]
    assert body["canary_endpoints"] == ["stable", "treatment"]
    stable = body["endpoints"][1]
    assert stable == {
        "name": "stable",
        "live_version": "2",
        "target_version": "3",
        "status": "UPDATE_FAILED",
        "description": None,
        "created_at": "2026-08-01T00:00:00+00:00",
        "last_updated_at": "2026-09-01T00:00:00+00:00",
        "failure_reason": "boom",
    }
    # allow-list: nothing sensitive from the AWS summaries survives
    assert not (_walk(body) & FORBIDDEN_KEYS)
    assert "do-not-leak" not in res.text and "private-role" not in res.text


def test_harness_agent_uses_harness_ops(client, stub):
    control = stub(
        {
            "list_harness_versions": [
                {
                    "harnessVersions": [
                        {
                            "harnessId": HARNESS_ID,
                            "harnessName": "hr",
                            "arn": "arn:aws:bedrock-agentcore:us-west-2:1:harness/x",
                            "harnessVersion": "2",
                            "status": "READY",
                            "createdAt": datetime(2026, 8, 2, tzinfo=UTC),
                            "updatedAt": datetime(2026, 8, 3, tzinfo=UTC),
                        },
                        {"harnessVersion": "1", "status": "READY"},
                    ]
                }
            ],
            "list_harness_endpoints": [
                {
                    "endpoints": [
                        {
                            "harnessId": HARNESS_ID,
                            "endpointName": "DEFAULT",
                            "arn": "arn:aws:bedrock-agentcore:us-west-2:1:harness/x/ep",
                            "status": "READY",
                            "liveVersion": "2",
                            "targetVersion": "2",
                            "createdAt": datetime(2026, 8, 2, tzinfo=UTC),
                            "updatedAt": datetime(2026, 8, 3, tzinfo=UTC),
                        }
                    ]
                }
            ],
        }
    )
    agent_id = _seed(method="harness", resource_id=HARNESS_ID, version="2")

    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 200, res.text
    body = res.json()
    assert control.ops() == ["list_harness_versions", "list_harness_endpoints"]
    assert all(kw["harnessId"] == HARNESS_ID for _, kw in control.calls)
    assert body["kind"] == "harness"
    assert body["latest_version"] == "2" and body["ledger_version"] == "2"
    # HarnessVersionSummary has no description and uses updatedAt
    assert body["versions"][0] == {
        "version": "2",
        "status": "READY",
        "description": None,
        "last_updated_at": "2026-08-03T00:00:00+00:00",
    }
    assert body["endpoints"][0]["name"] == "DEFAULT"
    assert body["endpoints"][0]["last_updated_at"] == "2026-08-03T00:00:00+00:00"
    assert body["canary_endpoints"] == []
    assert not (_walk(body) & FORBIDDEN_KEYS)


def test_discovered_harness_row_uses_harness_ops(client, stub):
    control = stub(
        {
            "list_harness_versions": [{"harnessVersions": [{"harnessVersion": "5"}]}],
            "list_harness_endpoints": [{"endpoints": []}],
        }
    )
    agent_id = _seed(
        method="discovered_runtime",
        resource_id=HARNESS_ID,
        version="4",
        spec={"discovery": {"resource_type": "harness", "aws_status": "READY"}},
    )
    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 200, res.text
    body = res.json()
    assert control.ops() == ["list_harness_versions", "list_harness_endpoints"]
    assert body["kind"] == "harness"
    # ledger vs AWS latest mismatch is reported, not an error
    assert body["ledger_version"] == "4" and body["latest_version"] == "5"
    assert body["endpoints"] == []


def test_discovered_runtime_row_defaults_to_runtime_ops(client, stub):
    control = stub(
        {
            "list_agent_runtime_versions": [{"agentRuntimes": []}],
            "list_agent_runtime_endpoints": [{"runtimeEndpoints": []}],
        }
    )
    agent_id = _seed(
        method="discovered_runtime",
        resource_id=RUNTIME_ID,
        version=None,
        spec={"discovery": {"aws_status": "READY"}},  # resource_type absent ⇒ runtime
    )
    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 200, res.text
    body = res.json()
    assert control.ops() == ["list_agent_runtime_versions", "list_agent_runtime_endpoints"]
    assert body == {
        "kind": "runtime",
        "resource_id": RUNTIME_ID,
        "versions": [],
        "endpoints": [],
        "latest_version": None,
        "ledger_version": None,
        "canary_endpoints": [],
    }


@pytest.mark.parametrize(
    ("method", "status", "resource_id", "spec"),
    [
        ("zip_runtime", "deploying", None, {}),
        ("harness", "failed", None, {}),
        ("harness", "deleted", HARNESS_ID, {}),
        ("discovered_runtime", "active", "x", {"discovery": {"resource_type": "mcp"}}),
    ],
)
def test_agent_without_resource_is_409_no_resource(client, stub, method, status, resource_id, spec):
    control = stub({})
    agent_id = _seed(method=method, status=status, resource_id=resource_id, spec=spec)
    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "agent.no_resource"
    assert body["message"]  # a human reason the UI can show
    assert control.calls == []  # never asked AWS


def test_agent_from_another_workspace_is_404(client, stub):
    control = stub({})
    agent_id = _seed(method="zip_runtime", resource_id=RUNTIME_ID, workspace_id="acct-other")
    res = client.get(f"/api/agents/{agent_id}/versions")
    assert res.status_code == 404
    assert res.json()["code"] == "agent.not_found"
    assert control.calls == []


def test_unknown_agent_is_404(client, stub):
    stub({})
    res = client.get("/api/agents/nope/versions")
    assert res.status_code == 404
    assert res.json()["code"] == "agent.not_found"
