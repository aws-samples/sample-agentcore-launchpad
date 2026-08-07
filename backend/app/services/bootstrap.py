"""Idempotent AgentCore bootstrap.

Reads the CDK stack outputs, ensures the account-singleton AgentCore
resources (registry, memory) exist exactly once, and writes the resulting
identifiers into ``config/launchpad.yaml``.

Every ``ensure_*`` function is single-purpose and create-if-missing by
name so later phases can add ``ensure_gateway()`` / ``ensure_policy_engine()``
alongside without touching existing behaviour.
"""

import secrets
import string
import time
from typing import Any

import boto3
import yaml
from botocore.exceptions import ClientError

from app.core.config import CONFIG_FILE, get_settings

STACK_NAME = "launchpad-base"
REGISTRY_NAME = "launchpad-registry"
REGISTRY_ACCESS_DENIED_REASON = (
    "Agent Registry access is denied by this account; Registry features are disabled"
)
MEMORY_NAME = "launchpad_memory"  # AgentCore memory names disallow hyphens
MEMORY_EVENT_EXPIRY_DAYS = 30

DEMO_USERS = [
    {"username": "river", "group": "platform-admin"},
    {"username": "demo", "group": "hr-analyst"},
]


def _client(service: str, region: str):
    return boto3.client(service, region_name=region)


def get_stack_outputs(region: str, stack_name: str = STACK_NAME) -> dict[str, str]:
    """CDK CfnOutputs as a flat dict; raises if the stack is absent."""
    cfn = _client("cloudformation", region)
    stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    return {o["OutputKey"]: o["OutputValue"] for o in stacks[0].get("Outputs", [])}


def ensure_registry(
    control: Any, name: str = REGISTRY_NAME
) -> tuple[dict[str, str] | None, bool]:
    """Return ({id, arn}, created), or (None, False) when account policy denies Registry."""
    try:
        paginator_items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
            page = control.list_registries(**kwargs)
            paginator_items.extend(page.get("registries", []))
            token = page.get("nextToken")
            if not token:
                break
        for reg in paginator_items:
            if reg.get("name") == name:
                return {"id": reg["registryId"], "arn": reg["registryArn"]}, False
        created = control.create_registry(
            name=name,
            description="AgentCore Launchpad asset catalog (agents / MCP tools / skills)",
        )
        # CreateRegistry returns only the ARN; the id is its final path segment.
        arn = created["registryArn"]
        return {"id": arn.split("/")[-1], "arn": arn}, True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"AccessDenied", "AccessDeniedException"}:
            return None, False
        raise


def ensure_memory(
    control: Any,
    name: str = MEMORY_NAME,
    execution_role_arn: str | None = None,
    wait: bool = True,
) -> tuple[dict[str, str], bool]:
    """Return ({id, arn}, created).

    Creates short-term event storage plus two long-term strategies
    (semantic facts + user preferences) used by the chat playground.
    """
    memories: list[dict[str, Any]] = []
    token: str | None = None
    while True:
        kwargs = {"maxResults": 100} | ({"nextToken": token} if token else {})
        page = control.list_memories(**kwargs)
        memories.extend(page.get("memories", []))
        token = page.get("nextToken")
        if not token:
            break
    for mem in memories:
        mem_id = mem.get("id") or mem.get("memoryId")
        if mem_id and mem_id.startswith(f"{name}-"):
            return {"id": mem_id, "arn": mem["arn"]}, False

    params: dict[str, Any] = {
        "name": name,
        "description": "Launchpad shared memory — short-term events + long-term strategies",
        "eventExpiryDuration": MEMORY_EVENT_EXPIRY_DAYS,
        "memoryStrategies": [
            {
                "semanticMemoryStrategy": {
                    "name": "semantic_facts",
                    "namespaces": ["/facts/{actorId}"],
                }
            },
            {
                "userPreferenceMemoryStrategy": {
                    "name": "user_preferences",
                    "namespaces": ["/preferences/{actorId}"],
                }
            },
        ],
    }
    if execution_role_arn:
        params["memoryExecutionRoleArn"] = execution_role_arn
    created = control.create_memory(**params)["memory"]
    mem_id, arn = created["id"], created["arn"]
    if wait:
        _wait_memory_active(control, mem_id)
    return {"id": mem_id, "arn": arn}, True


def _wait_memory_active(control: Any, memory_id: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = control.get_memory(memoryId=memory_id)["memory"]["status"]
        if status == "ACTIVE":
            return
        if status == "FAILED":
            raise RuntimeError(f"memory {memory_id} entered FAILED state")
        # Deliberate polling interval; the surrounding loop owns the deadline.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    raise TimeoutError(f"memory {memory_id} not ACTIVE after {timeout_s}s")


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    pw = [
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.digits),
    ]
    pw += [secrets.choice(alphabet) for _ in range(length - len(pw))]
    return "".join(pw)


