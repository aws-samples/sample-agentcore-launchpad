"""OpenCode — the Launchpad half of the Skill Lab executor seam (D16 / N13).

Everything the platform needs to know about the exec CLI lives here (the vendored
half is `vendor/skillopt/skillopt/model/opencode_backend.py`): the backend id the
API exposes, the host/worker environment contracts, the judge route and the CLI
readiness probe. The vendored CLI never spells a Bedrock hostname — the
OpenAI-compatible root is built HERE with the partition helper
(`aws_clients.global_bedrock_openai_base_url`) and handed over as
`SKILLOPT_OPENCODE_BASE_URL`; the Global Bedrock API key reaches the CLI only as
`AWS_BEARER_TOKEN_BEDROCK` (host: the hub's own bearer; worker: resolved through
Identity per session from `LAUNCHPAD_MODEL_CREDENTIAL`), exactly the contract of
docs/cn/interfaces/model-credential.md. See docs/cn/interfaces/skill-lab-executor.md.
"""

from __future__ import annotations

import json
import os
import shutil

from app.core.config import get_settings
from app.schemas.agent import (
    DEFAULT_MODEL_CREDENTIAL,
    catalog_version,
    model_catalog_table,
    model_entry,
    openai_compatible_providers,
)
from app.services import aws_clients
from app.services.workspace import WorkspaceContext

OPENCODE_BACKEND = "opencode_exec"
# The exec backends the API offers (routers.status → the wizards' picker).
TARGET_BACKENDS: tuple[str, ...] = (OPENCODE_BACKEND,)
CLI_BINARY = "opencode"
# The host binary each exec backend's agentic judge shells out to.
JUDGE_CLI_BINARY: dict[str, str] = {OPENCODE_BACKEND: CLI_BINARY}

# Environment contract shared with the vendored module (names must match
# opencode_backend.BASE_URL_ENV / API_KEY_ENV and agentcore_worker.MODEL_CREDENTIAL_ENV).
BASE_URL_ENV = "SKILLOPT_OPENCODE_BASE_URL"
API_KEY_ENV = aws_clients.GLOBAL_BEDROCK_BEARER_ENV  # AWS_BEARER_TOKEN_BEDROCK
MODEL_CREDENTIAL_ENV = "LAUNCHPAD_MODEL_CREDENTIAL"
MODEL_REGION_ENV = "LAUNCHPAD_MODEL_REGION"
# The vendored bedrock_chat judge/optimizer reads its region from this (us-west-2).
CHAT_REGION_ENV = "BEDROCK_CHAT_REGION"
DISABLE_AUTOUPDATE_ENV = "OPENCODE_DISABLE_AUTOUPDATE"
# Egress allow-list for the CLI's dead-proxy isolation (spike §7): the Global partition
# suffix the model endpoint lives on and the workspace's own suffix (S3 / Identity).
NO_PROXY_ENV = "SKILLOPT_OPENCODE_NO_PROXY"
# N-R6: the extra OpenAI-compatible providers (SiliconFlow …) as JSON — the vendored
# `opencode_backend.extra_providers()` contract; built from the catalog, never hand-written.
PROVIDERS_ENV = "SKILLOPT_OPENCODE_PROVIDERS"


def opencode_providers() -> dict[str, dict[str, object]]:
    """``{provider: {name, base_url, api_key_env, credential_provider, id_prefix, models}}``
    for every OpenAI-compatible provider of the catalog. `models` carries the OpenCode
    model entries (tool_call from the catalog) keyed by the id the endpoint receives."""
    table: dict[str, dict[str, object]] = {}
    for name, row in openai_compatible_providers().items():
        models = {
            str(entry["api_model_name"]): {
                "name": str(entry["label"]), "tool_call": bool(entry["tools"]),
            }
            for entry in model_catalog_table().values() if entry["provider"] == name
        }
        table[name] = {
            "name": f"{row['label']} (OpenAI-compatible)",
            "base_url": str(row["base_url"]),
            "api_key_env": str(row["api_key_env"]),
            "credential_provider": str(row["credential_provider"]),
            "id_prefix": str(row["id_prefix"]),
            "models": models,
            # D21 (M1-d, additive, per provider — the vendored `extra_providers()` treats
            # every top-level key as a provider): the catalog version this table came from
            "catalog_version": int(catalog_version()),
        }
    return table


