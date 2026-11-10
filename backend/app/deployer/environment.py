"""Shared runtime environment derivation for AgentCore deployers."""

import json
from collections.abc import Mapping
from typing import Any

from app.core import naming
from app.core.config import get_settings
from app.core.errors import AppError
from app.schemas.agent import AgentSpec
from app.services import a2a_actor
from app.templates.gateway_support import (
    ENV_GATEWAYS,
    ENV_TOOLS,
    ENV_URL,
    gateway_tool_patterns,
    uses_gateway,
)
from app.templates.identity_support import ENV_WORKLOAD, outbound_auth_env, uses_as_user
from app.templates.memory_support import (
    ENV_KB_OS_ENDPOINT,
    ENV_MEMORY_BUCKET,
    ENV_MEMORY_EXTRACT_MODEL,
    ENV_MEMORY_LONG_TERM,
    ENV_MEMORY_OS_ENDPOINT,
    ENV_MEMORY_PREFIX,
    ENV_MEMORY_QUEUE_URL,
)

# Tool-level outbound auth declarations ({tool, type, connection, kind, mode,
# scopes[, url]} JSON). BYOC code reads this to perform its own Identity SDK
# exchange (the platform generates no code for it); generated agents carry it
# for observability only — their exchange config is baked into the source.
ENV_OUTBOUND_AUTH = "LAUNCHPAD_OUTBOUND_AUTH"
# N16-obs90: the per-agent key an A2A runtime verifies the hub-signed memory actor with
ENV_A2A_ACTOR_KEY = a2a_actor.ENV_A2A_ACTOR_KEY
# as_user (3LO): where AgentCore Identity sends the browser after IdP consent,
# and the agent the round-tripped customState names. The deployer allow-lists
# the same URL on the workload identity (deployer/return_url.py).
ENV_OAUTH_RETURN_URL = "LAUNCHPAD_OAUTH_RETURN_URL"
ENV_AGENT_ID = "LAUNCHPAD_AGENT_ID"
# Global Bedrock model access (spec C3/C4): the region the profile is entered from and
# the AgentCore Identity API-key provider holding the key. Every method gets both —
# generated templates read them, BYOC code performs the same GetResourceApiKey exchange.
ENV_MODEL_REGION = "LAUNCHPAD_MODEL_REGION"
ENV_MODEL_CREDENTIAL = "LAUNCHPAD_MODEL_CREDENTIAL"
# Routed config bundles (N12, plan §B.6): the bucket the runtime's ``_config_bundle()``
# reads ``bundles/<agent>/<id>/v<n>.json`` from when a request carries a bundle pointer
# (CA-2). Set only once the workspace has an artifacts bucket — the reader treats a
# missing key as "no routed bundle", an empty name would look configured.
ENV_BUNDLE_BUCKET = "LAUNCHPAD_BUNDLE_BUCKET"
# N-SCN-bug4: read by both generated templates' max_tokens recovery (default 1 there too)
ENV_MAX_TOKENS_CONTINUATIONS = "LAUNCHPAD_MAX_TOKENS_CONTINUATIONS"
DEFAULT_MAX_TOKENS_CONTINUATIONS = 1  # the templates' own default
# N-SCN-bug4 ⑤: the lower output cap a max_tokens continuation call uses (both templates;
# 0 there too = the model's own cap). Written only when the operator set it.
ENV_CONTINUATION_MAX_TOKENS = "LAUNCHPAD_CONTINUATION_MAX_TOKENS"
DEFAULT_CONTINUATION_MAX_TOKENS = 0

# N16-obs72 (lead ruling 2026-10-19): AgentCore Runtime caps ``environmentVariables`` at 50
# and the platform's own keys share that budget with the member's ``AgentSpec.env``. EVERY
# key ``runtime_environment`` — or a method deployer on top of it (byoc's ``MODEL_ID`` /
# ``ALLOWED_MODEL_IDS``) — may inject is listed here, so the budget cannot drift silently:
# ``tests/test_runtime_environment.py`` builds a maximal configuration and asserts it injects
# exactly these keys, and that ``schemas/agent.AGENT_ENV_MAX_VARS`` = 50 − PLATFORM_ENV_RESERVED
# (the schema module cannot import this one — it imports ``AgentSpec`` — so the number is
# repeated there and pinned by the same test). Adding an injected key = add it here AND lower
# the schema cap.
PLATFORM_ENV_KEYS: tuple[str, ...] = (
    ENV_MODEL_REGION, ENV_MODEL_CREDENTIAL,
    ENV_MAX_TOKENS_CONTINUATIONS, ENV_CONTINUATION_MAX_TOKENS,
    ENV_BUNDLE_BUCKET, ENV_WORKLOAD, ENV_AGENT_ID,
    "LAUNCHPAD_MEMORY_ID", ENV_MEMORY_BUCKET, ENV_MEMORY_PREFIX, ENV_MEMORY_OS_ENDPOINT,
    ENV_MEMORY_QUEUE_URL, ENV_MEMORY_LONG_TERM,
    ENV_MEMORY_EXTRACT_MODEL,  # F-LOAD-02 (N17-obs533): the memory_extract role's row
    # memory_selfhosted.recall_env_for (long-term recall knobs)
    "LAUNCHPAD_MEMORY_HALF_LIFE_DAYS", "LAUNCHPAD_MEMORY_TTL_DAYS",
    ENV_KB_OS_ENDPOINT, naming.ENV_NAME_ENV_VAR,
    ENV_URL, ENV_TOOLS, ENV_GATEWAYS,
    ENV_OUTBOUND_AUTH, ENV_OAUTH_RETURN_URL,
    ENV_A2A_ACTOR_KEY,  # protocol a2a only (N16-obs90)
    "MODEL_ID", "ALLOWED_MODEL_IDS",  # deployer/byoc.py setdefault on top of this module
)
PLATFORM_ENV_RESERVED = 28  # the budget cap; 25 keys (N17-obs533: LAUNCHPAD_MEMORY_EXTRACT_MODEL)
# The Runtime's own cap on ``environmentVariables`` (= ``schemas/agent.RUNTIME_ENV_LIMIT``).
RUNTIME_ENV_LIMIT = 50


