"""Graft library for the self-built Harness (方式B, spec D1 / analysis §3.L).

The `agentcore export` CLI's codegen for a Strands runtime agent is vendored under
`templates/harness_runtime/` (contract: docs/cn/interfaces/harness-template.md).
`deployer/harness_local.py` renders an AgentSpec into that code with the anchored,
idempotent grafts below — the config-bundle contract, direct KB tools, native tools
from the CLI's own template, the attachments contract, the SigV4 shared-Gateway client,
soft-failing MCP construction, the platform actor, loop bounds and skill sources. A
missing anchor FAILS the render (ConversionError) instead of shipping a runtime that
reports active and silently lacks a contract.
"""

import ast
import fnmatch
import re
from pathlib import Path
from typing import Any

from app.optimization.bundles import RUNTIME_BUNDLE_READER
from app.services import aws_clients
from app.templates.attachment_support import render_attachment_source
from app.templates.gateway_support import render_sigv4_transport as _render_sigv4_transport

# deterministic codegen anchors of the pinned CLI (@aws/agentcore 1.0.0-preview.20 —
# the version recorded in MANIFEST.md; anchors are deterministic across 0.21-1.0)
_PROMPT_CONST_RE = re.compile(
    r'^DEFAULT_SYSTEM_PROMPT\s*=\s*(?:"""|\'\'\')', re.MULTILINE
)
_PROMPT_USE = "system_prompt=DEFAULT_SYSTEM_PROMPT"
_RESOLVED_PROMPT_USE = "system_prompt=resolve_system_prompt()"
_AGENT_APPLY_CALL = "_launchpad_apply_tool_descriptions(agent)"
_TOOLS_COLLECTION_RE = re.compile(r"^tools = \[\]\s*$", re.MULTILINE)

