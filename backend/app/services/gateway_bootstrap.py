"""Gateway bootstrap — extends the phase-2 singletons with the Build-Tools layer.

ensure_* functions are create-if-missing by name (idempotent), same contract
as ensure_registry / ensure_memory in bootstrap.py.
"""

import json
import time
from pathlib import Path
from typing import Any

import yaml

from app.core.config import REPO_ROOT
from app.core.naming import resource_name

# Base names; the `*_name()` functions below apply `Settings.env_name` (N11), so a
# parallel environment creates/looks up `launchpad-gw-t1` etc. Target names are
# scoped inside the gateway and need no suffix.
GATEWAY_NAME = "launchpad-gw"
HR_TARGET_NAME = "hr-database"
FACTS_TARGET_NAME = "office-facts"
API_KEY_PROVIDER_NAME = "launchpad-office-facts-key"


def gateway_name() -> str:
    return resource_name(GATEWAY_NAME)


def api_key_provider_name() -> str:
    return resource_name(API_KEY_PROVIDER_NAME)

SAMPLES = REPO_ROOT / "samples"


def ensure_gateway(
    control: Any,
    *,
    role_arn: str,
    region: str,
    name: str | None = None,
    timeout_s: int = 300,
) -> tuple[dict[str, str], bool]:
    """Create (or reuse) the shared MCP gateway with IAM (SigV4) inbound auth.
    `name=None` means this environment's gateway (`gateway_name()`)."""
    name = name or gateway_name()
    items: list[dict[str, Any]] = []
    token = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_gateways(**kwargs)
        items.extend(page.get("items", []))
        token = page.get("nextToken")
        if not token:
            break
    for gw in items:
        if gw.get("name") == name:
            detail = control.get_gateway(gatewayIdentifier=gw["gatewayId"])
            return (
                {
                    "id": detail["gatewayId"],
                    "arn": detail["gatewayArn"],
                    "url": detail["gatewayUrl"],
                },
                False,
            )

    created = control.create_gateway(
        name=name,
        description="AgentCore Launchpad shared MCP gateway",
        roleArn=role_arn,
        protocolType="MCP",
        authorizerType="AWS_IAM",
        # N17-obs474 (security): never `exceptionLevel="DEBUG"` — with it an interceptor /
        # target Lambda error returns the Lambda's errorMessage AND stack trace to the
        # CALLER (the agent, which may be prompt-injected). DEBUG is the only value the
        # service model has; omitting the field is the non-debug level.
    )
    gateway_id = created["gatewayId"]
    _wait_gateway_ready(control, gateway_id, timeout_s=timeout_s)
    detail = control.get_gateway(gatewayIdentifier=gateway_id)
    return (
        {"id": gateway_id, "arn": detail["gatewayArn"], "url": detail["gatewayUrl"]},
        True,
    )


# N17-obs474: every UpdateGateway input field except the identifier and exceptionLevel.
# UpdateGateway REPLACES the configuration — an omitted optional field is reset — so a
# repair carries each of these exactly as GetGateway reports it (a guard test ties this
# tuple to the SDK's UpdateGateway input shape).
GATEWAY_UPDATE_FIELDS = (
    "name", "description", "roleArn", "protocolType", "protocolConfiguration",
    "authorizerType", "authorizerConfiguration", "kmsKeyArn", "customTransformConfiguration",
    "interceptorConfigurations", "policyEngineConfiguration", "wafConfiguration",
)


def gateway_update_payload(detail: dict[str, Any]) -> dict[str, Any]:
    """The UpdateGateway request that keeps everything ``detail`` (a GetGateway answer)
    reports and drops only ``exceptionLevel`` — output-only fields never travel."""
    carried = {k: detail[k] for k in GATEWAY_UPDATE_FIELDS if detail.get(k) is not None}
    return {"gatewayIdentifier": detail["gatewayId"], **carried}