def check_env_budget(environment: Mapping[str, str], spec: AgentSpec) -> None:
    """The MERGED map (member + platform keys) must fit the Runtime's cap — asserted here,
    at second 1 of the ``generate`` stage, with a named reason (``agent.env_budget_exceeded
    {user, platform, limit}``) instead of ~130 s later as CreateAgentRuntime's
    ValidationException (rev-r16 FIX-FIRST on N16-obs72). The schema cap keeps the member's
    share honest; this is the belt for the day a platform key is added without lowering it."""
    user = len(spec.env)
    platform = len(set(environment) - set(spec.env))
    if len(environment) > RUNTIME_ENV_LIMIT:
        raise AppError(
            "agent.env_budget_exceeded",
            f"agent.env_budget_exceeded: the runtime environment would carry "
            f"{len(environment)} variables ({user} from the spec + {platform} platform keys) "
            f"but AgentCore Runtime allows {RUNTIME_ENV_LIMIT} — remove "
            f"{len(environment) - RUNTIME_ENV_LIMIT} from the spec's env",
            {"user": user, "platform": platform, "limit": RUNTIME_ENV_LIMIT},
            status_code=422,
        )


def runtime_environment(
    spec: AgentSpec,
    resources: Mapping[str, Any],
    workload_name: str = "",
    agent_id: str = "",
) -> dict[str, str]:
    """Merge user environment with platform-owned runtime values.

    ``workload_name`` is the runtime's auto-created workload identity name, which
    does not exist until the runtime does — so it is empty on create and supplied
    (the runtime id) on later updates. The rendered Gateway client treats a missing
    value as "no self-minted token", not as an error.

    Global parity (2026-10-27): the policy module writes NOTHING here — every declared Gateway
    tool pattern reaches ``LAUNCHPAD_GATEWAY_TOOLS`` and the Gateway interceptor decides each
    call; the former narrowing / ``LAUNCHPAD_POLICY_*`` keys are gone.
    """
    environment = dict(spec.env)
    environment[ENV_MODEL_REGION] = spec.model_region
    environment[ENV_MODEL_CREDENTIAL] = spec.model_credential
    if spec.protocol == "a2a" and agent_id:
        # N16-obs90: derived from the console's session secret in-process (never stored);
        # the A2A template binds a hub-signed memory actor only with this key
        environment[ENV_A2A_ACTOR_KEY] = a2a_actor.actor_key(agent_id)
    # N-SCN-bug4: the max_tokens continuation budget of the generated templates. Both
    # templates default to 1 on their own, so the key is only written when the operator
    # changed the hub setting (today's environment stays byte for byte, like D17's env
    # name); a value in the spec's own `env` wins (per-agent override).
    continuations = get_settings().max_tokens_continuations
    if continuations != DEFAULT_MAX_TOKENS_CONTINUATIONS:
        environment.setdefault(ENV_MAX_TOKENS_CONTINUATIONS, str(continuations))
    continuation_cap = get_settings().continuation_max_tokens
    if continuation_cap != DEFAULT_CONTINUATION_MAX_TOKENS:
        environment.setdefault(ENV_CONTINUATION_MAX_TOKENS, str(continuation_cap))
    bundle_bucket = str(resources.get("artifacts_bucket") or "")
    if bundle_bucket:
        environment[ENV_BUNDLE_BUCKET] = bundle_bucket
    # The runtime's own workload identity name (= its runtime id), known from the second
    # deploy on. Every template's LAST-RESORT self-mint for a request that arrives without
    # a Runtime-injected workload token — which Identity refuses for a runtime-linked
    # identity ("linked to a service"), so in practice the caller must send runtimeUserId.
    if workload_name:
        environment[ENV_WORKLOAD] = workload_name
    # a spec-pinned memory overrides the workspace's shared bootstrap memory
    memory_id = spec.memory.memory_id or resources.get("memory_id")
    if (spec.memory.short_term or spec.memory.long_term) and memory_id:
        environment["LAUNCHPAD_MEMORY_ID"] = str(memory_id)
    # --- CN memory/KB (N6) ---
    # Self-hosted substrate (S3 snapshots · OpenSearch · SQS). Only when the workspace has
    # it: the rendered library treats missing keys as "memory off" rather than erroring.
    endpoint = str(resources.get("memory_os_endpoint") or "")
    if (spec.memory.short_term or spec.memory.long_term) and memory_id and endpoint:
        environment[ENV_MEMORY_BUCKET] = str(
            resources.get("memory_bucket") or resources.get("artifacts_bucket") or ""
        )
        environment[ENV_MEMORY_PREFIX] = str(resources.get("memory_prefix") or "memory")
        environment[ENV_MEMORY_OS_ENDPOINT] = endpoint
        environment[ENV_MEMORY_QUEUE_URL] = str(resources.get("memory_queue_url") or "")
        environment[ENV_MEMORY_LONG_TERM] = "1" if spec.memory.long_term else "0"
        # F-LOAD-02 (N17-obs533): the `memory_extract` role's catalog row, as the hub's own
        # messages carry it (`extract_model_block`, incl. the F-LOAD-03 temperature capability),
        # so the Runtime's hook enqueues the same block. Baked in here: a role change reaches an
        # agent on its NEXT redeploy (hub-side messages follow the role at once). Omitted while
        # the role names no row — the rendered library then omits `extract_model` exactly as
        # before and the Lambda falls back to its own LAUNCHPAD_EXTRACT_MODEL_ID.
        from app.services import embedding_backend

        extract_model = embedding_backend.extract_model_block()
        if extract_model:
            environment[ENV_MEMORY_EXTRACT_MODEL] = json.dumps(
                extract_model, separators=(",", ":"), sort_keys=True)
        if spec.memory.long_term:
            # r14-a: the store's recall knobs (half-life / TTL) — written only when they
            # differ from the core defaults, so today's environment stays byte for byte
            from app.services.memory_selfhosted import recall_env_for

            environment.update(recall_env_for(str(memory_id)))
    if spec.knowledge_bases and endpoint:
        environment[ENV_KB_OS_ENDPOINT] = endpoint
    # D17: the rendered library prefixes its index names with the environment that
    # deployed the agent (`launchpad-<env>-memory`, `launchpad-<env>-kb-<id>`) on the
    # domain every environment shares. Only written for a named environment — the
    # default one keeps today's environment byte for byte, and the library treats the
    # missing key as the default (unprefixed) names.
    env_name = naming.current_env_name()
    if env_name and (ENV_MEMORY_OS_ENDPOINT in environment or ENV_KB_OS_ENDPOINT in environment):
        environment[naming.ENV_NAME_ENV_VAR] = env_name
    # --- end CN memory/KB (N6) ---
    if uses_gateway(spec):
        # Only set what is actually resolved: the generated client treats a
        # missing URL as "no gateway tools" and continues, whereas an empty
        # string would look configured and produce a confusing auth failure.
        # No outbound token to mint (AWS_IAM Gateway, SigV4 with the runtime's
        # own execution role) — nothing else to set.
        gateway_url = str(resources.get("gateway_url") or "")
        gateway_refs = [ref for ref in spec.tools if ref.type == "gateway"]
        if gateway_refs:
            gateway_url = str(gateway_refs[0].config.get("gateway_url") or gateway_url)
        if gateway_url:
            environment[ENV_URL] = gateway_url
            # the client loads only the selected records' tools (target-scoped); the
            # Gateway interceptor decides each call (Global parity — nothing narrowed here)
            environment[ENV_TOOLS] = ",".join(gateway_tool_patterns(spec))
            groups: dict[str, list] = {}
            for ref in gateway_refs:
                url = str(ref.config.get("gateway_url") or resources.get("gateway_url") or "")
                if url:
                    groups.setdefault(url, []).append(ref)
            environment[ENV_GATEWAYS] = json.dumps([
                {"url": url,
                 "tools": gateway_tool_patterns(spec.model_copy(update={"tools": refs}))}
                for url, refs in groups.items()
            ], separators=(",", ":"))
    auth_declarations = outbound_auth_env(spec)
    if auth_declarations:
        environment[ENV_OUTBOUND_AUTH] = auth_declarations
    if uses_as_user(spec):
        environment[ENV_OAUTH_RETURN_URL] = get_settings().resolved_oauth_return_url()
    # N12-obs33 (found while admitting Harness agents to A/B): the runtime's bundle reader
    # builds `bundles/<LAUNCHPAD_AGENT_ID>/<id>/v<n>.json` and the identity / policy-guard
    # templates read the same key — it was injected for as_user agents only, so a plain
    # agent's treatment arm resolved `bundles//…`, fetched nothing and silently ran the
    # production prompt. Every runtime that has an id names itself.
    if agent_id:
        environment[ENV_AGENT_ID] = agent_id
    check_env_budget(environment, spec)
    return environment
