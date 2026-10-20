"""An in-memory COA deployment + workspace vault for the context-layer tests.

Every boto3 client the code under test builds goes through
``aws_clients.client(service, ctx)``; ``install`` swaps that for fakes keyed on
``(service, region)``, so a test can assert which region each call landed in
(COA metadata in the COA region, the token vault in the workspace region).
"""

import base64
import json
from typing import Any

import httpx

from app.services import aws_clients
from app.services import context_layer_tokens as tokens

from .conftest import client_error

ACCOUNT = "111122223333"
WS_REGION = "us-west-2"
COA_REGION = "us-east-2"
PREFIX = "coa-w2"
ENV = "dev"
POOL = f"{COA_REGION}_PoolAbc12"
ISSUER = f"https://cognito-idp.{COA_REGION}.amazonaws.com/{POOL}"
AGENT_CLIENT = "agentclient123"
WEB_CLIENT = "webclient456"
CLIENT_SECRET = "s3cr3t-agent-client-value"  # never allowed to leave the fake
RUNTIME_BASE = f"arn:aws:bedrock-agentcore:{COA_REGION}:{ACCOUNT}:runtime"
MCP_ARN = f"{RUNTIME_BASE}/coa_w2_dev_mcp_server-1cRyvG5BcK"
SERVE_ARN = f"{RUNTIME_BASE}/coa_w2_dev_context_manager-Z8HDyj4suT"
API_ENDPOINT = f"https://r5shmv8yab.execute-api.{COA_REGION}.amazonaws.com/prod/"
CONSOLE_URL = "https://d3q37s5vts6pte.cloudfront.net"
HOSTED_UI = f"https://coa-w2-dev-auth-{ACCOUNT}.auth.{COA_REGION}.amazoncognito.com"
CALLBACK = "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/abc"
NAMESPACE = "dc09046b-28af-4c7b-a03a-4fa63cda243b"

# the shared COA demo account (an SSM SecureString under /<prefix>/launchpad/)
DEMO_PARAM = f"/{PREFIX}/launchpad/demo-account"
DEMO_USER = "launchpad-demo@coa-demo.example.com"
DEMO_SUB = "5f0c7a3e-1111-4c2b-9d8e-0a1b2c3d4e5f"
DEMO_PASSWORD = "Xq7-not-a-real-pass-Zr4!"  # never allowed outside the reveal response


def jwt(exp: float, **claims: Any) -> str:
    """An unsigned JWT-shaped access token (the broker only decodes ``exp``)."""
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': int(exp), **claims})}.sig"


class Calls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def ops(self, name: str) -> list[dict]:
        return [kw for op, kw in self.calls if op == name]


class FakeSSM(Calls):
    """``params`` are String parameters, ``secure`` SecureString ones (decrypted
    values; ``WithDecryption=False`` reads ciphertext, as SSM does)."""

    def __init__(self, params: dict[str, str]):
        super().__init__()
        self.params = params
        self.secure: dict[str, str] = {}

    def get_parameters(self, Names):
        self.calls.append(("get_parameters", {"Names": Names}))
        return {"Parameters": [
            {"Name": n, "Type": "String", "Value": self.params[n]}
            for n in Names if n in self.params
        ]}

    def get_parameter(self, Name, WithDecryption=False):
        self.calls.append(("get_parameter", {"Name": Name, "WithDecryption": WithDecryption}))
        if Name in self.secure:
            value = self.secure[Name] if WithDecryption else "AQICAHencrypted=="
            return {"Parameter": {"Name": Name, "Type": "SecureString", "Value": value}}
        if Name in self.params:
            return {"Parameter": {"Name": Name, "Type": "String", "Value": self.params[Name]}}
        raise client_error("ParameterNotFound", "Parameter not found", "GetParameter")

    def put_demo_account(self, *, name: str = DEMO_PARAM, sub: str = DEMO_SUB) -> None:
        self.secure[name] = json.dumps(
            {"username": DEMO_USER, "password": DEMO_PASSWORD, "sub": sub}
        )