def repair_exception_level(
    control: Any, gateway_id: str, *, apply: bool, timeout_s: int = 300,
) -> dict[str, Any]:
    """N17-obs474: take an existing gateway off ``exceptionLevel: DEBUG``, idempotently.

    Reads the gateway; a gateway already without it is left alone (``action: none``).
    Otherwise the update is ``gateway_update_payload`` (``action: would_update`` when not
    ``apply``). After the update the gateway is read back once READY: ``exceptionLevel``
    must be gone and every carried field unchanged, else ``RuntimeError`` naming the
    fields — a repair that changed anything else is never reported as done."""
    before = control.get_gateway(gatewayIdentifier=gateway_id)
    report: dict[str, Any] = {"gateway_id": gateway_id, "name": before.get("name"),
                              "before": before.get("exceptionLevel")}
    if before.get("exceptionLevel") is None:
        return report | {"action": "none"}
    payload = gateway_update_payload(before)
    if not apply:
        return report | {"action": "would_update", "payload": payload}
    control.update_gateway(**payload)
    _wait_gateway_ready(control, gateway_id, timeout_s=timeout_s)
    after = control.get_gateway(gatewayIdentifier=gateway_id)
    changed = [k for k in GATEWAY_UPDATE_FIELDS if after.get(k) != before.get(k)]
    if after.get("exceptionLevel") is not None:
        changed.append("exceptionLevel")
    if changed:
        raise RuntimeError(f"gateway {gateway_id} read back with unexpected changes: "
                           + ", ".join(changed))
    return report | {"action": "updated", "after": after.get("exceptionLevel"),
                     "unchanged_fields_verified": True}


def interceptor_configuration(lambda_arn: str) -> dict[str, Any]:
    """The one interceptor entry Launchpad attaches (design §13.6, P0 b): REQUEST + RESPONSE
    on one Lambda, ``passRequestHeaders=false`` — the principal rides in ``params._meta``
    and the caller's SigV4 headers never reach the Lambda."""
    return {"interceptor": {"lambda": {"arn": lambda_arn}},
            "interceptionPoints": ["REQUEST", "RESPONSE"],
            "inputConfiguration": {"passRequestHeaders": False}}


def _interceptor_fields(entry: dict[str, Any]) -> tuple[Any, ...]:
    """The read-back comparison: only the fields we set (a service-added default on an entry
    must not fail a correct attach)."""
    return (_interceptor_arn(entry), tuple(sorted(entry.get("interceptionPoints") or [])),
            bool((entry.get("inputConfiguration") or {}).get("passRequestHeaders")))


def _interceptor_arn(entry: dict[str, Any]) -> str | None:
    return ((entry.get("interceptor") or {}).get("lambda") or {}).get("arn")


