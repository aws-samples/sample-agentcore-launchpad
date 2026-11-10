"""Self-built Harness (方式B) deploy method — spec D1 / analysis §3.L, plan N7.

    generate  → render the vendored agentcore-export template with the AgentSpec
    package   → pip (ARM64 wheels) → zip of the rendered multi-file tree → S3
    provision / deploy / register → the zip_runtime stages, unchanged

The bundle is rendered on every run and never stored on the spec: a redeploy with an
edited prompt/model re-renders (unlike a pre-CN conversion, whose code_bundle was a
frozen export). Template contract: docs/cn/interfaces/harness-template.md.
"""

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.deployer.input_contract import record_input_contract
from app.deployer.pipeline import (
    MethodCapabilities,
    StageContext,
    StageResult,
    register_method,
)
from app.deployer.zip_runtime import (
    _stage_deploy,
    _stage_provision,
    _stage_register,
    build_dir_for,
    build_zip,
    platform_requirements,
)
from app.harness_tool_access import (
    BUILTIN_RUNTIME_TOOLS,
    NATIVE_HARNESS_TOOLS,
    selected_native_tools,
)
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services import harness_convert as hc
from app.services import policy_enforcement
from app.services.harness_convert import ConversionError
from app.services.workspace import WorkspaceContext
from app.templates.a2a_support import (
    A2A_GRAFT_END,
    A2A_GRAFT_START,
    a2a_prompt_section,
    a2a_tool_descriptions,
    mounted_agents,
    render_a2a_tools_source,
)
from app.templates.gateway_support import gateway_tool_patterns, uses_gateway
from app.templates.kb_support import (
    KB_DEEP_TOOL_NAME,
    KB_TOOL_NAME,
    kb_deep_tool_description,
    kb_prompt_section,
    kb_tool_description,
    mounted_kbs,
    render_direct_kb_source,
)
from app.templates.memory_support import render_memory_source
from app.templates.model_support import render_model_load
from app.templates.principal_support import render_carrier_source

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
TEMPLATE_DIR = TEMPLATES / "harness_runtime"
# N6's runtime library (docs/cn/interfaces/memory-runtime.md §6) as ONE bundle module beside
# main.py: the model helpers it reads come from model/load.py — never a second copy of the
# model block, so the bundle keeps one key cache and one credential path (spec C4).
MEMORY_LIBRARY_HEADER = (
    "from model.load import _resolve_bedrock_api_key, MODEL_REGION, BEDROCK_BEARER_ENV\n\n"
)
BUILTIN_TOOLS_TEMPLATE = TEMPLATES / "harness_builtin_tools.py.tmpl"
# Only when the code-interpreter builtin is selected (R10). Floor = the release the
# class/attribute names in harness_builtin_tools.py.tmpl were verified against (0.8.3).
BUILTIN_REQUIREMENTS = ["strands-agents-tools>=0.8.3,<1"]
BROWSER_TOOL_TEMPLATE = TEMPLATES / "harness_builtin_browser.py.tmpl"
BROWSER_GRAFT_START = "# <launchpad-browser-tool:v1>"
BROWSER_GRAFT_END = "# </launchpad-browser-tool:v1>"


@dataclass(frozen=True)
class BuiltinModule:
    """How one AgentCore builtin tool config is rendered into the bundle (N15-obs16):
    a sibling module grafted on A2 with its own markers, plus the pins it rides on."""

    module: str
    template: Path
    start: str
    end: str
    requirements: tuple[str, ...] = ()