def opencode_providers_env() -> str:
    return json.dumps(opencode_providers(), sort_keys=True)


def provider_hosts() -> list[str]:
    """Hostnames of the OpenAI-compatible endpoints (egress allow-list entries)."""
    from urllib.parse import urlparse

    return [urlparse(str(row["base_url"])).hostname or ""
            for row in openai_compatible_providers().values()]


def model_provider_name(model_id: str) -> str:
    """The provider name serving a target/judge model (`bedrock` for ids outside the table)."""
    entry = model_entry(model_id)
    return str(entry["provider"]) if entry else "bedrock"


def egress_allowlist(workspace: WorkspaceContext, model_region: str | None = None) -> str:
    model_suffix = aws_clients.dns_suffix_for_partition(
        aws_clients.partition_for_region(model_region or get_settings().model_region)
    )
    hosts = [model_suffix, *provider_hosts(), workspace.dns_suffix, "169.254.169.254",
             "localhost", "127.0.0.1"]
    return ",".join(dict.fromkeys(hosts))


def provider_base_url(model_region: str | None = None) -> str:
    """`https://bedrock-runtime.<model-region>.<partition suffix>/openai/v1`."""
    return aws_clients.global_bedrock_openai_base_url(model_region or get_settings().model_region)


def judge_exec_route(judge_model: str) -> tuple[str, str]:
    """(exec backend, exec model) the agentic judge runs with for `judge_model`.

    One CLI serves every model family — the id is passed through unchanged; the
    OpenCode provider addresses it as `bedrock-openai/<id>`."""
    return OPENCODE_BACKEND, judge_model


def judge_cli_binary(judge_model: str) -> str:
    return JUDGE_CLI_BINARY[judge_exec_route(judge_model)[0]]


def cli_present(binary: str = CLI_BINARY) -> bool:
    return shutil.which(binary) is not None


def model_credential_name(workspace: WorkspaceContext) -> str:
    """The Identity API-key provider the worker exchanges its workload token at."""
    return str(workspace.resources.get("model_credential_name") or DEFAULT_MODEL_CREDENTIAL)


def host_env(workspace: WorkspaceContext) -> dict[str, str]:
    """Env the HOST subprocess (vendored CLIs + the host-side agentic judge) needs to
    reach Global Bedrock: the OpenAI-compatible root for OpenCode, the hub's bearer
    (`RuntimeError` naming the variable when the hub has none), the egress
    allow-list and the Converse region for the bedrock_chat judge/optimizer."""
    settings = get_settings()
    env = {
        BASE_URL_ENV: provider_base_url(settings.model_region),
        API_KEY_ENV: aws_clients.require_global_bedrock_bearer(),
        NO_PROXY_ENV: egress_allowlist(workspace, settings.model_region),
        CHAT_REGION_ENV: settings.model_region,
        MODEL_REGION_ENV: settings.model_region,
        DISABLE_AUTOUPDATE_ENV: "1",
        PROVIDERS_ENV: opencode_providers_env(),
    }
    # N-R6: the hub's own key of each extra provider it HAS (process env, filled from SSM by
    # the host) — an unconfigured provider is simply absent and its models fail at the CLI
    # with a readable missing-key error rather than at submit.
    for row in openai_compatible_providers().values():
        key = os.environ.get(str(row["api_key_env"]), "").strip()
        if key:
            env[str(row["api_key_env"])] = key
    return env


def worker_runtime_environment(workspace: WorkspaceContext) -> dict[str, str]:
    """Env baked onto the exec-worker runtime. No key material: the worker resolves
    the Global Bedrock API key through Identity per session (model-credential.md)."""
    settings = get_settings()
    return {
        "AWS_REGION": workspace.region,
        BASE_URL_ENV: provider_base_url(settings.model_region),
        NO_PROXY_ENV: egress_allowlist(workspace, settings.model_region),
        MODEL_REGION_ENV: settings.model_region,
        MODEL_CREDENTIAL_ENV: model_credential_name(workspace),
        DISABLE_AUTOUPDATE_ENV: "1",
        # N-R6: provider table only — the worker resolves each extra provider's key through
        # Identity (`credential_provider`) per session, exactly like the Bedrock key.
        PROVIDERS_ENV: opencode_providers_env(),
    }