def set_interceptor(
    control: Any, gateway_id: str, lambda_arn: str, *, attach: bool, apply: bool,
    timeout_s: int = 300, record: Any = None,
) -> dict[str, Any]:
    """Attach (``attach=True``) or detach Launchpad's interceptor on an existing gateway.

    UpdateGateway REPLACES the configuration, so the request is ``gateway_update_payload``
    of the current GetGateway with only ``interceptorConfigurations`` changed. Attaching
    refuses (``RuntimeError``) when another interceptor holds a slot — the gateway allows one
    REQUEST and one RESPONSE interceptor. ``apply=False`` returns the planned payload
    (``action: would_update``). After an update the gateway is read back once READY: the
    interceptor list must be the intended one and every other carried field unchanged, else
    ``RuntimeError`` naming the fields. Removal order (design §5.2): detach BEFORE the
    Lambda is ever deleted, so the gateway never points at a missing function.

    ``record(attached: bool)`` (with ``apply``) persists the VERIFIED outcome — after a
    successful read-back, or when the gateway already is in the wanted state — so stream A
    reads ``policy_gateway_state``; a refused or failed update never calls it."""
    before = control.get_gateway(gatewayIdentifier=gateway_id)
    current = list(before.get("interceptorConfigurations") or [])
    ours = [e for e in current if _interceptor_arn(e) == lambda_arn]
    others = [e for e in current if _interceptor_arn(e) != lambda_arn]
    report: dict[str, Any] = {"gateway_id": gateway_id, "name": before.get("name"),
                              "lambda_arn": lambda_arn, "attach": attach,
                              "before": [_interceptor_arn(e) for e in current]}
    if before.get("exceptionLevel") is not None:
        # N17-obs474: an interceptor error on a DEBUG gateway reaches the agent with the
        # Lambda's stack trace; and an update here would silently drop DEBUG (it is not a
        # carried field) — attach AND detach refuse, the repair script takes it off first
        raise RuntimeError(f"gateway {gateway_id} runs with exceptionLevel "
                           f"{before['exceptionLevel']} — run "
                           "scripts/repair_gateway_exception_level.py --apply first")
    if attach:
        if others:
            raise RuntimeError(f"gateway {gateway_id} already has another interceptor "
                               f"({', '.join(str(_interceptor_arn(e)) for e in others)}) — "
                               "refusing to replace it")
        wanted = [interceptor_configuration(lambda_arn)]
    else:
        wanted = others
    if (attach and [_interceptor_fields(e) for e in ours] ==
            [_interceptor_fields(e) for e in wanted]) or (not attach and not ours):
        if apply and record is not None:
            record(attach)
        return report | {"action": "none"}
    payload = gateway_update_payload(before)
    payload.pop("interceptorConfigurations", None)
    if wanted:
        payload["interceptorConfigurations"] = wanted
    if not apply:
        return report | {"action": "would_update", "payload": payload}
    control.update_gateway(**payload)
    _wait_gateway_ready(control, gateway_id, timeout_s=timeout_s)
    after = control.get_gateway(gatewayIdentifier=gateway_id)
    changed = [k for k in GATEWAY_UPDATE_FIELDS
               if k != "interceptorConfigurations" and after.get(k) != before.get(k)]
    if [_interceptor_fields(e) for e in after.get("interceptorConfigurations") or []] != \
            [_interceptor_fields(e) for e in wanted]:
        changed.append("interceptorConfigurations")
    if after.get("exceptionLevel") != before.get("exceptionLevel"):
        changed.append("exceptionLevel")
    if changed:
        raise RuntimeError(f"gateway {gateway_id} read back with unexpected changes: "
                           + ", ".join(changed))
    if record is not None:
        record(attach)
    return report | {"action": "updated",
                     "after": [_interceptor_arn(e) for e in after.get(
                         "interceptorConfigurations") or []],
                     "unchanged_fields_verified": True}


def _wait_gateway_ready(control: Any, gateway_id: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_gateway(gatewayIdentifier=gateway_id)["status"]
        if status == "READY":
            return
        if status == "FAILED":
            raise RuntimeError(f"gateway {gateway_id} FAILED")
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"gateway {gateway_id} not READY after {timeout_s}s")


def ensure_api_key_provider(
    control: Any, api_key: str, name: str | None = None
) -> tuple[str, bool]:
    """Store the sample REST API key in AgentCore Identity (create-if-missing)."""
    name = name or api_key_provider_name()
    providers = control.list_api_key_credential_providers(maxResults=100).get(
        "credentialProviders", []
    )
    for provider in providers:
        if provider.get("name") == name:
            return provider["credentialProviderArn"], False
    created = control.create_api_key_credential_provider(name=name, apiKey=api_key)
    return created["credentialProviderArn"], True

# Still the system-owned Connection name AgentCore Identity's token vault would
# carry if a workspace was bootstrapped before this build removed the gateway's
# IdP-backed M2M outbound auth (`scripts/teardown.py` can still clean one up;
# nothing creates a new one). Kept as a literal — not re-derived from a deleted
# function.
GATEWAY_M2M_PROVIDER_NAME = "launchpad-gw-m2m"
GATEWAY_SCOPE = "launchpad-gw/invoke"


def gateway_m2m_provider_name() -> str:
    return resource_name(GATEWAY_M2M_PROVIDER_NAME)


def _list_targets(control: Any, gateway_id: str) -> dict[str, dict[str, Any]]:
    items = control.list_gateway_targets(gatewayIdentifier=gateway_id, maxResults=100).get(
        "items", []
    )
    return {t["name"]: t for t in items}