# One entry per ``BUILTIN_RUNTIME_TOOLS`` key — the renderer refuses a key without one.
BUILTIN_MODULES: dict[str, BuiltinModule] = {
    "code-interpreter": BuiltinModule(
        "launchpad_builtin_tools", BUILTIN_TOOLS_TEMPLATE,
        hc.BUILTIN_GRAFT_START, hc.BUILTIN_GRAFT_END, tuple(BUILTIN_REQUIREMENTS),
    ),
    # websockets rides on bedrock-agentcore (template pin) — no extra requirement
    "browser": BuiltinModule(
        "launchpad_browser_tool", BROWSER_TOOL_TEMPLATE, BROWSER_GRAFT_START, BROWSER_GRAFT_END,
    ),
}
GATEWAY_ALIAS = "launchpad_gw"
NATIVE_TOOL_HBS = Path("_cli") / "strands_base_main.py.hbs"
SKIP_DIRS = ("_cli",)
SKIP_FILES = ("requirements.txt.tmpl",)  # consumed by template_requirements, not a source
REQUIRED_FILES = (
    "main.py", "model/load.py", "memory/session.py", "skills/fetcher.py",
    "hooks/execution_limits.py", "mcp_client/client.py",
)
CONTRACT = "docs/cn/interfaces/harness-template.md"
MEMORY_SESSION_SHIM = '''"""memory/session.py — Launchpad shim (R8).

Contract: docs/cn/interfaces/memory-runtime.md. Replaces the agentcore export's AgentCore
Memory session manager with the self-hosted library N6 ships as `launchpad_memory`;
degrades to no memory when the platform did not inject LAUNCHPAD_MEMORY_ID or the
library is not packaged."""

import os


def get_memory_session_manager(session_id, actor_id):
    memory_id = os.environ.get("LAUNCHPAD_MEMORY_ID", "")
    if not memory_id:
        return None
    try:
        from launchpad_memory import make_session_manager
    except Exception as exc:  # pre-N6 bundle: the library is not packaged
        print(f"[launchpad] memory library unavailable: {type(exc).__name__}: {exc}",
              flush=True)
        return None
    try:
        return make_session_manager(actor_id=actor_id, session_id=session_id)
    except Exception as exc:
        print(f"[launchpad] memory session manager unavailable: {type(exc).__name__}: {exc}",
              flush=True)
        return None
'''


class HarnessRenderError(ConversionError):
    """A spec member the self-built Harness cannot honour (refused before any AWS call)."""


@dataclass
class HarnessBundle:
    files: dict[str, str]
    requirements: list[str]
    notes: dict[str, str] = field(default_factory=dict)


def load_template_files(template_dir: Path | None = None) -> dict[str, str]:
    """The vendored `*.tmpl` sources keyed by bundle path (suffix stripped), plus an
    empty `__init__.py` for every package directory."""
    root = template_dir or TEMPLATE_DIR
    if not (root / "main.py.tmpl").is_file():
        raise HarnessRenderError(
            f"harness template missing at {root} — vendor it per {CONTRACT} (N7a)"
        )
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*.tmpl")):
        rel = path.relative_to(root)
        if not path.is_file() or rel.parts[0] in SKIP_DIRS or rel.name in SKIP_FILES:
            continue
        files[rel.as_posix()[: -len(".tmpl")]] = path.read_text(encoding="utf-8")
    for name in list(files):
        parent = Path(name).parent
        while parent != Path("."):
            files.setdefault((parent / "__init__.py").as_posix(), "")
            parent = parent.parent
    return files


def template_requirements(template_dir: Path | None = None) -> list[str]:
    """The export's dependency ranges, frozen as pins at vendor time (R17)."""
    path = (template_dir or TEMPLATE_DIR) / "requirements.txt.tmpl"
    if not path.is_file():
        return []
    pins: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = re.split(r"(?:^|\s)#", line, maxsplit=1)[0].strip()
        if entry:
            pins.append(entry)
    return pins


