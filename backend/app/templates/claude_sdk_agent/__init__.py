"""Claude Agent SDK container template renderer + build-context assembly."""

import json
import shutil
from pathlib import Path
from typing import Any

from app.schemas.agent import AgentSpec
from app.templates.kb_support import (
    KB_DEEP_ITERATIONS_MULTI,
    KB_DEEP_ITERATIONS_SINGLE,
    KB_MCP_SERVER,
    KB_RESULTS,
    kb_deep_tool_description,
    kb_prompt_section,
    kb_tool_description,
    mounted_kbs,
)

TEMPLATE_DIR = Path(__file__).parent

# Claude Code tool names the platform allows agents to request.
DEFAULT_ALLOWED_TOOLS = ["Task"]  # Task = subagent dispatch; Bash/Edit stay off by default


def skill_name_from_path(path: str) -> str:
    """s3://…/skills/web-analyzer/ → web-analyzer (registry and custom prefixes)."""
    return path.rstrip("/").rsplit("/", 1)[-1]


def _mcp_servers(spec: AgentSpec) -> dict[str, Any]:
    """Free-text LAUNCHPAD_MCP_SERVERS JSON ∪ registry-selected remote servers.

    Registry chips are explicit UI selections — they win on key collision."""
    raw = spec.env.get("LAUNCHPAD_MCP_SERVERS", "")
    try:
        free = json.loads(raw) if raw else {}
    except ValueError:
        free = {}
    if not isinstance(free, dict):
        free = {}
    registry = {
        t.name: {"type": "http", "url": t.config["url"]}
        for t in spec.tools
        if t.type == "mcp" and t.config.get("url")
    }
    return {**free, **registry}


def render_main_py(spec: AgentSpec) -> str:
    mcp_config = _mcp_servers(spec)
    kbs = mounted_kbs(spec)
    allowed = list(DEFAULT_ALLOWED_TOOLS)
    if spec.skills:
        allowed.append("Skill")  # the tool Claude Code invokes agent skills through
    allowed += [f"mcp__{name}" for name in mcp_config]
    if kbs:
        # server-level allow, same convention as the registry MCP chips above —
        # the in-process KB server is assembled at runtime, not in mcp_config.
        # Server-level means every tool it carries (kb_search, kb_deep_search) is
        # covered by this one entry.
        allowed.append(f"mcp__{KB_MCP_SERVER}")
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    return (
        source.replace("__LAUNCHPAD_AGENT_NAME__", spec.name)
        .replace("__LAUNCHPAD_MODEL_ID__", spec.model_id)
        .replace(
            "__LAUNCHPAD_SYSTEM_PROMPT__",
            repr(spec.system_prompt + kb_prompt_section(kbs)),
        )
        .replace("__LAUNCHPAD_MOUNTED_KBS__", repr(kbs))
        .replace("__LAUNCHPAD_KB_MCP_SERVER__", KB_MCP_SERVER)
        .replace(
            "__LAUNCHPAD_KB_DEEP_TOOL_DESCRIPTION__",
            repr(kb_deep_tool_description(kbs)),
        )
        .replace("__LAUNCHPAD_KB_TOOL_DESCRIPTION__", repr(kb_tool_description(kbs)))
        .replace("__LAUNCHPAD_KB_RESULTS__", repr(KB_RESULTS))
        .replace(
            "__LAUNCHPAD_KB_DEEP_ITERATIONS_SINGLE__", repr(KB_DEEP_ITERATIONS_SINGLE)
        )
        .replace(
            "__LAUNCHPAD_KB_DEEP_ITERATIONS_MULTI__", repr(KB_DEEP_ITERATIONS_MULTI)
        )
        .replace("__LAUNCHPAD_MAX_TURNS__", str(spec.max_iterations))
        .replace("__LAUNCHPAD_ALLOWED_TOOLS__", repr(allowed))
        .replace("__LAUNCHPAD_MCP_SERVERS__", repr(mcp_config))
        .replace("__LAUNCHPAD_MEMORY_SHORT_TERM__", repr(spec.memory.short_term))
        .replace("__LAUNCHPAD_MEMORY_LONG_TERM__", repr(spec.memory.long_term))
    )


def assemble_build_context(spec: AgentSpec, target_dir: Path) -> Path:
    """Copy the static template files + rendered main.py into target_dir.

    Pure filesystem work — spec.skills S3 download happens in the deployer
    (bundle_skill_paths_into) so this stays testable without AWS."""
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True)
    for name in ("Dockerfile", "requirements.txt", "buildspec.yml", "README.md"):
        shutil.copy2(TEMPLATE_DIR / name, target_dir / name)
    # .claude scaffold ships empty since the fact-checker sample was dropped —
    # git can't track empty dirs, so copy only if a future scaffold reappears;
    # the skill bundler creates .claude/skills/ on demand.
    scaffold = TEMPLATE_DIR / ".claude"
    if scaffold.exists():
        shutil.copytree(scaffold, target_dir / ".claude")
    (target_dir / "main.py").write_text(render_main_py(spec), encoding="utf-8")
    return target_dir