GRAFT_START = "# <launchpad-config-bundle:v2>"
GRAFT_END = "# </launchpad-config-bundle:v2>"
KB_GRAFT_START = "# <launchpad-direct-kb:v1>"
KB_GRAFT_END = "# </launchpad-direct-kb:v1>"
NATIVE_GRAFT_START = "# <launchpad-native-tools:v1>"
NATIVE_GRAFT_END = "# </launchpad-native-tools:v1>"
# The vendored CLI export's own codegen template: grafted native tools are the exact
# implementation the export would have emitted.
NATIVE_TOOL_TEMPLATE = (
    Path(__file__).resolve().parents[1] / "templates" / "harness_runtime"
    / "_cli" / "strands_base_main.py.hbs"
)
_NATIVE_TEMPLATE_FLAGS = {"shell": "hasShell", "file_operations": "hasFileOperations"}
_NATIVE_TEMPLATE_IMPORTS = {"shell": "import subprocess", "file_operations": "import os"}
GW_SOFTFAIL_START = "# <launchpad-gateway-softfail:v1>"
GW_SOFTFAIL_END = "# </launchpad-gateway-softfail:v1>"
GW_LAZY_TOKEN_MARK = "_launchpad_lazy_gateway_transport"
GW_NO_BUNDLE_CLIENT = "_LaunchpadMCPClient"
_MCP_CLIENT_IMPORT = "from strands.tools.mcp.mcp_client import MCPClient\n"
# A converted twin may be handed a routed
# configuration bundle as OTel baggage. OTel's threading/asyncio instrumentation
# carries that baggage onto the twin's own Gateway MCP calls, and launchpad-gw then
# tries to resolve the (percent-encoded, truncated) bundle on tools/list and answers
# 400 — every A/B session fails at tool load. The twin already applies its bundle
# itself (config-bundle graft), so its Gateway calls go out without those keys.
_NO_BUNDLE_CLIENT_SOURCE = '''

_LAUNCHPAD_BUNDLE_BAGGAGE = ("aws.agentcore.configbundle_arn", "aws.agentcore.configbundle_version")


def _launchpad_attach_without_bundle_baggage():
    from opentelemetry import baggage, context

    current = context.get_current()
    for key in _LAUNCHPAD_BUNDLE_BAGGAGE:
        current = baggage.remove_baggage(key, context=current)
    return context.attach(current)


class _LaunchpadMCPClient(MCPClient):
    """Gateway MCP client whose calls never carry the caller's config-bundle baggage.

    ``start()`` spawns the session thread and every call is handed to it with the
    caller's OTel context, so both run with those baggage keys removed.
    """

    def start(self):
        from opentelemetry import context

        token = _launchpad_attach_without_bundle_baggage()
        try:
            return super().start()
        finally:
            context.detach(token)

    def _invoke_on_background_thread(self, coro):
        from opentelemetry import context

        token = _launchpad_attach_without_bundle_baggage()
        try:
            return super()._invoke_on_background_thread(coro)
        finally:
            context.detach(token)
'''
GW_SIGV4_TRANSPORT = "_launchpad_sigv4_streamablehttp"
# An AWS_IAM Gateway (e.g. a Registry-attached remote MCP behind its own Gateway)
# authorizes the caller's SigV4 signature as bedrock-agentcore:InvokeGateway — the
# grant agent_iam already scopes to the spec's gateway ids. There is no token to mint,
# so the rebuilt client signs every MCP request with the runtime role instead.
# rendered without the principal carrier line (stream A3 threads it here in A3b) — the
# graft is byte-identical to the template text it always was
_SIGV4_TRANSPORT_SOURCE = "\n\n" + _render_sigv4_transport(principal_carrier=False)
_GW_SIGV4_CLIENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)return MCPClient\(lambda: " + GW_SIGV4_TRANSPORT + r"\(",
    re.MULTILINE,
)
ATTACHMENT_GRAFT_START = "# <launchpad-attachments:v1>"
ATTACHMENT_GRAFT_END = "# </launchpad-attachments:v1>"
_ATTACHMENT_INVOKE = '''    if payload.get("attachments"):
        prompt = _launchpad_attachment_messages(prompt, payload["attachments"])
        yield {"event": "attachments", "contract": "v1"}
'''

# The export's eager token fetch, one occurrence per attached gateway. It bakes the
# Authorization header at MODULE scope, which is the reason a converted runtime
# could never authenticate: the workload access token only exists inside a request
# context, and this runs at import.
_GW_EAGER_TOKEN_RE = re.compile(
    r"^(?P<indent>[ \t]*)token = (?P<fetch>_get_bearer_token_\w+\(\))[ \t]*\n"
    r"(?P=indent)headers = \{\"Authorization\": f\"Bearer \{token\}\"\}"
    r" if token else \{\}[ \t]*\n"
    r"(?P=indent)return MCPClient\("
    r"lambda: streamablehttp_client\(url, headers=headers\)(?P<rest>[^)]*)\)[ \t]*$",
    re.MULTILINE,
)

# The export's module-scope gateway-client construction — the import-time crash
# site the v1 conversion caveat was protecting against.
# `[ \t]*`, not `\s*`: `\s` matches newlines, so a greedy tail would swallow the
# blank line after the call and silently reflow the exported source.
_GW_CLIENTS_CALL_RE = re.compile(
    r"^(?P<indent>[ \t]*)"
    r"(?P<call>mcp_clients[ \t]*\+=[ \t]*get_all_gateway_mcp_clients\(\)[ \t]*)$",
    re.MULTILINE,
)
_LEGACY_GRAFT_RE = re.compile(
    r"\n# ─── Launchpad platform contract: config bundles \(A/B experiments\)"
    r"[\s\S]*?# ─{10,}\n"
)


def _bundle_graft(
    default_system_prompt: str | None,
    tool_description_overrides: dict[str, str] | None,
) -> str:
    prompt_default = (
        repr(default_system_prompt)
        if default_system_prompt is not None
        else "DEFAULT_SYSTEM_PROMPT"
    )
    tool_defaults = repr(tool_description_overrides or {})
    return f'''

{GRAFT_START}
{RUNTIME_BUNDLE_READER}

_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT = {prompt_default}
_LAUNCHPAD_DEFAULT_TOOL_DESCRIPTIONS = {tool_defaults}


def _launchpad_config_bundle():
    """Active Launchpad config bundle for this request ({{}} when none routed; CA-2)."""
    try:
        ref = _launchpad_bundle_ref()
        return _launchpad_fetch_bundle(ref) if ref else {{}}
    except Exception:
        return {{}}


def resolve_system_prompt() -> str:
    """Bundle prompt wins; the promoted production prompt is the fallback."""
    return str(
        _launchpad_config_bundle().get("system_prompt")
        or _LAUNCHPAD_DEFAULT_SYSTEM_PROMPT
    )


def _launchpad_tool_descriptions():
    bundle = _launchpad_config_bundle()
    descriptions = dict(_LAUNCHPAD_DEFAULT_TOOL_DESCRIPTIONS)
    descriptions.update(bundle.get("tool_descriptions") or {{}})
    tools = bundle.get("tools") or {{}}
    if isinstance(tools, dict):
        descriptions.update({{
            name: value.get("description", "")
            for name, value in tools.items()
            if isinstance(value, dict)
        }})
    return descriptions


def _launchpad_apply_tool_descriptions(agent):
    registry = getattr(getattr(agent, "tool_registry", None), "registry", {{}})
    for name, description in _launchpad_tool_descriptions().items():
        tool = registry.get(name) if hasattr(registry, "get") else None
        if tool is not None and description:
            try:
                tool.tool_spec["description"] = str(description)
            except Exception:
                pass
    return agent
{GRAFT_END}
'''


BUNDLE_GRAFT = _bundle_graft(None, {})


class ConversionError(Exception):
    pass


def has_config_bundle_graft(main_py: str) -> bool:
    """Whether source contains a Launchpad-owned prompt bundle contract."""
    return (
        GRAFT_START in main_py
        or (
            "Launchpad platform contract: config bundles" in main_py
            and "def resolve_system_prompt()" in main_py
        )
    )


def _rewrite_prompt_constant_body(main_py: str, resolved: str) -> str:
    """A1: the baked DEFAULT_SYSTEM_PROMPT body is only ever the fallback the
    bundle graft falls back to — never a leftover template placeholder.

    The vendored template bakes the constant's body as the literal placeholder
    text __LAUNCHPAD_REFERENCE_PROMPT__; graft_config_bundle redirects the
    *usage* site to resolve_system_prompt(), but left unrewritten the constant's
    own body would still carry that placeholder into every render, tripping the
    renderer's blanket unrendered-__LAUNCHPAD_-placeholder scan. Keeps the
    original quote style (triple-double or triple-single) so _PROMPT_CONST_RE
    still matches on a later re-graft.
    """
    match = _PROMPT_CONST_RE.search(main_py)
    if match is None:
        return main_py
    quote = main_py[match.end() - 3 : match.end()]
    body_start = match.end()
    # The closing delimiter is the first UNESCAPED run of three quotes: a previously
    # rewritten body may end in `\\"` (escaped quote), which `str.index` would mistake
    # for the start of the delimiter and leave a stray quote behind on a re-graft.
    body_end = body_start
    while not main_py.startswith(quote, body_end):
        if body_end >= len(main_py):
            raise ConversionError(
                "graft anchor invalid (A1): DEFAULT_SYSTEM_PROMPT constant is unterminated"
            )
        body_end += 2 if main_py[body_end] == "\\" else 1
    # Uniform escaping: every backslash, then every quote character. The body then
    # never contains the delimiter (nor can it end in one or in a bare backslash), so
    # any run of quotes, a trailing quote and a trailing `\\"` all round-trip, and a
    # later re-graft finds the closing delimiter at the right place.
    escaped = resolved.replace("\\", "\\\\").replace(quote[0], "\\" + quote[0])
    return main_py[:body_start] + escaped + main_py[body_end:]


def graft_direct_kb_tools(main_py: str) -> str:
    """Register the materialized direct KB tools on an exported Harness."""
    return graft_tool_module(
        main_py, start=KB_GRAFT_START, end=KB_GRAFT_END,
        import_line="from launchpad_kb_tools import kb_deep_search, kb_search",
        tool_names=["kb_search", "kb_deep_search"],
    )


def native_tool_block(template: str, name: str) -> str:
    """The ``@tool def <name>`` block of the CLI template, ending with its append.

    The template gates it as ``{{#if <flag>}} ... {{/if}}`` without nested
    Handlebars; anything else means the CLI codegen changed."""
    flag = _NATIVE_TEMPLATE_FLAGS[name]
    for body in re.findall(r"\{\{#if " + flag + r"\}\}\n(.*?)\n\{\{/if\}\}", template, re.S):
        if f"def {name}(" in body:
            if "{{" in body or not body.rstrip().endswith(f"tools.append({name})"):
                break
            return body.rstrip()
    raise ConversionError(
        f"graft anchor missing (A2): no self-contained {name} block in the agentcore CLI "
        "template (agentcore CLI codegen changed?)"
    )


def graft_native_tools(main_py: str, names: list[str], template: str) -> str:
    """Add the native Harness tools the export dropped.

    The Harness accepts a bare builtin name (``file_operations``) in
    allowedTools, but the CLI export only enables a builtin when an entry
    matches ``builtin/<name>`` — a bare entry is silently dropped, and the
    Runtime twin can no longer read its Skills' ``references/`` files."""
    if NATIVE_GRAFT_START in main_py and NATIVE_GRAFT_END in main_py:
        return main_py
    missing = [n for n in names if not re.search(rf"^def {n}\(", main_py, re.M)]
    if not missing:
        return main_py
    match = _TOOLS_COLLECTION_RE.search(main_py)
    if match is None:
        raise ConversionError(
            "graft anchor missing (A2): tools = [] collection not found "
            "(agentcore CLI codegen changed?)"
        )
    imports = "\n".join(
        _NATIVE_TEMPLATE_IMPORTS[n] for n in missing
        if not re.search(rf"^{_NATIVE_TEMPLATE_IMPORTS[n]}$", main_py, re.M)
    )
    blocks = "\n\n".join(native_tool_block(template, n) for n in missing)
    parts = (NATIVE_GRAFT_START, imports, blocks, NATIVE_GRAFT_END)
    graft = "\n\n".join(part for part in parts if part)
    head = main_py[:match.start()] + match.group(0).rstrip()
    return head + "\n\n" + graft + "\n\n" + main_py[match.end():].lstrip("\n")


def _is_agent_apply(node: ast.AST) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    call = node.value
    return (
        isinstance(call.func, ast.Name)
        and call.func.id == "_launchpad_apply_tool_descriptions"
        and len(call.args) == 1
        and not call.keywords
        and isinstance(call.args[0], ast.Name)
        and call.args[0].id == "agent"
    )


def _agent_assignment(main_py: str) -> tuple[ast.Assign, bool]:
    try:
        tree = ast.parse(main_py)
    except SyntaxError as exc:
        raise ConversionError(
            f"graft anchor invalid Python: {exc.msg} at line {exc.lineno}"
        ) from exc

    matches: list[ast.Assign] = []
    statement_locations: dict[int, tuple[list[ast.stmt], int]] = {}
    for parent in ast.walk(tree):
        for _, value in ast.iter_fields(parent):
            if not isinstance(value, list):
                continue
            for index, child in enumerate(value):
                if isinstance(child, ast.stmt):
                    statement_locations[id(child)] = (value, index)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        call = node.value
        if not (
            isinstance(target, ast.Name)
            and target.id == "agent"
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "get_or_create_agent"
            and not call.keywords
            and all(isinstance(arg, ast.Name) for arg in call.args)
            and tuple(arg.id for arg in call.args) in (
                (),  # CLI export without Memory or Skills.
                ("_skill_plugins",),  # Skills enabled, Memory disabled.
                ("session_id", "user_id"),
                ("session_id", "user_id", "_skill_plugins"),
            )
        ):
            continue
        matches.append(node)

    if len(matches) != 1:
        raise ConversionError(
            "graft anchor missing (A3): expected exactly one "
            "supported agent = get_or_create_agent(...) assignment "
            "(no arguments, _skill_plugins, session_id/user_id, "
            "or session_id/user_id/_skill_plugins) "
            "(agentcore CLI codegen changed?)"
        )
    assignment = matches[0]
    body, index = statement_locations[id(assignment)]
    apply_calls = [node for node in ast.walk(tree) if _is_agent_apply(node)]
    apply_follows = index + 1 < len(body) and _is_agent_apply(body[index + 1])
    if apply_calls and (not apply_follows or len(apply_calls) != 1):
        raise ConversionError(
            "graft anchor invalid (A3): expected the tool-description apply call "
            "exactly once, immediately after the supported agent assignment"
        )
    return assignment, apply_follows


def _insert_agent_apply(main_py: str, assignment: ast.Assign) -> str:
    lines = main_py.splitlines(keepends=True)
    if assignment.end_lineno is None or assignment.end_col_offset is None:
        raise ConversionError("graft anchor missing (A3): agent assignment has no end position")
    source_line = lines[assignment.lineno - 1]
    end_line = lines[assignment.end_lineno - 1]
    if (
        source_line[: assignment.col_offset].strip()
        or (
            end_line[assignment.end_col_offset :].strip()
            and not end_line[assignment.end_col_offset :].lstrip().startswith("#")
        )
    ):
        raise ConversionError(
            "graft anchor invalid (A3): supported agent assignment must occupy "
            "its own statement line"
        )
    indent = source_line[: len(source_line) - len(source_line.lstrip())]
    before = "".join(lines[: assignment.end_lineno])
    after = "".join(lines[assignment.end_lineno :])
    separator = "" if before.endswith(("\n", "\r")) else "\n"
    return f"{before}{separator}{indent}{_AGENT_APPLY_CALL}\n{after}"


def graft_config_bundle(
    main_py: str,
    *,
    default_system_prompt: str | None = None,
    tool_description_overrides: dict[str, str] | None = None,
) -> str:
    """Insert or upgrade the owned config-bundle contract idempotently."""
    try:
        ast.parse(main_py)
    except SyntaxError as exc:
        raise ConversionError(
            f"graft anchor invalid Python: {exc.msg} at line {exc.lineno}"
        ) from exc
    match = _PROMPT_CONST_RE.search(main_py)
    if match is None:
        raise ConversionError(
            "graft anchor missing (A1): DEFAULT_SYSTEM_PROMPT constant not found "
            "(agentcore CLI codegen changed?)"
        )
    if _PROMPT_USE not in main_py and _RESOLVED_PROMPT_USE not in main_py:
        raise ConversionError(
            "graft anchor missing (A1): system_prompt=DEFAULT_SYSTEM_PROMPT "
            "construction site not found (agentcore CLI codegen changed?)"
        )
    graft = _bundle_graft(default_system_prompt, tool_description_overrides)
    if GRAFT_START in main_py:
        start = main_py.index(GRAFT_START)
        end = main_py.index(GRAFT_END, start) + len(GRAFT_END)
        grafted = main_py[:start] + graft.strip("\n") + main_py[end:]
    elif _LEGACY_GRAFT_RE.search(main_py):
        grafted = _LEGACY_GRAFT_RE.sub(graft, main_py, count=1)
    else:
        # Insert helpers immediately after the triple-quoted prompt constant.
        quote = main_py[match.end() - 3:match.end()]
        const_end = main_py.index(quote, match.end()) + 3
        grafted = main_py[:const_end] + graft + main_py[const_end:]

    grafted = grafted.replace(_PROMPT_USE, _RESOLVED_PROMPT_USE)
    # Ruling (Task 1 review, step 0): the constant body is rewritten too — the usage
    # site redirect alone leaves a vendored `__LAUNCHPAD_` placeholder sitting in the
    # fallback value, which would ship in every render that never hits the bundle.
    grafted = _rewrite_prompt_constant_body(grafted, default_system_prompt or "")
    assignment, apply_follows = _agent_assignment(grafted)
    if not apply_follows:
        grafted = _insert_agent_apply(grafted, assignment)
    return grafted


def graft_runtime_attachments(main_py: str) -> str:
    """Hydrate files and acknowledge before streaming on the checked CLI entrypoint."""
    helper = (
        ATTACHMENT_GRAFT_START + "\nLAUNCHPAD_ATTACHMENT_CONTRACT = 'v1'\n\n"
        + render_attachment_source() + ATTACHMENT_GRAFT_END + "\n\n"
    )
    # Only our complete, unmodified graft is idempotent. A partial or displaced
    # graft must not declare a contract that the entrypoint no longer implements.
    previous = main_py
    has_graft = ATTACHMENT_GRAFT_START in main_py or ATTACHMENT_GRAFT_END in main_py
    if has_graft:
        if main_py.count(helper) != 1 or main_py.count(_ATTACHMENT_INVOKE) != 1:
            raise ConversionError("attachment graft invalid (A5): partial or modified owned graft")
        main_py = main_py.replace(helper, "", 1).replace(_ATTACHMENT_INVOKE, "", 1)
    if "LAUNCHPAD_ATTACHMENT_CONTRACT" in main_py or "_launchpad_attachment_messages" in main_py:
        raise ConversionError("attachment graft invalid (A5): unrecognized existing contract")
    try:
        tree = ast.parse(main_py)
    except SyntaxError as exc:
        raise ConversionError("attachment graft anchor invalid Python") from exc
    invocations = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "invoke"
    ]
    if len(invocations) != 1:
        raise ConversionError("attachment graft anchor missing (A5): one async invoke required")
    invoke = invocations[0]
    if (
        len(invoke.decorator_list) != 1
        or ast.unparse(invoke.decorator_list[0]) != "app.entrypoint"
    ):
        raise ConversionError("attachment graft anchor missing (A5): @app.entrypoint")
    assignments = [
        node for node in invoke.body
        if isinstance(node, ast.Assign)
        and ast.unparse(node) == "prompt = _extract_prompt(payload)"
    ]
    prompt_writes = [
        node for node in ast.walk(invoke)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == "prompt"
    ]
    streams = [
        node for node in ast.walk(invoke)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "stream_async"
    ]
    if (
        len(assignments) != 1 or len(prompt_writes) != 1 or len(streams) != 1
        or ast.unparse(streams[0]) != "agent.stream_async(prompt)"
        or not any(
            isinstance(node, ast.AsyncFor) and node.iter is streams[0]
            for node in ast.walk(invoke)
        )
    ):
        raise ConversionError(
            "attachment graft anchor missing (A5): prompt extraction / stream_async"
        )
    assignment = assignments[0]
    if streams[0].lineno <= assignment.lineno or any(
        isinstance(node, (ast.Yield, ast.YieldFrom)) and node.lineno < assignment.lineno
        for node in ast.walk(invoke)
    ):
        raise ConversionError(
            "attachment graft invalid (A5): model events precede input adaptation"
        )
    lines = main_py.splitlines(keepends=True)
    if lines[assignment.lineno - 1].strip() != "prompt = _extract_prompt(payload)":
        raise ConversionError("attachment graft invalid (A5): extraction must occupy its own line")
    lines.insert(assignment.end_lineno, _ATTACHMENT_INVOKE)
    lines.insert(invoke.decorator_list[0].lineno - 1, helper)
    grafted = "".join(lines)
    if has_graft and grafted != previous:
        raise ConversionError("attachment graft invalid (A5): misplaced owned graft")
    return grafted