def _synthetic_mcp_tools(
    spec: AgentSpec, workspace: WorkspaceContext
) -> tuple[list[dict], list[str] | None]:
    """The harness-shaped tool list `graft_missing_gateway_clients` rebuilds clients from:
    the shared Gateway as an AWS_IAM `agentcore_gateway` (R9, SigV4 with the runtime
    role, URL from the partition-aware ARN) plus unauthenticated `remote_mcp` servers."""
    tools: list[dict] = []
    allowed: list[str] = []
    if uses_gateway(spec):
        gateway_id = str(workspace.resources.get("gateway_id") or "")
        groups: dict[str, list] = {}
        for ref in spec.tools:
            if ref.type == "gateway":
                gid = str((ref.config or {}).get("gateway_id") or gateway_id)
                if not gid:
                    raise HarnessRenderError("a Gateway tool must name a gateway_id")
                groups.setdefault(gid, []).append(ref)
        for index, (gid, refs) in enumerate(groups.items()):
            alias = GATEWAY_ALIAS if index == 0 else f"{GATEWAY_ALIAS}_{index + 1}"
            arn = workspace.arn("bedrock-agentcore", f"gateway/{gid}")
            tools.append({
                "type": "agentcore_gateway", "name": alias,
                "config": {"agentCoreGateway": {"gatewayArn": arn, "outboundAuth": {"awsIam": {}}}},
            })
            selected = spec.model_copy(update={"tools": refs})
            allowed += [f"@{alias}/{p}" for p in gateway_tool_patterns(selected)]
    for ref in spec.tools:
        if ref.type != "mcp":
            continue
        # round 16: the creation-time validator must have refused this — kept as the
        # last line of defence for stored specs (tests/fail_early_registry.py)
        if ref.auth is not None or (ref.config or {}).get("auth") not in (None, "none"):
            raise HarnessRenderError(
                f"mcp tool {ref.name!r}: the self-built Harness can mount unauthenticated remote "
                'MCP servers only (no auth block and config.auth absent or "none")'
            )
        url = str((ref.config or {}).get("url") or "")
        # round 16: the creation-time validator must have refused this — kept as the
        # last line of defence for stored specs (tests/fail_early_registry.py)
        if not url.startswith(("http://", "https://")):
            raise HarnessRenderError(f"mcp tool {ref.name!r}: config.url (http/https) is required")
        tools.append(
            {"type": "remote_mcp", "name": ref.name, "config": {"remoteMcp": {"url": url}}}
        )
        allowed.append(f"@{ref.name}")
    return tools, (allowed or None)


def _compile_check(files: dict[str, str]) -> None:
    for rel, text in files.items():
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as exc:
                raise HarnessRenderError(f"rendered {rel} does not compile: {exc}") from exc