def ensure_lambda_target(
    control: Any, gateway_id: str, lambda_arn: str
) -> tuple[str, bool]:
    existing = _list_targets(control, gateway_id)
    if HR_TARGET_NAME in existing:
        return existing[HR_TARGET_NAME]["targetId"], False
    tools = json.loads((SAMPLES / "hr_database_lambda" / "tool_schema.json").read_text())
    created = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=HR_TARGET_NAME,
        description="HR database Lambda → MCP tools",
        targetConfiguration={
            "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}}
        },
        credentialProviderConfigurations=[
            {"credentialProviderType": "GATEWAY_IAM_ROLE"}
        ],
    )
    _wait_target_ready(control, gateway_id, created["targetId"])
    return created["targetId"], True


def ensure_openapi_target(
    control: Any, gateway_id: str, api_url: str, provider_arn: str
) -> tuple[str, bool]:
    existing = _list_targets(control, gateway_id)
    if FACTS_TARGET_NAME in existing:
        return existing[FACTS_TARGET_NAME]["targetId"], False
    spec = yaml.safe_load((SAMPLES / "rest_api" / "openapi.yaml").read_text())
    spec["servers"] = [{"url": api_url.rstrip("/")}]
    created = control.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=FACTS_TARGET_NAME,
        description="office-facts REST API (OpenAPI) → MCP tools",
        targetConfiguration={"mcp": {"openApiSchema": {"inlinePayload": json.dumps(spec)}}},
        credentialProviderConfigurations=[
            {
                "credentialProviderType": "API_KEY",
                "credentialProvider": {
                    "apiKeyCredentialProvider": {
                        "providerArn": provider_arn,
                        "credentialLocation": "HEADER",
                        "credentialParameterName": "x-api-key",
                    }
                },
            }
        ],
    )
    _wait_target_ready(control, gateway_id, created["targetId"])
    return created["targetId"], True


def _wait_target_ready(
    control: Any, gateway_id: str, target_id: str, timeout_s: int = 300
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_gateway_target(
            gatewayIdentifier=gateway_id, targetId=target_id
        )["status"]
        if status == "READY":
            return
        if status in ("FAILED", "CREATE_FAILED"):
            detail = control.get_gateway_target(
                gatewayIdentifier=gateway_id, targetId=target_id
            )
            raise RuntimeError(
                f"target {target_id} {status}: {detail.get('statusReasons')}"
            )
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"target {target_id} not READY after {timeout_s}s")


def run_gateway_bootstrap(control: Any, apigw_client: Any, config: dict[str, Any]) -> dict:
    """Idempotently ensure the gateway (IAM inbound auth) + the demo targets.

    The Build-Tools demo layer (hr-database Lambda, office-facts REST API) is
    CDK-shipped sample code, so it only exists where the stack is deployed. Its
    identifiers are read defensively: a gateway with no demo targets is a valid
    environment, and `workspace_bootstrap` provisions exactly that.
    """
    resources = config.get("resources", {})
    region = config["region"]
    api_key_id = resources.get("office_facts_api_key_id") or ""
    hr_lambda_arn = resources.get("hr_lambda_arn") or ""
    facts_api_url = resources.get("office_facts_api_url") or ""

    provider_arn, provider_created = "", False
    if api_key_id:
        api_key_value = apigw_client.get_api_key(apiKey=api_key_id, includeValue=True)["value"]
        provider_arn, provider_created = ensure_api_key_provider(control, api_key_value)
    gateway, gateway_created = ensure_gateway(
        control, role_arn=resources["gateway_role_arn"], region=region
    )
    hr_target, hr_created = "", False
    if hr_lambda_arn:
        hr_target, hr_created = ensure_lambda_target(control, gateway["id"], hr_lambda_arn)
    facts_target, facts_created = "", False
    if facts_api_url and provider_arn:
        facts_target, facts_created = ensure_openapi_target(
            control, gateway["id"], facts_api_url, provider_arn
        )

    return {
        "gateway": {**gateway, "created": gateway_created},
        "api_key_provider": {"arn": provider_arn, "created": provider_created},
        "targets": {
            HR_TARGET_NAME: {"id": hr_target, "created": hr_created},
            FACTS_TARGET_NAME: {"id": facts_target, "created": facts_created},
        },
    }


def load_config_file() -> dict[str, Any]:
    path = Path(REPO_ROOT / "config" / "launchpad.yaml")
    return yaml.safe_load(path.read_text()) if path.is_file() else {}