# ─── self-built Harness renderers (N7): actor, loop bounds, skill sources ────────
ACTOR_RE = re.compile(
    r"^(?P<indent>[ \t]*)user_id = getattr\(context, "
    r"['\"]user_id['\"], ['\"]default-user['\"]\)[ \t]*$",
    re.MULTILINE,
)
_ACTOR_LINE = (
    'user_id = str(payload.get("actor_id") or '
    'getattr(context, "user_id", "default-user"))'
)
SKILL_SOURCES_RE = re.compile(
    r"^(?P<indent>[ \t]*)s3_skill_sources = \[[^\]]*\][ \t]*$", re.MULTILINE
)
LOOP_ITER_RE = re.compile(r"(ExecutionLimitsHook\(\s*max_iterations=)\d+(,)")
LOOP_HOOK_TIMEOUT_RE = re.compile(r"^([ \t]*timeout_seconds=)\d+(,)[ \t]*$", re.MULTILINE)
LOOP_INVOKE_TIMEOUT_RE = re.compile(r"^([ \t]*timeout_seconds = )\d+[ \t]*$", re.MULTILINE)
BUILTIN_GRAFT_START = "# <launchpad-builtin-tools:v1>"
BUILTIN_GRAFT_END = "# </launchpad-builtin-tools:v1>"


