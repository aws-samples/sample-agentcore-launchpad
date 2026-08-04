"""Managed Harness deploy method (方式B) — no build, live in seconds.

Stage mapping:
    generate  → build the CreateHarness request from the AgentSpec
    package   → skipped (no artifact for a managed harness)
    provision → reuse the shared execution role provisioned by CDK
    deploy    → CreateHarness + poll READY (idempotent on resume)
    register  → create/refresh the A2A registry record (auto-submit)
"""

import re
from typing import Any

from app.core.config import get_settings
from app.deployer.pipeline import StageContext, StageResult, register_method
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import agent_iam, registry_console
from app.services import kb_gateway as kbgw
from app.services.agentcore import harness as hc
from app.services.agentcore.client import control_client

BUILTIN_TOOL_TYPES = {
    "code-interpreter": "agentcore_code_interpreter",
    "browser": "agentcore_browser",
}
GATEWAY_SCOPE = "launchpad-gw/invoke"
_TOOL_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")

# spec.model_source → HarnessBedrockModelConfig.apiFormat. Both sources ride the
# same bedrockModelConfig union branch: Mantle-hosted models speak the Responses
# API, native Bedrock models the Converse API, and the harness execution role
# authenticates either — the keyed branches (openAiModelConfig / geminiModelConfig
# / liteLlmModelConfig) would demand an AgentCore Identity API-key credential
# provider ARN this repo never provisions. A dict, not a branch, so a third
# source is one line.
_API_FORMAT = {"mantle": "responses", "bedrock": "converse_stream"}


def _api_format(spec: AgentSpec) -> str:
    return _API_FORMAT[spec.model_source]


def _kb_prompt(spec: AgentSpec) -> str:
    """System-prompt section mapping mounted KBs to their gateway tool names.

    Deliberately not shared with ``app.templates.kb_support.kb_prompt_section``:
    that one names the ``kb_search`` tool the zip/container templates generate,
    this one names the kb-gw MCP tools only a harness can reach.
    """
    agentic = kbgw.agentic_target_name(spec.name)
    lines = [
        "",
        "## Knowledge bases",
        f"Retrieval tools are mounted for you. Prefer `{agentic}___AgenticRetrieveStream`",
        "(multi-step retrieval across every mounted knowledge base, returns a cited",
        "answer) for open questions; use a per-KB `…___Retrieve` tool for a targeted",
        "single search. Mounted knowledge bases:",
    ]
    for kb in spec.knowledge_bases:
        label = kb.name or kb.kb_id
        target = kbgw.retrieve_target_name(kb.kb_id, kb.name or kb.kb_id)
        desc = f" — {kb.description}" if kb.description else ""
        lines.append(f"- {label} (tool `{target}___Retrieve`){desc}")
    lines.append(
        "Ground answers on retrieved content and cite sources when you use them."
    )
    return "\n".join(lines)