class FakeCfn(Calls):
    def __init__(self, outputs: dict[str, dict[str, str]]):
        super().__init__()
        self.outputs = outputs

    def describe_stacks(self, StackName):
        self.calls.append(("describe_stacks", {"StackName": StackName}))
        if StackName not in self.outputs:
            raise client_error("ValidationError", f"Stack with id {StackName} does not exist")
        return {"Stacks": [{"Outputs": [
            {"OutputKey": k, "OutputValue": v} for k, v in self.outputs[StackName].items()
        ]}]}


class FakeCognito(Calls):
    def __init__(self, callbacks: list[str]):
        super().__init__()
        self.callbacks = callbacks

    def describe_user_pool_client(self, UserPoolId, ClientId):
        self.calls.append(("describe_user_pool_client", {"UserPoolId": UserPoolId}))
        return {"UserPoolClient": {
            "ClientId": ClientId, "ClientSecret": CLIENT_SECRET,
            "CallbackURLs": list(self.callbacks),
        }}


class FakeControl(Calls):
    """bedrock-agentcore-control: runtimes (COA region) + vault/workload (workspace)."""

    def __init__(self) -> None:
        super().__init__()
        self.runtime_status = {"coa_w2_dev_mcp_server-1cRyvG5BcK": "READY",
                               "coa_w2_dev_context_manager-Z8HDyj4suT": "READY"}
        self.providers: dict[str, dict] = {}
        self.workloads: dict[str, list[str]] = {}
        self.harness: dict[str, Any] = {"tools": [], "allowedTools": []}

    def get_agent_runtime(self, agentRuntimeId):
        self.calls.append(("get_agent_runtime", {"agentRuntimeId": agentRuntimeId}))
        return {"status": self.runtime_status[agentRuntimeId],
                "environmentVariables": {"NEVER": "shown"}}

    def list_oauth2_credential_providers(self, **kwargs):
        return {"credentialProviders": [{"name": n} for n in self.providers]}

    def list_api_key_credential_providers(self, **kwargs):
        return {"credentialProviders": []}

    def get_oauth2_credential_provider(self, name):
        if name not in self.providers:
            raise client_error("ResourceNotFoundException")
        return self.providers[name]

    def create_oauth2_credential_provider(self, **kwargs):
        self.calls.append(("create_oauth2_credential_provider", kwargs))
        name = kwargs["name"]
        config = kwargs["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
        self.providers[name] = {
            "name": name,
            "credentialProviderArn": f"arn:provider/{name}",
            "callbackUrl": CALLBACK,
            "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": {
                "clientId": config["clientId"], "oauthDiscovery": config["oauthDiscovery"],
            }},
        }
        return {"credentialProviderArn": f"arn:provider/{name}", "callbackUrl": CALLBACK}

    def get_workload_identity(self, name):
        if name not in self.workloads:
            raise client_error("ResourceNotFoundException")
        return {"name": name, "allowedResourceOauth2ReturnUrls": list(self.workloads[name])}

    def create_workload_identity(self, **kwargs):
        self.calls.append(("create_workload_identity", kwargs))
        self.workloads[kwargs["name"]] = list(kwargs.get("allowedResourceOauth2ReturnUrls") or [])
        return {"name": kwargs["name"]}

    def update_workload_identity(self, **kwargs):
        self.calls.append(("update_workload_identity", kwargs))
        self.workloads[kwargs["name"]] = list(kwargs["allowedResourceOauth2ReturnUrls"])
        return {"name": kwargs["name"]}

    def get_harness(self, harnessId, **kwargs):
        self.calls.append(("get_harness", {"harnessId": harnessId}))
        return {"harness": self.harness}


class FakeData(Calls):
    """bedrock-agentcore data plane: the token vault + InvokeHarness."""

    def __init__(self) -> None:
        super().__init__()
        self.token: str | None = None  # the vaulted COA token, None = consent needed
        self.stream: list[dict] = [
            {"contentBlockDelta": {"delta": {"text": "5 days"}}},
            {"messageStop": {"stopReason": "end_turn"}},
        ]

    def get_workload_access_token_for_user_id(self, **kwargs):
        self.calls.append(("get_workload_access_token_for_user_id", kwargs))
        return {"workloadAccessToken": "wat-" + kwargs["userId"]}

    def get_resource_oauth2_token(self, **kwargs):
        self.calls.append(("get_resource_oauth2_token", kwargs))
        if self.token and not kwargs.get("forceAuthentication"):
            return {"accessToken": self.token}
        return {"authorizationUrl": "https://coa-auth.example/authorize?state=x",
                "sessionUri": "urn:session:abc"}

    def complete_resource_token_auth(self, **kwargs):
        self.calls.append(("complete_resource_token_auth", kwargs))
        return {}

    def invoke_harness(self, **kwargs):
        self.calls.append(("invoke_harness", kwargs))
        return {"stream": list(self.stream)}