def ensure_demo_user_passwords(
    cognito: Any, user_pool_id: str, existing: dict[str, Any] | None = None
) -> tuple[dict[str, str], bool]:
    """Set permanent passwords for demo users still in FORCE_CHANGE_PASSWORD.

    Returns ({username: password}, changed). Existing known passwords are kept.
    """
    existing = existing or {}
    passwords: dict[str, str] = dict(existing)
    changed = False
    for spec in DEMO_USERS:
        username = spec["username"]
        user = cognito.admin_get_user(UserPoolId=user_pool_id, Username=username)
        if user["UserStatus"] == "CONFIRMED" and username in passwords:
            continue
        password = passwords.get(username) or generate_password()
        cognito.admin_set_user_password(
            UserPoolId=user_pool_id,
            Username=username,
            Password=password,
            Permanent=True,
        )
        passwords[username] = password
        changed = True
    return passwords, changed


def load_config() -> dict[str, Any]:
    if CONFIG_FILE.is_file():
        data = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    return {}


def merge_config(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge update into base (update wins; nested dicts merged)."""
    merged = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def write_config(update: dict[str, Any]) -> dict[str, Any]:
    merged = merge_config(load_config(), update)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        "# Generated by scripts/bootstrap.py — do not commit (gitignored).\n"
        + yaml.safe_dump(merged, sort_keys=True, allow_unicode=True),
        encoding="utf-8",
    )
    return merged


def run_bootstrap(region: str | None = None) -> dict[str, Any]:
    """Full bootstrap pass. Returns a summary of what was created vs reused."""
    region = region or get_settings().region
    outputs = get_stack_outputs(region)
    control = _client("bedrock-agentcore-control", region)
    cognito = _client("cognito-idp", region)
    sts = _client("sts", region)

    account_id = sts.get_caller_identity()["Account"]
    registry, registry_created = ensure_registry(control)
    registry_summary = {
        "available": registry is not None,
        "id": registry["id"] if registry else "",
        "arn": registry["arn"] if registry else "",
        "created": registry_created,
        "reason": None if registry else REGISTRY_ACCESS_DENIED_REASON,
    }
    memory, memory_created = ensure_memory(
        control, execution_role_arn=outputs.get("AgentExecutionRoleArn")
    )
    existing_pw = (load_config().get("demo_users") or {}).get("passwords", {})
    passwords, pw_changed = ensure_demo_user_passwords(
        cognito, outputs["UserPoolId"], existing_pw
    )

    config = write_config(
        {
            "account_id": account_id,
            "region": region,
            "resources": {
                "artifacts_bucket": outputs["ArtifactsBucketName"],
                "ecr_repo": outputs["EcrRepoName"],
                "ecr_repo_uri": outputs["EcrRepoUri"],
                "codebuild_project": outputs["CodeBuildProjectName"],
                "user_pool_id": outputs["UserPoolId"],
                "user_pool_client_id": outputs["UserPoolClientId"],
                "execution_role_arn": outputs["AgentExecutionRoleArn"],
                # Empty values deliberately replace stale identifiers after an
                # account policy starts denying Registry.
                "registry_id": registry_summary["id"],
                "registry_arn": registry_summary["arn"],
                "registry_unavailable_reason": registry_summary["reason"] or "",
                "memory_id": memory["id"],
                "memory_arn": memory["arn"],
                # build-tools layer (phase 6+); absent on stacks predating it
                "hr_lambda_arn": outputs.get("HrLambdaArn", ""),
                "office_facts_api_url": outputs.get("OfficeFactsApiUrl", ""),
                "office_facts_api_key_id": outputs.get("OfficeFactsApiKeyId", ""),
                "gateway_role_arn": outputs.get("GatewayRoleArn", ""),
                "m2m_client_id": outputs.get("M2MClientId", ""),
                # managed KB layer; absent on stacks predating it
                "kb_role_arn": outputs.get("KbRoleArn", ""),
            },
            "demo_users": {"passwords": passwords},
        }
    )

    gateway_summary = None
    if outputs.get("GatewayRoleArn"):
        from app.services.gateway_bootstrap import run_gateway_bootstrap

        gateway_summary = run_gateway_bootstrap(
            control, _client("apigateway", region), config, cognito_client=cognito
        )
        write_config(
            {
                "resources": {
                    "gateway_id": gateway_summary["gateway"]["id"],
                    "gateway_arn": gateway_summary["gateway"]["arn"],
                    "gateway_url": gateway_summary["gateway"]["url"],
                    "api_key_provider_arn": gateway_summary["api_key_provider"]["arn"],
                    "oauth_provider_arn": gateway_summary["oauth_provider"]["arn"],
                }
            }
        )

    policy_summary = None
    if gateway_summary is not None:
        from app.services.policy_bootstrap import run_policy_bootstrap

        policy_summary = run_policy_bootstrap(
            control,
            _client("xray", region),
            load_config(),
            logs=_client("logs", region),
        )
        write_config(
            {
                "resources": {
                    "policy_engine_id": policy_summary["policy_engine"]["id"],
                    "policy_engine_arn": policy_summary["policy_engine"]["arn"],
                }
            }
        )

    return {
        "account_id": account_id,
        "region": region,
        "registry": registry_summary,
        "memory": {**memory, "created": memory_created},
        "gateway": gateway_summary,
        "policy": policy_summary,
        "demo_passwords_set": pw_changed,
        "stack_outputs": outputs,
    }