def build_create_params(
    spec: AgentSpec,
    execution_role_arn: str,
    memory_arn: str | None,
    gateway: dict[str, str] | None = None,
    kb_gateway: dict[str, str] | None = None,
    gateway_attachments: list[dict[str, Any]] | None = None,
) -> dict:
    """AgentSpec → CreateHarness kwargs. Harness names disallow hyphens.

    ``gateway`` carries {arn, oauth_provider_arn} — any spec tool of type
    "gateway" attaches the shared gateway with CLIENT_CREDENTIALS outbound auth
    (legacy config-less ToolRefs). ``gateway_attachments`` is the server-side
    live Registry/Gateway resolution for new ToolRefs and takes precedence.
    ``kb_gateway`` carries the same shape for launchpad-kb-gw; it attaches when
    the spec mounts knowledge bases.
    """
    system_prompt = spec.system_prompt
    if spec.knowledge_bases:
        system_prompt += _kb_prompt(spec)
    params: dict[str, Any] = {
        "harnessName": spec.name.replace("-", "_"),
        "executionRoleArn": execution_role_arn,
        "model": {
            "bedrockModelConfig": {
                "modelId": spec.model_id,
                "apiFormat": _api_format(spec),
            }
        },
        "systemPrompt": [{"text": system_prompt}],
        "maxIterations": spec.max_iterations,
        "timeoutSeconds": spec.timeout_seconds,
    }
    tools = []
    for tool in spec.tools:
        if tool.type == "builtin" and tool.name in BUILTIN_TOOL_TYPES:
            tools.append({"type": BUILTIN_TOOL_TYPES[tool.name], "name": tool.name})
        elif tool.type == "mcp" and tool.config.get("url"):
            # External remote MCP server (streamable-http), typically picked
            # from an APPROVED registry MCP record. Unauthenticated for now —
            # header/Identity-based auth is a follow-up.
            tools.append(
                {
                    "type": "remote_mcp",
                    "name": tool.name,
                    "config": {"remoteMcp": {"url": tool.config["url"]}},
                }
            )
    if gateway_attachments is not None:
        used_names: set[str] = set()
        for index, attachment in enumerate(gateway_attachments, start=1):
            gateway_arn = attachment.get("gateway_arn")
            outbound_auth = attachment.get("outbound_auth")
            if not gateway_arn or not outbound_auth:
                raise ValueError("resolved Gateway attachment is missing ARN or outbound auth")
            name = _gateway_tool_name(
                str(attachment.get("gateway_name") or "gateway"),
                index,
                used_names,
            )
            tools.append(
                {
                    "type": "agentcore_gateway",
                    "name": name,
                    "config": {
                        "agentCoreGateway": {
                            "gatewayArn": gateway_arn,
                            "outboundAuth": outbound_auth,
                        }
                    },
                }
            )
    elif gateway and any(t.type == "gateway" for t in spec.tools):
        tools.append(
            {
                "type": "agentcore_gateway",
                "name": "launchpad_gw",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": gateway["arn"],
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": gateway["oauth_provider_arn"],
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": [GATEWAY_SCOPE],
                            }
                        },
                    }
                },
            }
        )
    resolved_gateway_arns = {
        attachment.get("gateway_arn")
        for attachment in gateway_attachments or []
    }
    if (
        kb_gateway
        and spec.knowledge_bases
        and kb_gateway["arn"] not in resolved_gateway_arns
    ):
        tools.append(
            {
                "type": "agentcore_gateway",
                "name": "launchpad_kb_gw",
                "config": {
                    "agentCoreGateway": {
                        "gatewayArn": kb_gateway["arn"],
                        "outboundAuth": {
                            "oauth": {
                                "providerArn": kb_gateway["oauth_provider_arn"],
                                "grantType": "CLIENT_CREDENTIALS",
                                "scopes": [GATEWAY_SCOPE],
                            }
                        },
                    }
                },
            }
        )
    if tools:
        params["tools"] = tools
    if spec.skills:
        params["skills"] = [_skill_source(path) for path in spec.skills]
    if spec.env:
        params["environmentVariables"] = dict(spec.env)
    if (spec.memory.short_term or spec.memory.long_term) and memory_arn:
        params["memory"] = {"agentCoreMemoryConfiguration": {"arn": memory_arn}}
    return params


def _gateway_tool_name(name: str, index: int, used: set[str]) -> str:
    base = _TOOL_NAME_RE.sub("_", name).strip("_") or f"gateway_{index}"
    if base[0].isdigit():
        base = f"gateway_{base}"
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _skill_source(path: str) -> dict[str, Any]:
    """spec.skills entry → HarnessSkills member. The API's ``path`` member is a
    *filesystem* path — S3 URIs sent there pass validation but are silently
    never loaded at runtime; S3 sources belong in {"s3": {"uri": <dir prefix>}}.
    Legacy specs may carry `…/SKILL.md` file paths — normalize to the directory."""
    if path.startswith("s3://"):
        return {"s3": {"uri": path.removesuffix("SKILL.md")}}
    return {"path": path}


def _kb_gateway_config(resources: dict[str, Any]) -> dict[str, str] | None:
    if resources.get("kb_gateway_arn") and resources.get("oauth_provider_arn"):
        return {
            "arn": resources["kb_gateway_arn"],
            "oauth_provider_arn": resources["oauth_provider_arn"],
        }
    return None


def _build_live_params(spec: AgentSpec, resources: dict[str, Any]) -> dict[str, Any]:
    return build_create_params(
        spec,
        resources.get("execution_role_arn", ""),
        resources.get("memory_arn"),
        kb_gateway=_kb_gateway_config(resources),
        gateway_attachments=registry_console.resolve_gateway_attachments(spec.tools),
    )


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    settings = get_settings()
    spec = AgentSpec(**agent.spec)
    params = _build_live_params(spec, settings.resources)
    ctx.scratch["create_params"] = params
    ctx.log(
        f"harness request generated for {params['harnessName']} · "
        f"model {spec.model_id} ({spec.model_source} · {_api_format(spec)})"
    )
    return StageResult(detail=f"harnessName: {params['harnessName']}")


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    return StageResult(skipped=True, detail="skipped · harness — no build required")