def graft_actor_from_payload(main_py: str) -> str:
    """A7: the platform sends the (agent-scoped) actor as payload.actor_id on every invoke."""
    if _ACTOR_LINE in main_py:
        return main_py
    out, n = ACTOR_RE.subn(lambda m: f"{m.group('indent')}{_ACTOR_LINE}", main_py)
    if n != 1:
        raise ConversionError("graft anchor missing (A7): user_id = getattr(context, 'user_id', …)")
    return out


def render_loop_bounds(main_py: str, max_iterations: int, timeout_seconds: int) -> str:
    """A8: the export bakes the Harness loop bounds; the spec owns them here."""
    out, n1 = LOOP_ITER_RE.subn(rf"\g<1>{int(max_iterations)}\g<2>", main_py)
    out, n2 = LOOP_HOOK_TIMEOUT_RE.subn(rf"\g<1>{int(timeout_seconds)}\g<2>", out)
    out, n3 = LOOP_INVOKE_TIMEOUT_RE.subn(rf"\g<1>{int(timeout_seconds)}", out)
    if (n1, n2, n3) != (1, 1, 1):
        raise ConversionError(
            f"graft anchor missing (A8): loop bounds matched {(n1, n2, n3)}, expected (1, 1, 1)"
        )
    return out


def render_skill_sources(main_py: str, skills: list[str]) -> str:
    """A6: the S3 skill prefixes the export's fetcher resolves per request."""
    out, n = SKILL_SOURCES_RE.subn(
        lambda m: f"{m.group('indent')}s3_skill_sources = {list(skills)!r}", main_py
    )
    if n != 1:
        raise ConversionError("graft anchor missing (A6): s3_skill_sources = [...] list literal")
    return out


def graft_tool_module(
    main_py: str, *, start: str, end: str, import_line: str, tool_names: list[str]
) -> str:
    """Register tools from a bundled module on the A2 collection (shape of the KB graft)."""
    if start in main_py and end in main_py:
        return main_py
    match = _TOOLS_COLLECTION_RE.search(main_py)
    if match is None:
        raise ConversionError("graft anchor missing (A2): tools = [] collection not found")
    graft = (f"{start}\n{import_line}\n{end}\n\n{match.group(0)}\n"
             f"tools.extend([{', '.join(tool_names)}])")
    return main_py[:match.start()] + graft + main_py[match.end():]


def flatten_requirements(files: dict[str, str], platform: list[str]) -> list[str]:
    """pyproject [project].dependencies → extras not already satisfied by the
    platform's own requirement lists (the platform wins on package-name conflicts).

    `platform` must be the FULL platform contribution for the spec being built
    (`zip_runtime.platform_requirements(...)`), not just the template base list —
    otherwise a project the platform names in an extras list survives here and is
    emitted a second time into the spec.

    Consequence of "the platform wins": a source Harness whose own floor is higher
    than the platform's range loses that floor. That is the safe direction — the
    platform's range is what the runtime template's code was verified against.
    """
    pyproject = files.get("pyproject.toml", "")
    deps: list[str] = []
    in_deps = False
    for line in pyproject.splitlines():
        stripped = line.strip()
        if stripped.startswith("dependencies"):
            in_deps = True
            continue
        if in_deps:
            if stripped.startswith("]"):
                break
            entry = stripped.strip('",').strip("',")
            if entry:
                deps.append(entry)
    taken = {re.split(r"[<>=!\[ ]", req, maxsplit=1)[0].lower() for req in platform}
    return [d for d in deps
            if re.split(r"[<>=!\[ ]", d, maxsplit=1)[0].lower() not in taken]


def discover_skills(files: dict[str, str]) -> tuple[list[str], list[str]]:
    """Skill sources the exported code will fetch → `(s3_uris, other_uris)`.

    The export bakes the harness's skill bundles into module-level list literals
    (`s3_skill_sources = ["s3://…/skills/<name>/"]`, and a git equivalent), and
    resolves them at request time via its own `skills/fetcher.py`. Read from the
    code rather than only the source agent's ledger row because the code is what
    actually performs the fetch — the exec role has to allow exactly that, and a
    grant derived from a stale ledger row would be authorized for the wrong thing.
    """
    s3_uris: list[str] = []
    other: list[str] = []
    for name, content in files.items():
        if not name.endswith(".py"):
            continue
        try:
            tree = ast.parse(content)
        except SyntaxError:
            continue  # a graft anchor check elsewhere owns malformed exports
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.List):
                continue
            if not any(
                isinstance(t, ast.Name) and t.id.endswith("_skill_sources")
                for t in node.targets
            ):
                continue
            for element in node.value.elts:
                if not isinstance(element, ast.Constant) or not isinstance(
                    element.value, str
                ):
                    continue
                uri = element.value.strip()
                if not uri:
                    continue
                (s3_uris if uri.startswith("s3://") else other).append(uri)
    return sorted(set(s3_uris)), sorted(set(other))


