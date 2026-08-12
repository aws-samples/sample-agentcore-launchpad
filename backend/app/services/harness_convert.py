"""Harness → runtime conversion: export via the agentcore CLI, graft the
launchpad config-bundle contract, and materialize an AgentSpec code_bundle.

Why the graft is mandatory: the exported main.py bakes DEFAULT_SYSTEM_PROMPT
as a constant and never reads get_config_bundle() — deployed as-is, config-
bundle A/B experiments would no-op exactly as they do against the managed
harness (the trap this feature exists to remove). A conversion whose graft
anchors are missing FAILS instead of shipping a silently non-A/B-able agent.

Shared-Gateway MCP **is** wired: the converted spec carries the source Harness's
gateway ToolRefs (so the exec role keeps its AgentCore Identity grants and the
invoke chain sends the `runtimeUserId` that makes a workload token exist),
`discover_env` resolves the `GATEWAY_*_URL` the export reads, and
`graft_gateway_softfail` wraps the export's module-scope client construction so a
token failure degrades to "no gateway tools" instead of crashing the runtime at
import. The KB gateway is still replaced rather than wired — conversion grafts
direct `kb_search`/`kb_deep_search` tools, and wiring the KB gateway URL as well
would give the runtime two duplicate retrieval surfaces.
"""

import ast
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.config import DATA_DIR
from app.core.errors import AppError
from app.schemas.agent import AgentSpec, KnowledgeBaseRef, MemoryConfig, ToolRef
from app.schemas.requirements import resolve_pins
from app.services.workspace import WorkspaceContext
from app.templates.kb_support import (
    KB_DEEP_TOOL_NAME,
    KB_TOOL_NAME,
    kb_deep_tool_description,
    kb_prompt_section,
    kb_tool_description,
    mounted_kb_refs,
    render_direct_kb_source,
)

EXPORT_TIMEOUT_S = 120
SCRATCH_PROJECT = "harnessexport"
_SCRATCH_DIR = DATA_DIR / "harness-export"
MANAGED_AGENTCORE_CLI = (
    DATA_DIR / "agentcore-cli" / "node_modules" / ".bin" / "agentcore"
)

# deterministic codegen anchors of the pinned CLI (0.21.1)
_PROMPT_CONST_RE = re.compile(
    r'^DEFAULT_SYSTEM_PROMPT\s*=\s*(?:"""|\'\'\')', re.MULTILINE
)
_PROMPT_USE = "system_prompt=DEFAULT_SYSTEM_PROMPT"
_RESOLVED_PROMPT_USE = "system_prompt=resolve_system_prompt()"
_AGENT_APPLY_CALL = "_launchpad_apply_tool_descriptions(agent)"
_ENV_KEY_RE = re.compile(r'os\.(?:environ\.get|getenv)\(\s*["\']([A-Z0-9_]+)["\']')
_TOOLS_COLLECTION_RE = re.compile(r"^tools = \[\]\s*$", re.MULTILINE)

GRAFT_START = "# <launchpad-config-bundle:v2>"
GRAFT_END = "# </launchpad-config-bundle:v2>"
KB_GRAFT_START = "# <launchpad-direct-kb:v1>"
KB_GRAFT_END = "# </launchpad-direct-kb:v1>"
GW_SOFTFAIL_START = "# <launchpad-gateway-softfail:v1>"
GW_SOFTFAIL_END = "# </launchpad-gateway-softfail:v1>"
GW_LAZY_TOKEN_MARK = "_launchpad_lazy_gateway_transport"

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
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext as _LPContext

_LAUNCHPAD_DEFAULT_SYSTEM_PROMPT = {prompt_default}
_LAUNCHPAD_DEFAULT_TOOL_DESCRIPTIONS = {tool_defaults}


def _launchpad_config_bundle():
    """Active Launchpad config bundle for this request ({{}} when none routed)."""
    try:
        return _LPContext.get_config_bundle() or {{}}
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


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    # Callers assemble argv from fixed CLI verbs; values never enter a shell.
    return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
        cmd, cwd=cwd, capture_output=True, text=True, timeout=EXPORT_TIMEOUT_S
    )