def render_harness_bundle(
    spec: AgentSpec, workspace: WorkspaceContext, template_dir: Path | None = None,
    *, principal_carrier: bool = False,
) -> HarnessBundle:
    """Render one AgentSpec into the multi-file runtime tree the package stage zips.

    Order (contract §"How N7 renders it"): load → KB tools (A2) → native tools (A2) →
    builtin tools (A2) → config bundle (A1/A3) → attachments (A5) → actor (A7) → memory
    hooks + recall (A10–A12) → loop bounds (A8) → skills (A6) → gateway / remote-MCP
    clients + soft-fail (A4) → replace model/load.py (A9) and memory/session.py (A10) →
    write launchpad_memory.py. ``principal_carrier`` (stream A3b) also writes the carrier
    module and grafts it; off, the bundle is byte for byte what it was before.
    """
    if spec.method != "harness":
        raise HarnessRenderError(f"render_harness_bundle got method {spec.method!r}")
    root = template_dir or TEMPLATE_DIR
    files = load_template_files(root)
    main = files["main.py"]
    notes: dict[str, str] = {}
    requirements = (
        platform_requirements(spec.method, spec.model_source, spec.protocol)
        + template_requirements(root)
    )

    # knowledge bases → direct retrieval tools (same channel as zip_runtime; N6 owns the tmpl)
    kbs = mounted_kbs(spec)
    peers = mounted_agents(spec)
    prompt = spec.system_prompt + kb_prompt_section(kbs) + a2a_prompt_section(peers)
    tool_defaults = dict(spec.tool_description_overrides)
    # mounted A2A agents → one ask_<agent> tool each, in a sibling module (N11 r3-C); the
    # same source the Strands template inlines, grafted on A2 like the builtin tools
    if peers:
        files["launchpad_a2a_tools.py"] = render_a2a_tools_source(peers)
        main = hc.graft_tool_module(
            main, start=A2A_GRAFT_START, end=A2A_GRAFT_END,
            import_line="from launchpad_a2a_tools import A2A_TOOLS", tool_names=["*A2A_TOOLS"],
        )
        tool_defaults = {**a2a_tool_descriptions(peers), **tool_defaults}
        notes["a2a_agents"] = (
            f"wired ({len(peers)} A2A peer(s) via InvokeAgentRuntime: "
            f"{', '.join(p['name'] for p in peers)})"
        )
    if kbs:
        files["launchpad_kb_tools.py"] = render_direct_kb_source(kbs)
        main = hc.graft_direct_kb_tools(main)
        tool_defaults = {KB_TOOL_NAME: kb_tool_description(kbs),
                         KB_DEEP_TOOL_NAME: kb_deep_tool_description(kbs), **tool_defaults}
        notes["knowledge_bases"] = f"wired ({len(kbs)} direct kb_search/kb_deep_search)"
    # native tools from the CLI's own codegen template, vendored next to the export
    natives = [n for n in NATIVE_HARNESS_TOOLS if n in selected_native_tools(spec.model_dump())]
    if natives:
        hbs = root / NATIVE_TOOL_HBS
        if not hbs.is_file():
            raise HarnessRenderError(
                f"{NATIVE_TOOL_HBS.as_posix()} missing from {root} ({CONTRACT})"
            )
        main = hc.graft_native_tools(main, natives, hbs.read_text(encoding="utf-8"))
        notes["native_tools"] = f"wired ({', '.join(natives)})"
    # builtin tools (R10; browser N15-obs16) — one sibling module per selected builtin.
    # The request boundary (services/method_validation) already refused what is not in
    # BUILTIN_RUNTIME_TOOLS; this stays the last line of defence for stored specs.
    builtins = list(dict.fromkeys(t.name for t in spec.tools if t.type == "builtin"))
    unsupported = [
        b for b in builtins if b not in BUILTIN_RUNTIME_TOOLS or b not in BUILTIN_MODULES
    ]
    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    if unsupported:
        raise HarnessRenderError(
            f"builtin tool(s) not rendered by the self-built Harness: {', '.join(unsupported)}"
        )
    wired: list[str] = []
    for name in builtins:
        module = BUILTIN_MODULES[name]
        callables = list(BUILTIN_RUNTIME_TOOLS[name])
        files[f"{module.module}.py"] = module.template.read_text(encoding="utf-8")
        main = hc.graft_tool_module(
            main, start=module.start, end=module.end,
            import_line=f"from {module.module} import {', '.join(callables)}",
            tool_names=callables,
        )
        requirements = requirements + [r for r in module.requirements if r not in requirements]
        wired += callables
    if wired:
        notes["builtin_tools"] = f"wired ({', '.join(wired)})"
    # platform contracts
    main = hc.graft_config_bundle(
        main, default_system_prompt=prompt, tool_description_overrides=tool_defaults
    )
    main = hc.graft_runtime_attachments(main)
    main = hc.graft_actor_from_payload(main)
    main = hc.graft_memory(main)  # A10–A12: hooks appended, recall prepended (memory-runtime.md §6)
    if principal_carrier:  # stream A3b: the turn's principal token into the Gateway _meta
        main = hc.graft_principal_carrier(main)
        files[f"{hc.PRINCIPAL_MODULE_NAME}.py"] = render_carrier_source()
        notes["principal"] = ("wired (launchpad_principal_carrier: token in the Gateway "
                              "MCP _meta + policy-session header)")
    main = hc.render_loop_bounds(main, spec.max_iterations, spec.timeout_seconds)
    main = hc.render_skill_sources(main, list(spec.skills))
    if spec.skills:
        notes["skills"] = f"wired ({len(spec.skills)} S3 prefix(es) fetched per request)"
    files["main.py"] = main
    # shared Gateway (SigV4) + remote MCP clients
    mcp_tools, allowed = _synthetic_mcp_tools(spec, workspace)
    if mcp_tools:
        files = hc.graft_missing_gateway_clients(files, mcp_tools, allowed,
                                                 principal_carrier=principal_carrier)
        files["mcp_client/client.py"] = hc.graft_lazy_gateway_token(files["mcp_client/client.py"])
        files["main.py"] = hc.graft_gateway_softfail(files["main.py"])
        if uses_gateway(spec):
            notes["gateway"] = "wired (SigV4 shared gateway, target-scoped tool filters)"
        if any(t["type"] == "remote_mcp" for t in mcp_tools):
            notes["remote_mcp"] = "wired (soft-fail: an unreachable server yields no tools)"
    # model + memory (replaced wholesale)
    files["model/load.py"] = render_model_load(spec)
    files["memory/session.py"] = MEMORY_SESSION_SHIM
    # the library is env-driven and inert without LAUNCHPAD_MEMORY_ID, so it always ships
    files["launchpad_memory.py"] = MEMORY_LIBRARY_HEADER + render_memory_source()
    notes["memory"] = "wired (launchpad_memory: S3 snapshots · SQS extraction · OpenSearch recall)"
    if not (spec.memory.short_term or spec.memory.long_term):
        notes["memory"] = "off (spec) — no LAUNCHPAD_MEMORY_ID is injected, the shim returns None"
    unrendered = [rel for rel, text in files.items() if "__LAUNCHPAD_" in text]
    if unrendered:
        raise HarnessRenderError(
            f"unrendered __LAUNCHPAD_ placeholder in {', '.join(unrendered)} — if the spec's "
            "system prompt or tool descriptions contain the text `__LAUNCHPAD_`, remove it: "
            "that substring is reserved for template placeholders and refused in rendered code"
        )
    _compile_check(files)
    # platform base + template pins + spec extras (R17); a pin both halves state
    # identically is listed once, a narrower template pin stays beside the platform range
    requirements = list(dict.fromkeys(requirements + list(spec.requirements)))
    return HarnessBundle(files=files, requirements=requirements, notes=notes)