def graft_lazy_gateway_token(client_py: str) -> str:
    """Defer the export's outbound token fetch from import time to session-open time.

    The exported ``mcp_client/client.py`` does::

        token = _get_bearer_token_launchpad_gw()          # module scope!
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return MCPClient(lambda: streamablehttp_client(url, headers=headers), ...)

    and ``main.py`` calls ``get_all_gateway_mcp_clients()`` at module scope. So the
    token is fetched before any request exists — and the workload access token the
    M2M exchange needs is a **per-request** context value (the Runtime injects it as
    a header). Measured on a live conversion: the SDK found no context token, fell
    through to its *local dev* branch and died on
    ``AccessDeniedException … CreateWorkloadIdentity``.

    ``MCPClient`` is a lazy ``ToolProvider`` — its transport callable does not run
    until ``load_tools()``, which happens inside the request — so moving just the
    fetch into that callable is enough. The connection was already lazy; only the
    header was eager.

    A remote MCP (not behind a Gateway at all — e.g. the public
    `knowledge-mcp.global.api.aws`) carries no token to defer; it instead gets the
    soft-failing client (``_LaunchpadSoftMCPClient``) so an unreachable server
    degrades to "no tools" instead of failing every request.

    Idempotent. Raises ``ConversionError`` if the anchor is gone (codegen drift),
    same posture as the other grafts: better a failed conversion than a runtime that
    reports active and reaches no tools.
    """
    if (
        GW_LAZY_TOKEN_MARK in client_py
        or f"class {GW_NO_BUNDLE_CLIENT}(" in client_py
        or f"class {GW_SOFT_CLIENT}(" in client_py
    ):
        return client_py
    if "MCPClient(" not in client_py:
        # Nothing is constructed, so there is no eager fetch to defer. Checked
        # before the anchor so "no gateway client" stays benign while "a client
        # whose shape we no longer recognise" still fails loudly below.
        return client_py

    def _replace(match: re.Match[str]) -> str:
        indent = match.group("indent")
        return (
            f"{indent}def {GW_LAZY_TOKEN_MARK}():\n"
            f"{indent}    # <launchpad-lazy-gateway-token:v1> fetched at session-open\n"
            f"{indent}    # time, not import time: the workload access token only\n"
            f"{indent}    # exists inside a request context.\n"
            f"{indent}    _token = {match.group('fetch')}\n"
            f"{indent}    return streamablehttp_client(\n"
            f"{indent}        url,\n"
            f'{indent}        headers={{"Authorization": f"Bearer {{_token}}"}}'
            f" if _token else {{}},\n"
            f"{indent}    )\n"
            f"\n"
            f"{indent}return {GW_NO_BUNDLE_CLIENT}({GW_LAZY_TOKEN_MARK}{match.group('rest')})"
        )

    grafted, count = _GW_EAGER_TOKEN_RE.subn(_replace, client_py)
    # AWS_IAM clients fetch nothing, but they need the same baggage-free wrapper.
    grafted, signed = _GW_SIGV4_CLIENT_RE.subn(
        lambda m: f"{m.group('indent')}return {GW_NO_BUNDLE_CLIENT}(lambda: "
        f"{GW_SIGV4_TRANSPORT}(", grafted,
    )
    # A remote MCP (no Gateway, no token) gets the soft-failing subclass instead —
    # a bare lazy-token wrapper would have nothing to defer, but the connection
    # itself still needs to degrade gracefully rather than crash every request.
    grafted, remote = _GW_REMOTE_CLIENT_RE.subn(
        lambda m: f"{m.group('indent')}return {GW_SOFT_CLIENT}(lambda: "
        f"streamablehttp_client({m.group('url')})", grafted,
    )
    if count + signed + remote == 0:
        raise ConversionError(
            "graft anchor missing (A4): eager 'token = _get_bearer_token_*()' + "
            "MCPClient(lambda: streamablehttp_client(url, headers=headers)) shape, nor a "
            "remote MCP client, not found in mcp_client/client.py "
            "(agentcore CLI codegen changed?)"
        )
    if _MCP_CLIENT_IMPORT not in grafted:
        raise ConversionError(
            "graft anchor missing (A4): 'from strands.tools.mcp.mcp_client import MCPClient' "
            "not found in mcp_client/client.py (agentcore CLI codegen changed?)"
        )
    return grafted.replace(
        _MCP_CLIENT_IMPORT,
        _MCP_CLIENT_IMPORT + _NO_BUNDLE_CLIENT_SOURCE + _SOFT_CLIENT_SOURCE,
        1,
    )


GW_CLIENTS_MARK = "# <launchpad-gateway-clients:v1>"
_MCP_CLIENTS_INIT_RE = re.compile(r"^mcp_clients = \[\][ \t]*$", re.MULTILINE)
_GLOB_CHARS = set("*?[")
_GW_REMOTE_CLIENT_RE = re.compile(
    r"^(?P<indent>[ \t]*)return MCPClient\(lambda: streamablehttp_client\((?P<url>'https?://[^']+')\)",
    re.MULTILINE,
)
GW_SOFT_CLIENT = "_LaunchpadSoftMCPClient"
_SOFT_CLIENT_SOURCE = f'''

class {GW_SOFT_CLIENT}({GW_NO_BUNDLE_CLIENT}):
    """A remote MCP server this runtime cannot reach degrades to "no tools" instead of
    failing every request (spec D7: knowledge-mcp.global.api.aws reachability from
    aws-cn is an N9 live check)."""

    def load_tools(self, *args, **kwargs):
        try:
            return super().load_tools(*args, **kwargs)
        except Exception as exc:
            print(f"[launchpad] MCP tools unavailable: {{type(exc).__name__}}: {{exc}}", flush=True)
            return []
'''


def _gateway_allowlist(alias: str, allowed_tools: list[str] | None) -> list[str] | None:
    """The tool names `allowedTools` selects on one Gateway alias.

    ``None`` = every tool (``*``, ``@alias`` or no allowlist at all); ``[]`` = the
    alias is not selected, so the converted runtime must not mount it either.
    """
    if allowed_tools is None or "*" in allowed_tools or f"@{alias}" in allowed_tools:
        return None
    prefix = f"@{alias}/"
    return [entry[len(prefix):] for entry in allowed_tools if entry.startswith(prefix)]