class Account:
    """One fake AWS account: a COA deployment in COA_REGION, a vault in WS_REGION."""

    def __init__(self, *, agent_oauth: bool = True, callbacks: list[str] | None = None):
        params = {
            f"/{PREFIX}/mcp/runtime-arn": MCP_ARN,
            f"/{PREFIX}/serve/runtime-arn": SERVE_ARN,
            f"/{PREFIX}/userpool-id": POOL,
            f"/{PREFIX}/issuer": ISSUER,
            f"/{PREFIX}/userpool-client-id": WEB_CLIENT,
        }
        if agent_oauth:
            params[f"/{PREFIX}/agent-oauth-client-id"] = AGENT_CLIENT
        self.ssm = FakeSSM(params)
        self.cfn = FakeCfn({
            f"{PREFIX}-{ENV}-api": {"ApiEndpoint": API_ENDPOINT},
            f"{PREFIX}-{ENV}-web": {"PublicUIWebsiteURL0A1B2C3D": CONSOLE_URL},
            f"{PREFIX}-{ENV}-auth": {"CognitoDomainUrl": HOSTED_UI},
        })
        self.cognito = FakeCognito([CALLBACK] if callbacks is None else callbacks)
        self.control = FakeControl()
        self.data = FakeData()
        self.regions: list[tuple[str, str]] = []

    def client(self, service: str, ctx: Any, cache_token: str | None = None, **cfg: Any) -> Any:
        self.regions.append((service, ctx.region))
        if service == "ssm":
            return self.ssm
        if service == "cloudformation":
            return self.cfn
        if service == "cognito-idp":
            return self.cognito
        if service == "bedrock-agentcore-control":
            return self.control
        if service == "bedrock-agentcore":
            return self.data
        raise AssertionError(f"unexpected client {service}")

    def regions_of(self, service: str) -> set[str]:
        return {region for svc, region in self.regions if svc == service}


def install(monkeypatch, account: Account | None = None) -> Account:
    account = account or Account()
    monkeypatch.setattr(aws_clients, "client", account.client)
    tokens.forget("default")
    with tokens._LOCK:
        tokens._CACHE.clear()
        tokens._FORCE.clear()
    from app.services import context_layer, context_layer_demo

    context_layer._CALLBACK_CACHE.clear()
    with context_layer_demo._LOCK:
        context_layer_demo._CACHE.clear()
    return account


def connect_default(
    account: Account, connection: str | None = None, demo_account_param: str | None = None
) -> None:
    """Connect the default workspace to the fake deployment (no HTTP)."""
    from app.core.db import SessionLocal
    from app.services import context_layer
    from app.services.workspace import context_for_workspace

    db = SessionLocal()
    try:
        context_layer.connect(
            db, context_for_workspace("default"), prefix=PREFIX, env=ENV, region=COA_REGION,
            connection=connection, return_url="http://localhost:5173/auth/return",
            demo_account_param=demo_account_param,
        )
    finally:
        db.close()


# ── COA REST + Serve (httpx) ─────────────────────────────────────────────────
# Shapes trimmed from the 2026-10-19 live probes of coa-w2-dev ("AnyCompany HR"),
# reduced to what the projections read plus a few fields they must drop. No token,
# user id or secret from the live run is recorded here.

SOURCE_DOCS = "src-docs-1a2b"
SOURCE_GLUE = "src-glue-3c4d"
ONTOLOGY_INDUCED = "https://coa.example/ontology/anycompany-hr#"
ONTOLOGY_BASE = "https://coa.example/ontology/foundation#"