def resolve_agentcore_cli() -> str:
    """Return the repository-managed CLI path or the stable API error boundary."""
    if not MANAGED_AGENTCORE_CLI.is_file() or not os.access(
        MANAGED_AGENTCORE_CLI, os.X_OK
    ):
        raise AppError(
            "agent.convert_cli_missing",
            "the managed AgentCore CLI is missing or unusable; run `make bootstrap` "
            f"to install it at {MANAGED_AGENTCORE_CLI}",
            status_code=502,
        )
    return str(MANAGED_AGENTCORE_CLI)


def _run_agentcore(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    try:
        return _run([resolve_agentcore_cli(), *args], cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(
            f"agentcore CLI timed out after {EXPORT_TIMEOUT_S} seconds"
        ) from exc
    except OSError as exc:
        raise AppError(
            "agent.convert_cli_missing",
            "the managed AgentCore CLI is missing or unusable; run `make bootstrap`",
            status_code=502,
        ) from exc


def _last_json(stdout: str) -> dict[str, Any]:
    """The CLI prints the result object on one line, sometimes followed by
    update notices — take the last parseable JSON line."""
    for line in reversed([ln for ln in stdout.splitlines() if ln.strip()]):
        try:
            body = json.loads(line)
        except ValueError:
            continue
        if isinstance(body, dict):
            return body
    raise ConversionError(f"agentcore CLI returned no JSON: {stdout[-300:]}")


def ensure_scratch_project() -> Path:
    """One reusable agentcore project dir — the CLI refuses to export
    outside a project cwd."""
    project = _SCRATCH_DIR / SCRATCH_PROJECT
    if (project / "agentcore").exists() or (project / "agentcore.json").exists() \
            or project.exists():
        return project
    _SCRATCH_DIR.mkdir(parents=True, exist_ok=True)
    proc = _run_agentcore(
        ["create", "--project-name", SCRATCH_PROJECT, "--no-agent", "--json"],
        cwd=_SCRATCH_DIR,
    )
    body = _last_json(proc.stdout)
    if not body.get("success"):
        raise ConversionError(f"scratch project creation failed: {body.get('error')}")
    project_path = body.get("projectPath")
    if not isinstance(project_path, str) or not project_path:
        raise ConversionError("scratch project creation returned no projectPath")
    return Path(project_path)


def export_harness(harness_arn: str) -> dict[str, str]:
    """Run the CLI export; return {relpath: content} for the generated project.

    Exports into a **unique, immediately-discarded** target agent name. The CLI
    derives `<harnessName>Agent` by default and refuses to overwrite it, so with
    the default name the shared scratch project accumulates one directory per
    harness and every *second* conversion of the same harness dies with
    `A runtime agent named "…" already exists`. A per-call name also keeps two
    concurrent conversions of different harnesses from tripping over each other's
    export directory.

    The generated tree is read into memory and is not an artifact of record — the
    spec's `code_bundle` is — so it is removed once read.
    """
    project = ensure_scratch_project()
    target = f"lpexport{uuid4().hex[:10]}Agent"
    proc = _run_agentcore(
        [
            "export",
            "harness",
            "--arn",
            harness_arn,
            "--target-agent-name",
            target,
            "--build",
            "CodeZip",
            "--json",
        ],
        cwd=project,
    )
    body = _last_json(proc.stdout)
    if not body.get("success"):
        raise ConversionError(f"harness export failed: {body.get('error')}")
    exported_path = body.get("agentPath")
    if not isinstance(exported_path, str) or not exported_path:
        raise ConversionError("harness export returned no agentPath")
    agent_path = Path(exported_path)
    files: dict[str, str] = {}
    try:
        for path in agent_path.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(agent_path).as_posix()
            if rel.startswith(".") or rel.endswith((".md", ".gitignore")):
                continue  # docs/git housekeeping — not runtime source
            files[rel] = path.read_text(encoding="utf-8")
    finally:
        shutil.rmtree(agent_path, ignore_errors=True)
    if "main.py" not in files:
        raise ConversionError("export produced no main.py")
    return files


def has_config_bundle_graft(main_py: str) -> bool:
    """Whether source contains a Launchpad-owned prompt bundle contract."""
    return (
        GRAFT_START in main_py
        or (
            "Launchpad platform contract: config bundles" in main_py
            and "def resolve_system_prompt()" in main_py
        )
    )


def graft_direct_kb_tools(main_py: str) -> str:
    """Register the materialized direct KB tools on an exported Harness."""
    if KB_GRAFT_START in main_py and KB_GRAFT_END in main_py:
        return main_py
    match = _TOOLS_COLLECTION_RE.search(main_py)
    if match is None:
        raise ConversionError(
            "direct KB graft anchor missing: tools = [] collection not found "
            "(agentcore CLI codegen changed?)"
        )
    graft = f"""{KB_GRAFT_START}
from launchpad_kb_tools import kb_deep_search, kb_search
{KB_GRAFT_END}

{match.group(0)}
tools.extend([kb_search, kb_deep_search])"""
    return main_py[:match.start()] + graft + main_py[match.end():]


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
            and len(call.args) in (2, 3)
            and not call.keywords
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == "session_id"
            and isinstance(call.args[1], ast.Name)
            and call.args[1].id == "user_id"
            and (
                len(call.args) == 2
                or (
                    isinstance(call.args[2], ast.Name)
                    and call.args[2].id == "_skill_plugins"
                )
            )
        ):
            continue
        matches.append(node)

    if len(matches) != 1:
        raise ConversionError(
            "graft anchor missing: expected exactly one "
            "supported agent = get_or_create_agent(session_id, user_id"
            "[, _skill_plugins]) assignment "
            "(agentcore CLI codegen changed?)"
        )
    assignment = matches[0]
    body, index = statement_locations[id(assignment)]
    apply_calls = [node for node in ast.walk(tree) if _is_agent_apply(node)]
    apply_follows = index + 1 < len(body) and _is_agent_apply(body[index + 1])
    if apply_calls and (not apply_follows or len(apply_calls) != 1):
        raise ConversionError(
            "graft anchor invalid: expected the tool-description apply call "
            "exactly once, immediately after the supported agent assignment"
        )
    return assignment, apply_follows


def _insert_agent_apply(main_py: str, assignment: ast.Assign) -> str:
    lines = main_py.splitlines(keepends=True)
    if assignment.end_lineno is None or assignment.end_col_offset is None:
        raise ConversionError("graft anchor missing: agent assignment has no end position")
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
            "graft anchor invalid: supported agent assignment must occupy "
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
            "graft anchor missing: DEFAULT_SYSTEM_PROMPT constant not found "
            "(agentcore CLI codegen changed?)"
        )
    if _PROMPT_USE not in main_py and _RESOLVED_PROMPT_USE not in main_py:
        raise ConversionError(
            "graft anchor missing: system_prompt=DEFAULT_SYSTEM_PROMPT "
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
    assignment, apply_follows = _agent_assignment(grafted)
    if not apply_follows:
        grafted = _insert_agent_apply(grafted, assignment)
    return grafted


def _gateway_url_for(key: str, resources: Any) -> str | None:
    """The shared-Gateway URL a `GATEWAY_<id>_URL` env key is asking for.

    The agentcore CLI bakes the gateway id into the key, upper-cased with
    hyphens turned into underscores (live export:
    `GATEWAY_GATEWAY_LAUNCHPAD_GW_A1B2C3D4E5_URL` for
    `launchpad-gw-a1b2c3d4e5`), so the key is matched back against the ids the
    bootstrap config knows.

    The KB gateway is deliberately NOT resolved here: conversion replaces the
    Harness KB gateway with grafted direct-retrieval tools, so wiring its URL as
    well would give the converted runtime two duplicate retrieval surfaces.
    """
    gateway_id = str(resources.get("gateway_id") or "")
    gateway_url = str(resources.get("gateway_url") or "")
    if not (gateway_id and gateway_url):
        return None
    return gateway_url if gateway_id.upper().replace("-", "_") in key else None


def discover_env(
    files: dict[str, str], workspace: WorkspaceContext
) -> dict[str, str | None]:
    """Env keys the exported code reads → wired value or None (degrades).

    Both the launchpad memory id and the shared Gateway URL are wired. The
    Gateway URL used to be withheld because the exported client crashes at
    *import* when the URL is set and the M2M token fetch fails — that is real
    (`mcp_client/client.py` calls `requires_access_token`, which raises inside a
    container when no workload access token is present, from a module-scope
    `mcp_clients += get_all_gateway_mcp_clients()`). Two changes make wiring it
    safe rather than reworded:

    - the converted spec now carries the source Harness's gateway ToolRefs, so the
      exec role gets its identity grants and `runtimeUserId` is sent on invoke
      (which is what makes a workload token exist at all); and
    - `graft_gateway_softfail` wraps that module-scope call, so a token failure
      degrades to "no gateway tools" instead of taking the runtime down.

    The KB gateway's own `GATEWAY_*_URL` stays unset on purpose — conversion
    replaces it with grafted direct-retrieval tools.
    """
    resources = workspace.resources
    keys: set[str] = set()
    for content in files.values():
        keys.update(_ENV_KEY_RE.findall(content))
    env: dict[str, str | None] = {}
    for key in sorted(keys):
        if key in ("AWS_REGION", "AWS_DEFAULT_REGION"):
            continue  # runtime-provided
        if key.startswith("MEMORY_MEMORY_") and resources.get("memory_id"):
            env[key] = resources["memory_id"]
        elif key.startswith("GATEWAY_") and key.endswith("_URL"):
            env[key] = _gateway_url_for(key, resources)
        else:
            env[key] = None
    return env


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

    Idempotent. Raises ``ConversionError`` if the anchor is gone (codegen drift),
    same posture as the other grafts: better a failed conversion than a runtime that
    reports active and reaches no tools.
    """
    if GW_LAZY_TOKEN_MARK in client_py:
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
            f"{indent}return MCPClient({GW_LAZY_TOKEN_MARK}{match.group('rest')})"
        )

    grafted, count = _GW_EAGER_TOKEN_RE.subn(_replace, client_py)
    if count == 0:
        raise ConversionError(
            "graft anchor missing: eager 'token = _get_bearer_token_*()' + "
            "MCPClient(lambda: streamablehttp_client(url, headers=headers)) shape not "
            "found in mcp_client/client.py (agentcore CLI codegen changed?)"
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
            "graft anchor missing: module-scope 'mcp_clients += "
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


def flatten_requirements(files: dict[str, str], platform: list[str]) -> list[str]:
    """pyproject [project].dependencies → extras not already satisfied by the
    platform's own requirement lists (the platform wins on package-name conflicts).

    `platform` must be the FULL platform contribution for the spec being built
    (`zip_runtime.platform_requirements(...)`), not just the template base list —
    otherwise a project the platform names in an extras list (Mantle's `openai`)
    survives here and is emitted a second time into the spec.

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


def conversion_platform_inputs(source_agent: Any) -> tuple[str, str, str]:
    """`(method, model_source, protocol)` the converted spec will carry.

    The caller needs these *before* the spec exists, to look up the platform
    requirement list to resolve pins against. Shared with `build_conversion_spec`
    below so the two cannot derive a different `model_source` — a disagreement
    there would resolve pins against a platform list the deploy never uses, which
    is the whole failure this indirection exists to prevent.
    """
    source_spec = source_agent.spec or {}
    return (
        "zip_runtime",  # conversion always targets the zip runtime
        source_spec.get("model_source")
        or AgentSpec.model_fields["model_source"].default,
        "http",  # conversion never emits an A2A runtime
    )


def build_conversion_spec(
    source_agent: Any, files: dict[str, str], platform: list[str],
    new_name: str, workspace: WorkspaceContext,
) -> AgentSpec:
    grafted = dict(files)
    source_spec = source_agent.spec or {}
    # Gateway attachments must survive the conversion or the runtime reports READY
    # and then reaches no tools: agent_iam gates the AgentCore Identity statements
    # on a gateway/mcp ToolRef being present, and the invoke chain gates
    # `runtimeUserId` — without which the Runtime injects no workload token — on the
    # same field. Same failure shape as the dropped `spec.skills` (see the note
    # below): deploy succeeds, invoke is silently tool-less.
    gateway_tools = [
        ToolRef(**ref)
        for ref in (source_spec.get("tools") or [])
        if isinstance(ref, dict) and ref.get("type") in ("gateway", "mcp")
    ]
    kb_refs = [
        KnowledgeBaseRef(**ref)
        for ref in (source_spec.get("knowledge_bases") or [])
    ]
    kbs = mounted_kb_refs(kb_refs)
    source_prompt = source_spec.get("system_prompt")
    prompt_default = source_prompt
    source_tool_defaults = dict(source_spec.get("tool_description_overrides") or {})
    tool_defaults = source_tool_defaults
    if kbs:
        prompt_default = str(source_prompt or "") + kb_prompt_section(kbs)
        tool_defaults = {
            KB_TOOL_NAME: kb_tool_description(kbs),
            KB_DEEP_TOOL_NAME: kb_deep_tool_description(kbs),
            **source_tool_defaults,
        }
        grafted["launchpad_kb_tools.py"] = render_direct_kb_source(kbs)
        grafted["main.py"] = graft_direct_kb_tools(files["main.py"])
    grafted["main.py"] = graft_config_bundle(
        grafted["main.py"],
        default_system_prompt=prompt_default,
        tool_description_overrides=tool_defaults,
    )
    if "mcp_client/client.py" in grafted:
        # Only when the export actually carries a gateway client. Its absence is
        # benign (no gateway attached); the anchors missing while the client IS
        # present means the codegen changed, which both grafts reject.
        grafted["mcp_client/client.py"] = graft_lazy_gateway_token(
            grafted["mcp_client/client.py"]
        )
        grafted["main.py"] = graft_gateway_softfail(grafted["main.py"])
    env_contract = discover_env(grafted, workspace)
    wired = {k: v for k, v in env_contract.items() if v is not None}
    notes = {"system_prompt": "wired (config-bundle override grafted)",
             "inline_tools": "carried verbatim"}
    if kbs:
        notes["knowledge_bases"] = (
            "wired (direct kb_search + kb_deep_search; Harness KB Gateway replaced)"
        )
    # Skill bundles must land in spec.skills or the exec role is never granted S3
    # read for them (agent_iam gates that statement on spec.skills), and the
    # exported code — which fetches the prefixes it baked in at request time —
    # fails with AccessDenied at INVOKE while the deploy itself reports success.
    code_s3_skills, code_other_skills = discover_skills(grafted)
    skills = sorted({*(source_spec.get("skills") or []), *code_s3_skills})
    if skills:
        notes["skills"] = (
            f"wired ({len(skills)} bundle(s) fetched from S3 at request time; "
            "exec role granted read on those prefixes)"
        )
    if code_other_skills:
        # Non-S3 sources (e.g. git) need no S3 grant, but the runtime must be able
        # to reach them — say so rather than implying the whole capability is wired.
        notes["skills_non_s3"] = (
            f"not verified — exported code fetches {len(code_other_skills)} "
            "non-S3 skill source(s); network egress for those is unverified"
        )
    if gateway_tools:
        notes["gateway_tools"] = (
            f"wired ({len(gateway_tools)} attachment(s) carried; exec role keeps its "
            "AgentCore Identity grants and invoke sends runtimeUserId so the "
            "exported MCP client can mint its outbound token)"
        )
    for key, value in env_contract.items():
        # A GATEWAY_* key belongs to the shared Gateway (now wired) or to the KB
        # gateway (deliberately replaced by grafted direct-retrieval tools); the
        # resolved value is what distinguishes them.
        label = "memory" if key.startswith("MEMORY_") else (
            ("gateway" if value is not None else "kb_gateway")
            if key.startswith("GATEWAY_") else key.lower()
        )
        notes[label] = (
            f"wired ({key})" if value is not None
            else f"not wired — {key} unset; exported code degrades gracefully"
        )
    return AgentSpec(
        name=new_name,
        method="zip_runtime",
        model_id=source_spec.get("model_id") or AgentSpec.model_fields["model_id"].default,
        model_source=conversion_platform_inputs(source_agent)[1],
        system_prompt=prompt_default or "(baked into exported code)",
        tool_description_overrides=tool_defaults,
        tools=gateway_tools,
        skills=skills,
        # The source Harness declares ranges in its own pyproject.toml. A spec
        # must name immutable artifacts (app/schemas/requirements.py), so resolve
        # them here rather than either refusing the conversion or storing a spec
        # that installs something different on every rebuild. `platform` is what
        # keeps the resolved pins lockable by the package stage — see resolve_pins.
        requirements=resolve_pins(
            flatten_requirements(grafted, platform), platform
        ),
        code_bundle={k: v for k, v in grafted.items() if k != "pyproject.toml"},
        source_harness={
            "agent_id": source_agent.id,
            "agent_name": source_agent.name,
            "harness_arn": source_agent.arn or "",
        },
        conversion_notes=notes,
        env=wired,
        memory=MemoryConfig(**(source_spec.get("memory") or {})),
        knowledge_bases=kb_refs,
    )