def graft_missing_gateway_clients(
    files: dict[str, str],
    harness_tools: list[dict[str, Any]],
    allowed_tools: list[str] | None,
    *,
    skip_gateway_arns: frozenset[str] = frozenset(),
    principal_carrier: bool = False,
) -> dict[str, str]:
    """Build the Gateway (and remote-MCP) clients the vendored template leaves out.

    ``principal_carrier`` (stream A3b): the SigV4 transport also puts the turn's principal
    token into the requests it signs (``launchpad_principal_carrier``, written beside the
    bundle by the Harness renderer); off, the graft is byte-identical to before.

    `mcp_client/client.py.tmpl` is an imports-only scaffold (contract A4): it imports
    `MCPClient`/`streamablehttp_client` and defines no client. This rebuilds the
    clients from the spec's attached tools, in the agentcore CLI's own export shape
    (so the lazy-token and soft-fail grafts apply unchanged), with narrowing carried
    over as Strands ``tool_filters``. An OAuth Gateway mints its M2M bearer token; an
    AWS_IAM Gateway (``outboundAuth.awsIam``) gets a SigV4-signing transport with its
    URL built from the ARN, because the platform wires only the shared Gateway's URL
    into the env. A ``remote_mcp`` tool (no Gateway at all — e.g. the public
    `knowledge-mcp.global.api.aws`) gets a plain, unauthenticated streamable-HTTP
    client that `graft_lazy_gateway_token` wraps in the soft-failing subclass.
    Gateways in ``skip_gateway_arns`` (the KB gateway, replaced by direct retrieval)
    and aliases the allowlist does not select are left out. Idempotent; a no-op when
    the export already constructs clients.
    """
    client_py = files.get("mcp_client/client.py") or ""
    if GW_CLIENTS_MARK in client_py or "MCPClient(" in client_py:
        return files
    gateways = []
    remotes: list[tuple[str, str, list[str] | None]] = []
    for tool in harness_tools:
        if tool.get("type") == "remote_mcp":
            url = ((tool.get("config") or {}).get("remoteMcp") or {}).get("url")
            if not url:
                raise ConversionError(f"remote MCP {tool.get('name')!r} has no url")
            allow = _gateway_allowlist(str(tool.get("name") or ""), allowed_tools)
            if allow != []:
                remotes.append((str(tool["name"]), str(url), allow))
            continue
        config = (tool.get("config") or {}).get("agentCoreGateway") or {}
        arn = config.get("gatewayArn")
        if tool.get("type") != "agentcore_gateway" or not arn or arn in skip_gateway_arns:
            continue
        allow = _gateway_allowlist(str(tool.get("name") or ""), allowed_tools)
        if allow == []:
            continue
        outbound = config.get("outboundAuth") or {}
        oauth = outbound.get("oauth") or {}
        if not oauth.get("providerArn") and "awsIam" not in outbound:
            raise ConversionError(
                f"gateway {tool.get('name')!r} has neither OAuth nor AWS_IAM outbound "
                "auth; the converted runtime cannot authenticate to it"
            )
        gateways.append((str(tool["name"]), arn, oauth or None, allow))
    if not gateways and not remotes:
        return files
    if "main.py" not in files or not _MCP_CLIENTS_INIT_RE.search(files["main.py"]):
        raise ConversionError(
            "graft anchor missing (A4): module-scope 'mcp_clients = []' not found "
            "(agentcore CLI codegen changed?)"
        )

    lines = [GW_CLIENTS_MARK,
             "# Rebuilt by Launchpad: the vendored template defines no Gateway clients."]
    head = ["import os", "import logging"]
    if any(allow and any(set(name) & _GLOB_CHARS for name in allow) for *_, allow in gateways):
        head.append("import re")
    signed = any(oauth is None for _, _, oauth, _ in gateways)
    if signed:
        head += ["import boto3", "import httpx", "from botocore.auth import SigV4Auth",
                 "from botocore.awsrequest import AWSRequest"]
    head += ["from mcp.client.streamable_http import streamablehttp_client",
             "from strands.tools.mcp.mcp_client import MCPClient"]
    if not all(oauth is None for _, _, oauth, _ in gateways):
        head.append("from bedrock_agentcore.identity import requires_access_token")
    getters = []
    for alias, arn, oauth, allow in gateways:
        ident = re.sub(r"\W", "_", alias)
        rest = f", prefix={alias!r}"
        if allow is not None:
            matchers = ", ".join(
                f"re.compile({fnmatch.translate(name)!r})" if set(name) & _GLOB_CHARS
                else repr(name)
                for name in allow
            )
            rest += f", tool_filters={{'allowed': [{matchers}]}}"
        if oauth is None:
            region, gateway_id = arn.split(":")[3], arn.rsplit("/", 1)[-1]
            suffix = aws_clients.dns_suffix_for_partition(aws_clients.partition_for_region(region))
            url = f"https://{gateway_id}.gateway.bedrock-agentcore.{region}.{suffix}/mcp"
            lines += [
                "",
                f"def get_{ident}_mcp_client() -> MCPClient | None:",
                f"    url = {url!r}  # AWS_IAM Gateway: SigV4 with the runtime role",
                f"    return MCPClient(lambda: {GW_SIGV4_TRANSPORT}(url, {region!r}){rest})",
            ]
            getters.append(ident)
            continue
        env_key = "GATEWAY_GATEWAY_" + arn.rsplit("/", 1)[-1].upper().replace("-", "_") + "_URL"
        provider = oauth["providerArn"].rsplit("/", 1)[-1]
        scopes = list(oauth.get("scopes") or [])
        lines += [
            "",
            "@requires_access_token(",
            f"    provider_name={provider!r},",
            f"    scopes={scopes!r},",
            '    auth_flow="M2M",',
            ")",
            f"def _get_bearer_token_{ident}(*, access_token: str):",
            "    return access_token",
            "",
            f"def get_{ident}_mcp_client() -> MCPClient | None:",
            f'    url = os.environ.get("{env_key}")',
            "    if not url:",
            f'        logger.warning("{env_key} not set — {alias} gateway tools unavailable")',
            "        return None",
            f"    token = _get_bearer_token_{ident}()",
            '    headers = {"Authorization": f"Bearer {token}"} if token else {}',
            f"    return MCPClient(lambda: streamablehttp_client(url, headers=headers){rest})",
        ]
        getters.append(ident)
    for alias, url, allow in remotes:
        ident = re.sub(r"\W", "_", alias)
        rest = f", prefix={alias!r}" + (
            f", tool_filters={{'allowed': {allow!r}}}" if allow else ""
        )
        lines += [
            "",
            f"def get_{ident}_mcp_client() -> MCPClient | None:",
            f"    return MCPClient(lambda: streamablehttp_client({url!r}){rest})",
        ]
        getters.append(ident)
    lines += ["", "def get_all_gateway_mcp_clients() -> list[MCPClient]:", "    clients = []"]
    for ident in getters:
        lines += [f"    client = get_{ident}_mcp_client()", "    if client:",
                  "        clients.append(client)"]
    lines.append("    return clients")
    # Every name the rebuilt clients use, whatever the export's scaffold already had.
    missing = [line for line in head if line.startswith(("import ", "from "))
               and line not in client_py.splitlines()]
    if "logger = logging.getLogger(__name__)" not in client_py:
        missing.append("logger = logging.getLogger(__name__)")
    transport = _SIGV4_TRANSPORT_SOURCE
    if signed and principal_carrier:
        missing.append(PRINCIPAL_CLIENT_IMPORT)
        transport = "\n\n" + _render_sigv4_transport(principal_carrier=True)
    grafted = dict(files)
    grafted["mcp_client/client.py"] = (
        (client_py.rstrip() + "\n\n" if client_py.strip() else "")
        + "\n".join(missing) + "\n"
        + (transport if signed else "")
        + "\n" + "\n".join(lines) + "\n"
    )
    grafted.setdefault("mcp_client/__init__.py", "")
    grafted["main.py"] = _MCP_CLIENTS_INIT_RE.sub(
        "mcp_clients = []\n"
        "from mcp_client.client import get_all_gateway_mcp_clients  "
        "# <launchpad-gateway-clients:v1>\n"
        "mcp_clients += get_all_gateway_mcp_clients()",
        files["main.py"], count=1,
    )
    return grafted


def graft_gateway_softfail(main_py: str) -> str:
    """Make the exported module-scope gateway-client construction fail soft.

    The export ends up with, at import time:

        mcp_clients += get_all_gateway_mcp_clients()

    which raises when the outbound M2M token cannot be fetched. A crash there is
    worse than missing tools: the runtime never starts, the deploy pipeline's
    health signal still reports the agent `active`, and every invoke fails.

    Idempotent — re-grafting an already-grafted bundle is a no-op.
    """
    if GW_SOFTFAIL_START in main_py:
        return main_py
    match = _GW_CLIENTS_CALL_RE.search(main_py)
    if match is None:
        raise ConversionError(
            "graft anchor missing (A4): module-scope 'mcp_clients += "
            "get_all_gateway_mcp_clients()' not found while the export DOES carry "
            "mcp_client/client.py (agentcore CLI codegen changed?)"
        )
    indent = match.group("indent")
    replacement = (
        f"{indent}{GW_SOFTFAIL_START}\n"
        f"{indent}try:\n"
        f"{indent}    {match.group('call').strip()}\n"
        f"{indent}except Exception as _launchpad_gw_exc:\n"
        f"{indent}    print(\n"
        f"{indent}        '[launchpad] gateway MCP clients unavailable: '\n"
        f"{indent}        f'{{type(_launchpad_gw_exc).__name__}}: {{_launchpad_gw_exc}}',\n"
        f"{indent}        flush=True,\n"
        f"{indent}    )\n"
        f"{indent}{GW_SOFTFAIL_END}"
    )
    return main_py[: match.start()] + replacement + main_py[match.end():]