def _bundle(ctx: StageContext, agent: Agent) -> HarnessBundle:
    bundle = ctx.scratch.get("harness_bundle")
    if bundle is None:  # resumed job: re-render (stages are idempotent)
        spec = AgentSpec(**agent.spec)
        policy_enforcement.stage_policy(ctx, agent, spec)  # logged once per job
        # stream A3b: the principal carrier — the job's one decision (re-review nit b)
        carrier = policy_enforcement.job_carrier_decision(ctx, agent, spec)[0]
        bundle = render_harness_bundle(spec, ctx.workspace, principal_carrier=carrier)
        ctx.scratch["harness_bundle"] = bundle
        ctx.scratch["code"] = bundle.files["main.py"]
        ctx.scratch["requirements"] = bundle.requirements
    return bundle


def _stage_generate(ctx: StageContext, agent: Agent) -> StageResult:
    spec = AgentSpec(**agent.spec)
    bundle = _bundle(ctx, agent)
    for capability, note in bundle.notes.items():
        ctx.log(f"{capability}: {note}")
    ctx.log(f"harness template · {len(bundle.files)} files · model {spec.model_id}")
    return StageResult(
        detail=f"harness template · {len(bundle.files)} files · "
               f"{len(bundle.files['main.py'])} chars"
    )