def _stage_provision(ctx: StageContext, agent: Agent, iam_client: Any = None) -> StageResult:
    spec = AgentSpec(**agent.spec)
    role_arn, role_detail = agent_iam.provision_execution_role(
        agent, spec, get_settings(), ctx.log, iam=iam_client
    )
    ctx.scratch["execution_role_arn"] = role_arn

    if spec.knowledge_bases:
        control = control_client()
        gw = kbgw.ensure_kb_gateway_persisted(control)
        for kb in spec.knowledge_bases:
            kbgw.ensure_retrieve_target(
                control, gw["id"], kb.kb_id, kb.name or kb.kb_id, kb.description
            )
        kbgw.sync_agentic_target(
            control,
            gw["id"],
            spec.name,
            [
                {"kb_id": kb.kb_id, "description": kb.description or kb.name}
                for kb in spec.knowledge_bases
            ],
        )
        # generate ran before the KB gateway existed on first attach — rebuild
        # the request now that kb_gateway_* resources are persisted
        settings = get_settings()
        ctx.scratch["create_params"] = _build_live_params(spec, settings.resources)
        ctx.log(f"kb gateway ready · {len(spec.knowledge_bases)} knowledge base(s) mounted")
        return StageResult(
            detail=f"{role_detail} · kb targets ready ({len(spec.knowledge_bases)})"
        )

    # re-publish with every KB unselected → drop the stale per-agent target
    resources = get_settings().resources
    if resources.get("kb_gateway_id"):
        kbgw.sync_agentic_target(
            control_client(), resources["kb_gateway_id"], spec.name, []
        )
    return StageResult(detail=role_detail)


def _stage_deploy(ctx: StageContext, agent: Agent) -> StageResult:
    client = control_client()
    mode = ctx.scratch.get("mode", "create")
    db = ctx.session()
    try:
        row = db.get(Agent, agent.id)

        def _params() -> dict[str, Any]:
            params = ctx.scratch.get("create_params")
            if params is None:  # resume/update path without scratch — regenerate
                settings = get_settings()
                params = _build_live_params(AgentSpec(**row.spec), settings.resources)
            return params

        if mode == "update" and row.resource_id:  # in-place re-publish → UpdateHarness
            harness_id = row.resource_id
            update_params = hc.wrap_params_for_update(_params())
            update_params["harnessId"] = harness_id
            harness = agent_iam.retry_iam_propagation(
                lambda: hc.update_harness(client, update_params), ctx.log
            )
            row.version = str(harness.get("harnessVersion", row.version or "1"))
            db.commit()
            ctx.log(
                f"UpdateHarness accepted · harnessId {harness_id} · new version {row.version}"
            )
        elif row.resource_id:  # resumed create — harness already made, just poll
            harness_id = row.resource_id
            ctx.log(f"resuming — harness {harness_id} already created, polling status")
        else:  # first create
            harness = agent_iam.retry_iam_propagation(
                lambda: hc.create_harness(client, _params()), ctx.log
            )
            harness_id = harness["harnessId"]
            row.resource_id = harness_id
            row.arn = harness.get("arn")
            row.version = str(harness.get("harnessVersion", "1"))
            db.commit()
            ctx.log(f"CreateHarness accepted · harnessId {harness_id}")

        ready = hc.wait_harness_ready(client, harness_id)
        row.arn = ready["arn"]
        row.version = str(ready.get("harnessVersion", row.version or "1"))
        db.commit()
        ctx.log(f"harness READY · {ready['arn']}")
        return StageResult(detail=f"READY · {ready['arn']}")
    finally:
        db.close()


def _stage_register(ctx: StageContext, agent: Agent) -> StageResult:
    from app.deployer.registration import register_stage

    return register_stage(ctx, agent)


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}

register_method("harness", STAGES)


def delete_agent_resources(agent: Agent) -> None:
    """Remove the AWS-side harness + per-agent KB target for a ledger row (idempotent)."""
    client = control_client()
    resources = get_settings().resources
    if resources.get("kb_gateway_id"):
        spec_name = (agent.spec or {}).get("name") or agent.name
        try:
            kbgw.delete_agentic_target(client, resources["kb_gateway_id"], spec_name)
        except Exception:  # noqa: BLE001 — target cleanup must not block deletion
            pass
    if not agent.resource_id:
        return
    try:
        hc.delete_harness(client, agent.resource_id)
    except client.exceptions.ResourceNotFoundException:
        pass