NAMESPACE_DETAIL = {"namespace": {
    "namespaceId": NAMESPACE, "name": "anycompany-hr", "displayName": "AnyCompany HR",
    "description": "HR policies and leave data", "owner": "owner@example.com",
    "status": "ACTIVE", "sourceCount": 2, "createdAt": "2026-10-10T08:00:00Z",
    "updatedAt": "2026-10-18T09:30:00Z",
    "athenaWorkgroupName": "coa-w2-dev-dc09046b",  # system-managed: not projected
    "externalId": "never-projected",
}}
SOURCES_PAGES = [
    {"items": [{
        "sourceId": SOURCE_DOCS, "namespaceId": NAMESPACE, "name": "HR policy documents",
        "sourceType": "DOCUMENTS", "sourceSubType": "S3_DOCUMENTS", "status": "COMPLETED",
        "createdAt": 1760083200, "updatedAt": 1760774400.0,
        "configuration": {"bucket": "never-projected"},
    }], "nextToken": "page-2"},
    {"items": [{
        "sourceId": SOURCE_GLUE, "namespaceId": NAMESPACE, "name": "anyhr_glue",
        "sourceType": "DATABASE", "sourceSubType": "GLUE_DATABASE", "status": "APPROVED",
        "createdAt": 1760083200, "tablesDiscovered": 3, "status_detail": "x",
    }]},
]
METRICS_PAGE = {"metrics": [{
    "metricId": "m-1", "namespaceId": NAMESPACE, "name": "available_annual_leave_days",
    "description": "Annual leave still bookable: remaining days minus pending requests.",
    "expression": {"dialects": [
        {"dialect": "ATHENA", "expression": "SUM(remaining_days) - SUM(pending_days)"},
        {"dialect": "", "expression": "dropped: no dialect"},
    ]},
    "dataSourceId": SOURCE_GLUE, "sourceTable": "leave_balances", "unit": "days",
    "returnType": "decimal",
    "aiContext": {"synonyms": ["可用年假", "leave left"], "instructions": "not projected"},
    "definedBy": "owner@example.com",
}]}
ONTOLOGIES = {"ontologies": [
    {"ontologyId": ONTOLOGY_BASE, "uri": ONTOLOGY_BASE, "title": "Foundation",
     "ontologyType": "foundational", "classCount": 2, "graphUri": "urn:graph:never"},
    {"ontologyId": ONTOLOGY_INDUCED, "uri": ONTOLOGY_INDUCED, "title": "AnyCompany HR",
     "ontologyType": "induced", "parseStatus": "ok", "classCount": 2, "propertyCount": 9,
     "updatedAt": "2026-10-18T09:30:00Z"},
]}
OVERVIEWS = {
    ONTOLOGY_INDUCED: {
        "ontology_id": ONTOLOGY_INDUCED, "namespace": NAMESPACE,
        "classes": [
            {"uri": f"{ONTOLOGY_INDUCED}employees", "label": "employees",
             "comment": "People employed by AnyCompany.", "subClassOf": []},
            {"uri": f"{ONTOLOGY_INDUCED}leave_balances", "label": "leave_balances",
             "comment": "Remaining and pending leave per employee."},
        ],
        "objectProperties": [{"uri": f"{ONTOLOGY_INDUCED}hasBalance"}],
        "totalClasses": 2,
    },
    ONTOLOGY_BASE: {
        "ontologyId": ONTOLOGY_BASE,
        "classes": [{"uri": f"{ONTOLOGY_BASE}Person"}],
        "totalClasses": 1,
    },
}
SYSTEM_HEALTH = {
    "status": "degraded", "dynamodb": "ok", "neptuneKg": "ok",
    "opensearch": "error: collection unreachable",
    "ontologyInduction": {"graphStore": "ok", "vectorStore": "ok",
                          "bedrockEmbeddings": "ok", "descriptionLlm": "degraded"},
    "internal": "never-projected",
}