def _stage_package(ctx: StageContext, agent: Agent) -> StageResult:
    # round 16: the creation-time validator must have refused this — kept as the
    # last line of defence for stored specs (tests/fail_early_registry.py)
    bucket = ctx.workspace.resources.get("artifacts_bucket")
    if not bucket:
        raise RuntimeError(
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap"
        )
    if agent.system_key:  # preset: publish the versioned skill snapshot first (R5/R14)
        from app.system_agents.service import package_preset_skills

        ctx.log(package_preset_skills(ctx, agent).detail)
    bundle = _bundle(ctx, agent)
    build_dir = build_dir_for(agent.name)
    t0 = time.monotonic()

    def _on_pkg_ready(pkg_dir: Path) -> None:
        count = 0
        for rel, content in bundle.files.items():
            if rel == "main.py":
                continue  # build_zip already wrote the entrypoint
            dest = pkg_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
            count += 1
        ctx.log(f"bundle files staged: {count} (+ main.py)")

    zip_path = build_zip(
        bundle.files["main.py"], bundle.requirements, build_dir, on_pkg_ready=_on_pkg_ready,
        on_lock_ready=lambda n: ctx.log(f"requirements locked · {n} packages pinned with hashes"),
    )
    pip_secs = time.monotonic() - t0
    size_mb = zip_path.stat().st_size / 1e6
    s3_key = f"agents/{agent.name}/deployment_package.zip"
    ctx.workspace.client("s3").upload_file(str(zip_path), bucket, s3_key)
    ctx.scratch["s3_bucket"], ctx.scratch["s3_key"] = bucket, s3_key
    record_input_contract(ctx, bundle.files["main.py"])
    ctx.log(f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB → s3://{bucket}/{s3_key}")
    return StageResult(detail=f"pip+zip {pip_secs:.1f}s · {size_mb:.1f}MB · s3 ✓")


STAGES = {
    "generate": _stage_generate,
    "package": _stage_package,
    "provision": _stage_provision,
    "deploy": _stage_deploy,
    "register": _stage_register,
}
# N12-obs33/34: the self-built Harness is an AgentCore Runtime like any zip deploy, and
# its rendered entrypoint carries the config-bundle graft (A1/A3 above) — so it is both a
# config-bundle experiment subject and a canary subject (candidate = re-rendered bundle).
register_method(
    "harness", STAGES,
    MethodCapabilities(
        config_bundle=True, runtime_versions=True, candidate_mint=True,
        # creation-time domain (round 16): what render_harness_bundle above can take —
        # BUILTIN_RUNTIME_TOOLS is the render map and its key set must equal this
        # (tests/test_method_capabilities.py); remote MCP unauthenticated only (R9);
        # rest / tool auth ride a Gateway target instead (schemas TOOL_AUTH_METHODS)
        builtin_tools=frozenset(BUILTIN_RUNTIME_TOOLS),
        tool_types=frozenset({"builtin", "gateway", "mcp", "a2a"}),
        tool_auth_modes=frozenset(),
        native_tools=frozenset(NATIVE_HARNESS_TOOLS),
        protocols=frozenset({"http"}),
        memory=True, knowledge_bases=True,
        requires=frozenset({"artifacts_bucket"}),
    ),
)

# ── template self-check (scripts/harness_template_check.py, G(N7a)) ───────────────
_REDACTION = (  # partition-tolerant redaction regexes, not literals the renderer emits
    re.compile(r"arn:aws:"),  # cn-lint: allow
    re.compile(r"\.amazonaws\.com([^.]|$)"),  # cn-lint: allow
    re.compile(r"\b\d{12}\b"),
)


def _probe_spec() -> AgentSpec:
    """A representative spec: gateway + remote MCP + KB + skill + memory + natives + builtin."""
    return AgentSpec.model_validate({
        "name": "template-check", "method": "harness", "system_prompt": "probe",
        "native_tools": ["shell", "file_operations"], "skills": ["s3://bucket/skills/probe/"],
        "max_iterations": 5, "timeout_seconds": 60,
        "memory": {"short_term": True, "long_term": True},
        "knowledge_bases": [{"kb_id": "KBPROBE", "name": "probe", "description": "probe"}],
        "tools": [
            {"type": "gateway", "name": "probe",
             "config": {"gateway_id": "gw-probe", "tools": ["probe___x"]}},
            {"type": "mcp", "name": "remote",
             "config": {"url": "https://mcp.example/mcp", "auth": "none"}},
            {"type": "builtin", "name": "code-interpreter"},
            {"type": "builtin", "name": "browser"},  # N15-obs16
            # N11 r3-C: one mounted A2A peer so the self-check exercises the client graft
            {"type": "a2a", "name": "probe-peer",
             "config": {"record_id": "rec-probe", "runtime_arn":
                        "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:"
                        "runtime/probe_peer-abc123"}},
        ],
    })


def check_template(template_dir: Path) -> list[str]:
    """Every contract problem in a vendored template directory, tagged by anchor id;
    `[]` means the template renders and compiles for the representative spec."""
    problems: list[str] = []
    template_dir = Path(template_dir)
    try:
        files = load_template_files(template_dir)
    except HarnessRenderError as exc:
        return [str(exc)]
    problems += [f"missing {rel}.tmpl" for rel in REQUIRED_FILES if rel not in files]
    if not (template_dir / "requirements.txt.tmpl").is_file():
        problems.append("missing requirements.txt.tmpl")
    main = files.get("main.py", "")
    anchors = {
        "A1 DEFAULT_SYSTEM_PROMPT constant": hc._PROMPT_CONST_RE.search(main),
        "A1 system_prompt=DEFAULT_SYSTEM_PROMPT": hc._PROMPT_USE in main,
        "A2 tools = []": hc._TOOLS_COLLECTION_RE.search(main),
        "A4 mcp_clients = []": hc._MCP_CLIENTS_INIT_RE.search(main),
        "A6 s3_skill_sources = [...]": hc.SKILL_SOURCES_RE.search(main),
        "A7 user_id = getattr(context, 'user_id', …)": hc.ACTOR_RE.search(main),
        "A8 ExecutionLimitsHook(max_iterations=N,": hc.LOOP_ITER_RE.search(main),
        "A8 timeout_seconds=N, (hook)": hc.LOOP_HOOK_TIMEOUT_RE.search(main),
        "A8 timeout_seconds = N (invoke)": hc.LOOP_INVOKE_TIMEOUT_RE.search(main),
        "A9 from model.load import load_model": "from model.load import load_model" in main,
        "A10 from memory.session import get_memory_session_manager":
            "from memory.session import get_memory_session_manager" in main,
    }
    problems += [f"{name}: anchor missing" for name, ok in anchors.items() if not ok]
    for name, fn in (("A3", hc._agent_assignment), ("A5", hc.graft_runtime_attachments),
                     ("A11", hc.graft_memory_hooks), ("A12", hc._recall_anchor)):
        try:
            fn(main)
        except ConversionError as exc:
            problems.append(f"{name}: {exc}")
    if "MCPClient(" in files.get("mcp_client/client.py", ""):
        problems.append(
            "A4: mcp_client/client.py must be the imports-only scaffold "
            "(no MCPClient( construction)"
        )
    hbs = template_dir / NATIVE_TOOL_HBS
    if not hbs.is_file():
        problems.append(f"missing {NATIVE_TOOL_HBS.as_posix()}")
    else:
        for name in NATIVE_HARNESS_TOOLS:
            try:
                hc.native_tool_block(hbs.read_text(encoding="utf-8"), name)
            except ConversionError as exc:
                problems.append(f"A2 _cli hbs: {exc}")
    for rel, text in files.items():
        problems += [
            f"redaction: {rel} matches {pat.pattern}" for pat in _REDACTION if pat.search(text)
        ]
        if rel.endswith(".py"):
            try:
                compile(text, rel, "exec")
            except SyntaxError as exc:
                problems.append(f"{rel}: {exc}")
    if not problems:  # full render + compile with a representative spec
        try:
            ws = WorkspaceContext(account_id="111122223333", region="cn-northwest-1",
                                  resources={"gateway_id": "gw-probe"})
            render_harness_bundle(_probe_spec(), ws, template_dir)  # compiles every .py itself
        except Exception as exc:  # noqa: BLE001 — reported, not raised
            problems.append(f"render: {type(exc).__name__}: {exc}")
    return problems