# ─── memory grafts (N8; docs/cn/interfaces/memory-runtime.md §6 steps 2–3) ───────
# The bundle carries N6's library as `launchpad_memory.py` (harness_local). These grafts
# wire it into the vendored entrypoint: A11 appends the library's hooks to the Harness
# `Agent(hooks=[ExecutionLimitsHook(…)])` list, A12 prepends `recall_context(actor, prompt)`
# to the system prompt each turn. Both are no-ops at runtime without LAUNCHPAD_MEMORY_ID.
MEMORY_GRAFT_START = "# <launchpad-memory:v1>"
MEMORY_GRAFT_END = "# </launchpad-memory:v1>"
MEMORY_HOOKS_MARK = "# <launchpad-memory-hooks:v1>"
MEMORY_RECALL_START = "# <launchpad-memory-recall:v1>"
MEMORY_RECALL_END = "# </launchpad-memory-recall:v1>"
_MEMORY_IMPORT_ANCHOR = "from memory.session import get_memory_session_manager"  # A10
_MEMORY_HOOKS_CALL = "*make_memory_hooks(user_id, session_id)"
_MEMORY_FACTORY_PARAMS = ("session_id", "user_id")
_MEMORY_PROMPT_LINE = "prompt = _extract_prompt(payload)"
_MEMORY_IMPORT_GRAFT = f'''
{MEMORY_GRAFT_START}
# docs/cn/interfaces/memory-runtime.md §6: the bundled library is inert without
# LAUNCHPAD_MEMORY_ID; one that cannot be imported degrades to no memory, like the
# memory/session.py shim does.
try:
    from launchpad_memory import make_memory_hooks, recall_context
except Exception as _launchpad_memory_exc:
    print(f"[launchpad] memory library unavailable: {{type(_launchpad_memory_exc).__name__}}: "
          f"{{_launchpad_memory_exc}}", flush=True)

    def make_memory_hooks(actor_id, session_id):
        return []

    def recall_context(actor_id, query, top_k=3):
        return ""


def _launchpad_recall_query(prompt):
    """The user text of this turn ('' for tool results and non-text payloads)."""
    if isinstance(prompt, str):
        return prompt
    if isinstance(prompt, list):
        for message in reversed(prompt):
            if isinstance(message, dict) and message.get("role") == "user":
                texts = [block.get("text") for block in message.get("content", [])
                         if isinstance(block, dict) and block.get("text")]
                if texts:
                    return "\\n".join(texts)
    return ""
{MEMORY_GRAFT_END}
'''
_MEMORY_RECALL_BLOCK = (
    "{indent}" + MEMORY_RECALL_START + "\n"
    "{indent}_launchpad_query = _launchpad_recall_query(prompt)\n"
    "{indent}_launchpad_known = recall_context(user_id, _launchpad_query) "
    'if _launchpad_query else ""\n'
    # The Harness caches one Agent per session/actor (unlike the HTTP strands_agent
    # runtime, which builds one per turn), so the reset keys on the USER turn
    # (_launchpad_query), not on recall presence (_launchpad_known) — otherwise a
    # recall-empty user turn would inherit the previous turn's stale recall text
    # still sitting on the cached Agent's system_prompt. A tool-result continuation
    # turn (_launchpad_query == "") skips the reset and leaves the loop's context
    # untouched.
    "{indent}if _launchpad_query:\n"
    "{indent}    agent.system_prompt = resolve_system_prompt() + (\n"
    '{indent}        f"\\n\\nKnown user context:\\n{{_launchpad_known}}" if _launchpad_known '
    'else ""\n'
    "{indent}    )\n"
    "{indent}" + MEMORY_RECALL_END + "\n"
)


def _parse(main_py: str, anchor: str) -> ast.Module:
    try:
        return ast.parse(main_py)
    except SyntaxError as exc:
        raise ConversionError(
            f"graft anchor invalid Python ({anchor}): {exc.msg} at line {exc.lineno}"
        ) from exc


def _offset(main_py: str, lineno: int, col: int) -> int:
    lines = main_py.splitlines(keepends=True)
    return sum(len(line) for line in lines[: lineno - 1]) + col


def _agent_hooks_list(main_py: str) -> ast.List:
    """A11: the `hooks=[…]` list literal of the one `Agent(...)` call inside a factory whose
    parameters start with `(session_id, user_id, …)` — the names the graft scopes memory to."""
    tree = _parse(main_py, "A11")
    found: list[ast.List] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        params = tuple(a.arg for a in fn.args.args)
        if params[: len(_MEMORY_FACTORY_PARAMS)] != _MEMORY_FACTORY_PARAMS:
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "Agent"):
                continue
            hooks = [kw.value for kw in node.keywords if kw.arg == "hooks"]
            if len(hooks) == 1 and isinstance(hooks[0], ast.List):
                found.append(hooks[0])
    if len(found) != 1:
        raise ConversionError(
            "graft anchor missing (A11): expected exactly one Agent(..., hooks=[...]) list "
            "literal inside get_or_create_agent(session_id, user_id, …) "
            "(agentcore CLI codegen changed?)"
        )
    return found[0]


def graft_memory_import(main_py: str) -> str:
    """Import the bundled library (soft) and define the recall-query helper, right after
    the A10 session import so both grafts below resolve their names."""
    if MEMORY_GRAFT_START in main_py:
        return main_py
    if main_py.count(_MEMORY_IMPORT_ANCHOR) != 1:
        raise ConversionError(
            f"graft anchor missing (A10): {_MEMORY_IMPORT_ANCHOR!r} not found once "
            "(agentcore CLI codegen changed?)"
        )
    return main_py.replace(
        _MEMORY_IMPORT_ANCHOR, _MEMORY_IMPORT_ANCHOR + "\n" + _MEMORY_IMPORT_GRAFT.rstrip("\n"), 1
    )


def graft_memory_hooks(main_py: str) -> str:
    """A11: APPEND `*make_memory_hooks(user_id, session_id)` to the Harness
    `Agent(hooks=[ExecutionLimitsHook(…)])` list — never replace it (the loop bounds stay)."""
    if MEMORY_HOOKS_MARK in main_py or _MEMORY_HOOKS_CALL in main_py:
        return main_py
    hooks = _agent_hooks_list(main_py)
    if hooks.lineno == hooks.end_lineno or hooks.end_lineno is None or hooks.end_col_offset is None:
        raise ConversionError(
            "graft anchor invalid (A11): hooks=[...] must span several lines with the "
            "closing bracket on its own line (agentcore CLI codegen changed?)"
        )
    lines = main_py.splitlines(keepends=True)
    bracket_line = lines[hooks.end_lineno - 1]
    bracket_indent = bracket_line[: len(bracket_line) - len(bracket_line.lstrip())]
    if bracket_line[: hooks.end_col_offset - 1].strip():
        raise ConversionError(
            "graft anchor invalid (A11): the closing bracket of hooks=[...] shares its line"
        )
    out = main_py
    if hooks.elts:
        last = hooks.elts[-1]
        last_end = _offset(main_py, last.end_lineno, last.end_col_offset)
        bracket_at = _offset(main_py, hooks.end_lineno, hooks.end_col_offset - 1)
        between = re.sub(r"#[^\n]*", "", main_py[last_end:bracket_at])
        if not between.strip().endswith(","):
            out = main_py[:last_end] + "," + main_py[last_end:]
        first_line = lines[last.lineno - 1]
        elt_indent = first_line[: len(first_line) - len(first_line.lstrip())]
    else:
        elt_indent = bracket_indent + "    "
    extra = elt_indent[len(bracket_indent):] if elt_indent.startswith(bracket_indent) else "    "
    tree_after = _agent_hooks_list(out)  # offsets after the optional comma insertion
    bracket_at = _offset(out, tree_after.end_lineno, tree_after.end_col_offset - 1)
    graft = f"{extra}{_MEMORY_HOOKS_CALL},  {MEMORY_HOOKS_MARK}\n{bracket_indent}"
    return out[:bracket_at] + graft + out[bracket_at:]


