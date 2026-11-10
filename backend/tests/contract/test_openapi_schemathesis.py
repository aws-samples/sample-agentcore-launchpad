"""Property-based API contract run over the console's OpenAPI (quality pack Q6, N12-obs58).

Schemathesis generates requests from ``GET /api/openapi.json`` and calls the app
in-process (ASGI — no server, no sockets, no AWS: the hermetic ``conftest`` stubs and a
refused ``socket.connect`` make any upstream call fail fast). Authenticated as the
administrator the open console grants (``LAUNCHPAD_ALLOW_OPEN_CONSOLE``), stateless, with a
modest example budget so the file finishes well inside the gate's budget.

Scope — a tag / path allowlist, never the whole surface:

* every ``GET`` (reads are side-effect free) except the routes that only exist against a
  live AWS account or a running sub-process (``EXCLUDED_PATHS``: they answer a structured
  502 / 503 here and are covered by the ``e2e_*.py`` scripts);
* the pure ``POST`` evaluators (``/api/policy-engines/{id}/validate`` / ``/format``) — they write
  nothing;
* no ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` otherwise: those create billable resources,
  deploy, or destroy (the permission matrix covers their authorization).

Checks (all stateless):

* ``not_a_server_error`` — no 5xx at all;
* ``success_schema_conformance`` — a 2xx body matches the declared response schema (the
  stock check minus 4xx: FastAPI's generated ``HTTPValidationError`` schema for ``422`` does
  not describe the envelope the app actually returns — see the finding in N12-obs58);
* ``error_envelope`` (project-specific, replaces the stock ``status_code_conformance``):
  FastAPI documents only the success status and ``422``, while the project's error contract
  (``core/errors.py``) is that EVERY 4xx / 5xx body is the ``{code, message, detail}``
  envelope with a dotted code — so an undeclared ``404`` with the envelope conforms and a
  bare Starlette ``{"detail": ...}`` does not.
"""

from __future__ import annotations

import re
import socket

import pytest
import schemathesis
from hypothesis import HealthCheck, Phase, settings
from schemathesis import Case
from schemathesis.checks import CheckContext, not_a_server_error, response_schema_conformance
from schemathesis.core.failures import Failure

from app.main import create_app
from app.services import knowledge_backend, memory_backend, policy_backend, registry_backend
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from app.services.policy_cedar import CedarEngine
from app.services.registry_local import LocalRegistryBackend

MAX_EXAMPLES = 8  # per operation — ~180 operations → well under a minute in-process

# GET routes that only exist against a live account / running process. Each answers a
# structured 502 / 503 here (or needs a real sub-process); they are e2e territory.
EXCLUDED_PATHS: tuple[str, ...] = (
    "/api/agents/discovery",  # ListAgentRuntimes
    "/api/agents/uploads/{upload_id}",  # S3 HEAD on a BYOC staging prefix
    "/api/agents/{agent_id}/identity",  # GetWorkloadIdentity
    "/api/agents/{agent_id}/versions",  # ListAgentRuntimeVersions
    "/api/demos/browser/options",  # AgentCore browser catalogue
    "/api/eval/datasets/cloud",  # the S3 snapshot store
    "/api/eval/datasets/cloud/{cloud_id}",
    "/api/eval/log-groups",  # CloudWatch Logs
    "/api/eval/log-sessions",
    "/api/eval/log-services",
    "/api/eval/agents/{agent_id}/log-streams",
    "/api/generate-code/status",  # the studio sub-process
    "/api/governance/gateways",  # ListGateways
    "/api/governance/gateways/{gateway_id}",
    "/api/governance/gateways/{gateway_id}/rate-limits",
    "/api/identity/connections",  # Identity token vault
    "/api/identity/connections/oidc-sources",  # ListOauth2CredentialProviders
    "/api/identity/connections/{kind}/{name}",
    "/api/identity/consent-portal",
    "/api/identity/gateway-targets",
    "/api/identity/grants/{connection}/status",
    "/api/memory/actors",  # AgentCore Memory data plane
    "/api/memory/events",
    "/api/memory/extraction-jobs",
    "/api/memory/namespaces",
    "/api/memory/overview",
    "/api/memory/records",
    "/api/memory/resources",
    "/api/memory/resources/{memory_id}",
    "/api/memory/sessions",
    "/api/observability/dashboard",  # CloudWatch / X-Ray
    "/api/observability/sessions",
    "/api/observability/sessions/{session_id}",
    "/api/observability/sessions/{session_id}/transcript",
    "/api/observability/traces",
    "/api/observability/traces/{trace_id}",
    "/api/traces/{session_id}",
    "/api/registry/attachables",  # Gateway catalogue (ListGateways)
    "/api/registry/import-gateway/preview",  # Gateway ListGatewayTargets
    "/api/registry/records/discoverable",  # the data-plane ListDiscoverableRegistryRecords
    "/api/registry/records/{record_id}/live-agent-card",  # GetAgentCard on the runtime
    "/api/registry/skills/capabilities",  # probes the host for git / uv
    "/api/skill-lab/jobs/{job_id}/artifacts/raw",  # S3 object body
    "/api/tools",  # Gateway MCP listing
    "/api/workspaces/hub-identity",  # STS GetCallerIdentity
    "/api/workspaces/{workspace_id}/bootstrap",  # CloudFormation describe
    "/api/settings/model-providers/{provider_id}",  # SSM-backed credential state
)
SAFE_POSTS: tuple[str, ...] = ("/api/policy-engines/{engine_id}/validate",
                               "/api/policy-engines/{engine_id}/format")

