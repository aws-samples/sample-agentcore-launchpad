"""An in-memory COA deployment + workspace vault for the context-layer tests.

Every boto3 client the code under test builds goes through
``aws_clients.client(service, ctx)``; ``install`` swaps that for fakes keyed on
``(service, region)``, so a test can assert which region each call landed in
(COA metadata in the COA region, the token vault in the workspace region).
"""

import base64
import json
from typing import Any

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
CLIENT_SECRET = "s3cr3t-agent-client-value"  # never allowed to leave the fake
RUNTIME_BASE = f"arn:aws:bedrock-agentcore:{COA_REGION}:{ACCOUNT}:runtime"
MCP_ARN = f"{RUNTIME_BASE}/coa_w2_dev_mcp_server-1cRyvG5BcK"
SERVE_ARN = f"{RUNTIME_BASE}/coa_w2_dev_context_manager-Z8HDyj4suT"
API_ENDPOINT = f"https://r5shmv8yab.execute-api.{COA_REGION}.amazonaws.com/prod/"
CONSOLE_URL = "https://d3q37s5vts6pte.cloudfront.net"
HOSTED_UI = f"https://coa-w2-dev-auth-{ACCOUNT}.auth.{COA_REGION}.amazoncognito.com"
CALLBACK = "https://bedrock-agentcore.us-west-2.amazonaws.com/identities/oauth2/callback/abc"
NAMESPACE = "dc09046b-28af-4c7b-a03a-4fa63cda243b"


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
    def __init__(self, params: dict[str, str]):
        super().__init__()
        self.params = params

    def get_parameters(self, Names):
        self.calls.append(("get_parameters", {"Names": Names}))
        return {"Parameters": [
            {"Name": n, "Value": self.params[n]} for n in Names if n in self.params
        ]}


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
    from app.services import context_layer

    context_layer._CALLBACK_CACHE.clear()
    return account


def connect_default(account: Account, connection: str | None = None) -> None:
    """Connect the default workspace to the fake deployment (no HTTP)."""
    from app.core.db import SessionLocal
    from app.services import context_layer
    from app.services.workspace import context_for_workspace

    db = SessionLocal()
    try:
        context_layer.connect(
            db, context_for_workspace("default"), prefix=PREFIX, env=ENV, region=COA_REGION,
            connection=connection, return_url="http://localhost:5173/auth/return",
        )
    finally:
        db.close()