def _recall_anchor(main_py: str) -> tuple[ast.AsyncFunctionDef, ast.Assign]:
    """A12: inside the A5 entrypoint, `agent = get_or_create_agent(session_id, user_id, …)`
    precedes `prompt = _extract_prompt(payload)`; both on their own statement lines."""
    tree = _parse(main_py, "A12")
    invocations = [
        node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "invoke"
    ]
    if len(invocations) != 1:
        raise ConversionError("graft anchor missing (A12): one async invoke required")
    invoke = invocations[0]
    prompts = [
        node for node in invoke.body
        if isinstance(node, ast.Assign) and ast.unparse(node) == _MEMORY_PROMPT_LINE
    ]
    agents = [
        node for node in invoke.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "agent"
        and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "get_or_create_agent"
        and tuple(ast.unparse(a) for a in node.value.args)[:2] == _MEMORY_FACTORY_PARAMS
    ]
    if len(prompts) != 1 or len(agents) != 1 or agents[0].lineno >= prompts[0].lineno:
        raise ConversionError(
            "graft anchor missing (A12): expected `agent = get_or_create_agent(session_id, "
            "user_id, …)` before `prompt = _extract_prompt(payload)` in invoke "
            "(agentcore CLI codegen changed?)"
        )
    return invoke, prompts[0]


def graft_memory_recall(main_py: str) -> str:
    """A12: `recall_context(actor, prompt)` prepended to the system prompt each turn, exactly
    as strands_agent/main.py.tmpl does (advisory text; '' when memory is off)."""
    if MEMORY_RECALL_START in main_py:
        return main_py
    if "def resolve_system_prompt()" not in main_py:
        raise ConversionError(
            "graft order (A12): the config-bundle graft (A1) must run before the memory recall "
            "graft — resolve_system_prompt() is not defined"
        )
    _, prompt = _recall_anchor(main_py)
    lines = main_py.splitlines(keepends=True)
    source_line = lines[prompt.lineno - 1]
    if source_line.strip() != _MEMORY_PROMPT_LINE:
        raise ConversionError("graft anchor invalid (A12): extraction must occupy its own line")
    indent = source_line[: len(source_line) - len(source_line.lstrip())]
    lines.insert(prompt.end_lineno, _MEMORY_RECALL_BLOCK.format(indent=indent))
    return "".join(lines)


def graft_memory(main_py: str) -> str:
    """Import (A10) + hooks (A11) + recall (A12); idempotent as a whole."""
    return graft_memory_recall(graft_memory_hooks(graft_memory_import(main_py)))


# ── stream A3b: the per-user principal carrier in a Harness bundle ──────────────────
PRINCIPAL_MODULE_NAME = "launchpad_principal_carrier"
PRINCIPAL_IMPORT_MARK = "# <launchpad-principal-import:v1>"
PRINCIPAL_CAPTURE_MARK = "# <launchpad-principal-capture:v1>"
PRINCIPAL_CLIENT_IMPORT = (
    f"from {PRINCIPAL_MODULE_NAME} import _launchpad_with_principal  "
    "# <launchpad-principal-transport:v1>"
)


PRINCIPAL_UNSCOPED_NAME = "_launchpad_invoke_unscoped"


def graft_principal_carrier(main_py: str) -> str:
    """The carrier's import (after the memory import graft, A10) and a turn-scoped
    entrypoint: the generated ``invoke`` keeps its body under ``_launchpad_invoke_unscoped``
    and the new ``@app.entrypoint invoke`` runs it inside ``launchpad_principal_turn`` — the
    token is set when the turn starts and cleared in a ``finally`` when it ends (review D1).
    Idempotent. Runs after every other graft (they anchor on ``invoke``'s body). The
    bundle's Agent already runs tools with a ``SequentialToolExecutor`` (the vendored
    template), which T10 needs."""
    if PRINCIPAL_IMPORT_MARK in main_py:
        return main_py
    if main_py.count(MEMORY_GRAFT_END) != 1:
        raise ConversionError(
            "graft order (principal carrier): the memory import graft (A10) must run first")
    tree = _parse(main_py, "principal carrier")
    invoke = next((fn for fn in tree.body if isinstance(fn, ast.AsyncFunctionDef)
                   and fn.name == "invoke"), None)
    entry = None if invoke is None else next(
        (d for d in invoke.decorator_list if ast.unparse(d) == "app.entrypoint"), None)
    if invoke is None or not invoke.body or entry is None or len(invoke.decorator_list) != 1:
        raise ConversionError(
            "graft anchor missing (principal carrier): no `@app.entrypoint async def invoke` "
            "(agentcore CLI codegen changed?)")
    if not any(isinstance(n, ast.Yield | ast.YieldFrom) for n in ast.walk(invoke)):
        raise ConversionError(
            "graft anchor changed (principal carrier): `invoke` is no async generator")
    args = [a.arg for a in invoke.args.args]
    if invoke.args.vararg or invoke.args.kwarg or invoke.args.kwonlyargs or not args \
            or args[0] != "payload":
        raise ConversionError(
            "graft anchor changed (principal carrier): `invoke` must take (payload, ...)")
    lines = main_py.splitlines(keepends=True)
    def_line = lines[invoke.lineno - 1]
    if "async def invoke(" not in def_line or entry.lineno != invoke.lineno - 1:
        raise ConversionError(
            "graft anchor changed (principal carrier): unexpected `invoke` header layout")
    params = ", ".join(args)
    wrapper = (
        "\n\n@app.entrypoint  # <launchpad-principal-turn:v1>\n"
        f"async def invoke({params}):\n"
        "    async for event in launchpad_principal_turn(\n"
        f"            payload, {PRINCIPAL_UNSCOPED_NAME}({params})):  {PRINCIPAL_CAPTURE_MARK}\n"
        "        yield event\n"
    )
    end = invoke.end_lineno
    tail = "".join(lines[end:])
    head = lines[: entry.lineno - 1]
    body = [def_line.replace("async def invoke(", f"async def {PRINCIPAL_UNSCOPED_NAME}(", 1),
            *lines[invoke.lineno: end]]
    if body and not body[-1].endswith("\n"):
        body[-1] += "\n"
    main_py = "".join(head) + "".join(body) + wrapper + tail
    return main_py.replace(
        MEMORY_GRAFT_END,
        MEMORY_GRAFT_END + f"\nfrom {PRINCIPAL_MODULE_NAME} import launchpad_principal_turn  "
        f"{PRINCIPAL_IMPORT_MARK}", 1)