_CODE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")


def _in_scope(ctx) -> bool:
    operation = ctx.operation
    if operation.path.startswith("/api/experiments"):
        return False  # configuration experiments are being removed (cn/r13-b); canaries stay
    if operation.method.upper() == "GET":
        return operation.path not in EXCLUDED_PATHS and not operation.path.startswith("/v1")
    return operation.method.upper() == "POST" and operation.path in SAFE_POSTS


@schemathesis.check
def error_envelope(ctx: CheckContext, response, case: Case) -> None:
    """Every non-2xx answer is the project's ``{code, message, detail}`` envelope."""
    if response.status_code < 400:
        return
    try:
        body = response.json()
    except ValueError as exc:
        raise Failure(operation=case.operation.label,
                      title="error body is not JSON",
                      message=f"{response.status_code} without a JSON envelope") from exc
    code = body.get("code") if isinstance(body, dict) else None
    if not (isinstance(body, dict) and {"code", "message", "detail"} <= set(body)
            and isinstance(code, str) and _CODE.match(code)):
        raise Failure(operation=case.operation.label,
                      title="error body is not the launchpad envelope",
                      message=f"{response.status_code}: {str(body)[:300]}")


@pytest.fixture(autouse=True)
def _backends_as_create_app_registers_them():
    """The root conftest empties every feature-backend slot before each test; the app
    under contract has them registered (``app/main.py``), so put back the same
    ledger-backed implementations — without a substrate they answer like production
    answers on a workspace that is not provisioned yet."""
    knowledge_backend.set_backend(SelfHostedKnowledgeBackend())
    memory_backend.set_backend(SelfHostedMemoryBackend())
    registry_backend.set_backend(LocalRegistryBackend())
    policy_backend.set_backend(CedarEngine())
    yield


@schemathesis.check
def success_schema_conformance(ctx: CheckContext, response, case: Case) -> None:
    """``response_schema_conformance`` for 2xx / 3xx only: FastAPI's generated ``422``
    schema (``HTTPValidationError``) does not describe the launchpad envelope, so 4xx
    bodies are judged by ``error_envelope`` instead (finding N12-obs58)."""
    if response.status_code < 400:
        response_schema_conformance(ctx, response, case)


@pytest.fixture(scope="module", autouse=True)
def _hermetic():
    """No AWS client and no socket during the run: an operation that needs one fails at
    once with a clear message (add it to ``EXCLUDED_PATHS``) instead of waiting out
    botocore's connection retries."""
    from app.services import aws_clients

    def no_client(*args, **kwargs):
        raise AssertionError(
            "AWS client requested during the contract run — this operation needs a live "
            f"account; add its path to EXCLUDED_PATHS: {args[:2]}"
        )

    def refuse(self, *args, **kwargs):
        raise OSError(f"network connect attempted during the contract run: {args}")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(aws_clients, "client", no_client)
        mp.setattr(aws_clients, "get_session", no_client)
        mp.setattr(socket.socket, "connect", refuse)
        yield


schema = schemathesis.openapi.from_asgi("/api/openapi.json", create_app()).include(_in_scope)


@schema.parametrize()
@settings(
    max_examples=MAX_EXAMPLES, deadline=None, database=None, derandomize=True,
    phases=[Phase.explicit, Phase.generate],
    suppress_health_check=list(HealthCheck),
)
def test_api_contract(case: Case) -> None:
    case.call_and_validate(
        checks=[not_a_server_error, success_schema_conformance, error_envelope],
    )


def test_the_scope_is_what_the_doc_says() -> None:
    """``docs/cn/interfaces/testing.md`` quotes the shape of the run; keep them in step."""
    operations = list(schema.get_all_operations())
    methods = {op.ok().method.upper() for op in operations}
    assert methods == {"GET", "POST"}
    posts = {op.ok().path for op in operations if op.ok().method.upper() == "POST"}
    assert posts == set(SAFE_POSTS)
    assert not any(op.ok().path in EXCLUDED_PATHS for op in operations)
    assert not any(op.ok().path.startswith("/api/experiments") for op in operations)
    assert len(operations) >= 80