COA_SESSION = "7c1e2a90-5b6d-4f3e-9a8b-0c1d2e3f4a5b"
SERVE_FRAMES = [
    {"type": "step", "requestId": "r-1", "timestamp": "2026-10-19T10:00:00Z",
     "sessionId": COA_SESSION,
     "payload": {"stepName": "T1_METRIC_MATCH", "status": "skipped", "durationMs": 120}},
    {"type": "step", "requestId": "r-1", "timestamp": "2026-10-19T10:00:01Z",
     "sessionId": COA_SESSION,
     "payload": {"stepName": "T2_NL_TO_SQL", "status": "success", "durationMs": 9800,
                 "toolUsed": "athena", "detail": {"table": "employees"}}},
    {"type": "token", "requestId": "r-1", "timestamp": "2026-10-19T10:00:02Z",
     "sessionId": COA_SESSION, "payload": {"text": "各部门员工数如下。"}},
    {"type": "done", "requestId": "r-1", "timestamp": "2026-10-19T10:00:03Z",
     "sessionId": COA_SESSION, "payload": {"result": {
         "tier": 2, "confidence": {"score": 0.92, "rationale": "governed table"},
         "resultRows": [
             {"department": "Engineering", "employee_count": 41},
             {"department": "Finance", "employee_count": 12},
             {"department": "HR", "employee_count": 9},
             {"department": "Marketing", "employee_count": 15},
             {"department": "Operations", "employee_count": 18},
             {"department": "Sales", "employee_count": 8},
         ],
         "synthesizedAnswer": "共 6 个部门，Engineering 人数最多（41 人）。",
         "queryUsed": "SELECT department, COUNT(*) AS employee_count FROM employees GROUP BY 1",
         "trace": [{"step": "T2_NL_TO_SQL", "status": "success", "durationMs": 9800}],
         "dataSources": ["anyhr_glue"], "partial": False,
         "metadata": {"totalMs": 17200, "principal": "never-projected"},
     }}},
]


def sse_body(frames: list[Any]) -> bytes:
    """COA frames as the Serve runtime streams them (``data: <json>`` + blank line)."""
    return "".join(
        f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames
    ).encode()


class CoaHttp:
    """COA's REST API and Serve runtime behind ``httpx.MockTransport``.

    ``overrides`` maps a path suffix (``"metrics"``, ``"system-health"``, …) or
    ``"serve"`` to an ``httpx.Response`` (or a callable building one); every request
    is recorded."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.overrides: dict[str, Any] = {}
        self.serve_frames: list[Any] = list(SERVE_FRAMES)

    def _override(self, key: str, request: httpx.Request) -> httpx.Response | None:
        hit = self.overrides.get(key)
        if hit is None:
            return None
        return hit(request) if callable(hit) else hit

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if "/invocations" in url:
            hit = self._override("serve", request)
            return hit or httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=sse_body(self.serve_frames),
            )
        path = request.url.path.split("/prod/", 1)[-1]
        ns = f"namespaces/{NAMESPACE}"
        key = path.removeprefix(ns).strip("/") or "namespace"
        if path == "namespaces":
            key = "namespaces"
        elif path == "system-health":
            key = "system-health"
        hit = self._override(key, request)
        if hit is not None:
            return hit
        if key == "namespaces":
            return httpx.Response(200, json={"namespaces": [NAMESPACE_DETAIL["namespace"]]})
        if not path.startswith(ns) and key != "system-health":
            return httpx.Response(404, json={"message": "Namespace not found"})
        if key == "namespace":
            return httpx.Response(200, json=NAMESPACE_DETAIL)
        if key == "sources":
            page = 1 if request.url.params.get("nextToken") == "page-2" else 0
            return httpx.Response(200, json=SOURCES_PAGES[page])
        if key == "metrics":
            return httpx.Response(200, json=METRICS_PAGE)
        if key == "ontologies":
            return httpx.Response(200, json=ONTOLOGIES)
        if key == "graph/ontology-overview":
            overview = OVERVIEWS.get(request.url.params.get("ontology_id") or "")
            if overview is None:
                return httpx.Response(400, json={"message": "Invalid ontology id"})
            return httpx.Response(200, json=overview)
        if key == "system-health":
            return httpx.Response(200, json=SYSTEM_HEALTH)
        return httpx.Response(404, json={"message": f"no route {path}"})

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def install_http(monkeypatch, coa: CoaHttp | None = None) -> CoaHttp:
    """Route the REST client (sync) and the playground (async) to ``coa``."""
    from app.services import context_layer_api, context_layer_playground

    coa = coa or CoaHttp()
    monkeypatch.setattr(context_layer_api, "_transport", lambda: httpx.MockTransport(coa.handler))
    monkeypatch.setattr(
        context_layer_playground, "_transport", lambda: httpx.MockTransport(coa.handler)
    )
    return coa
