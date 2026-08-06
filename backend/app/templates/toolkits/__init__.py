"""Platform-owned agent toolkits — named bundles of local ``@tool`` functions.

A *toolkit* is source, not a resource. Selecting one in ``AgentSpec.toolkits``
inlines its functions and their seed data into the generated ``main.py``; there
is no ARN, no IAM grant, no gateway, no network call and no extra pip
requirement. That is the whole point: the emitted code stays platform-generated,
so ``spec.code``/``spec.code_bundle`` remain ``None`` and the agent keeps its
config-bundle experiment eligibility (``app/optimization/service.py:253``).

Tool names and descriptions are **derived from the toolkit source with ast**, not
declared a second time. Three consumers need them — the rendered
``DEFAULT_TOOL_DESCRIPTIONS``, ``discover_agent_tools`` (which feeds
``expected_tools`` for readiness and the recommend UI's "current description"),
and the wizard chip — and a hand-maintained copy is a fourth place to forget.
Descriptions are reduced with the same rule Strands applies to a ``@tool``
docstring (everything except the ``Args:`` section, see
``strands/tools/decorator.py::_extract_description_from_docstring``), so what the
platform reports is exactly what the model sees.
"""

import ast
from dataclasses import dataclass
from functools import cache
from pathlib import Path

TEMPLATE_DIR = Path(__file__).parent

# Sections strands keeps in a tool description; only Args-like sections are cut.
_ARGS_HEADINGS = ("args:", "arguments:", "parameters:", "param:", "params:")
_OTHER_HEADINGS = (
    "returns:", "return:", "yields:", "yield:",
    "raises:", "raise:", "except:", "exceptions:",
    "examples:", "example:", "note:", "notes:",
    "see also:", "seealso:", "references:", "ref:",
)


@dataclass(frozen=True)
class ToolkitDef:
    """One toolkit: where its source lives and what prompt the wizard offers."""

    name: str
    source_file: str
    # Offered by the Create Agent wizard as the default system prompt. Kept
    # deliberately generic — it says "use the tools" and "do not make up" without
    # saying what to do when a tool errors, which is what makes the resulting
    # defects prompt-fixable. Hardening it would remove what an experiment
    # measures, so do not "improve" this either.
    default_system_prompt: str

    @property
    def path(self) -> Path:
        return TEMPLATE_DIR / self.source_file


HR_ASSISTANT_PROMPT = """You are a helpful HR Assistant for Acme Corp.

You help employees with:
- Checking PTO (paid time off) balances
- Submitting PTO requests
- Looking up HR policies (PTO, remote work, parental leave, code of conduct)
- Understanding employee benefits (health, dental, vision, 401k, life insurance)
- Retrieving pay stub information

Always use the available tools to answer questions accurately. Do not make up
policy details, benefit amounts, or pay information — look them up.
Be concise, professional, and friendly."""


TOOLKITS: dict[str, ToolkitDef] = {
    "hr_assistant": ToolkitDef(
        name="hr_assistant",
        source_file="hr_assistant.py.tmpl",
        default_system_prompt=HR_ASSISTANT_PROMPT,
    ),
}


def _strip_args_section(docstring: str) -> str:
    """Docstring minus its Args section — the description strands would derive."""
    kept: list[str] = []
    skipping = False
    for line in docstring.strip().splitlines():
        lowered = line.strip().lower()
        if lowered.startswith(_ARGS_HEADINGS):
            skipping = True
            continue
        if lowered.startswith(_OTHER_HEADINGS):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


@cache
def _parsed(name: str) -> tuple[tuple[str, str], ...]:
    """((tool name, description), …) for one toolkit, in source order.

    Parsed rather than imported: the source is a ``.py.tmpl`` fragment with no
    imports of its own (``tool`` comes from the host template), so it is not an
    importable module — and parsing keeps the backend free of a strands import.
    """
    tree = ast.parse(TOOLKITS[name].path.read_text(encoding="utf-8"))
    found: list[tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(
            (isinstance(d, ast.Name) and d.id == "tool")
            or (isinstance(d, ast.Attribute) and d.attr == "tool")
            for d in node.decorator_list
        )
        if not decorated:
            continue
        found.append((node.name, _strip_args_section(ast.get_docstring(node) or "")))
    return tuple(found)


def _known(names: list[str]) -> list[str]:
    """Selected toolkit names, deduplicated, unknown ones dropped."""
    seen: list[str] = []
    for name in names:
        if name in TOOLKITS and name not in seen:
            seen.append(name)
    return seen


def toolkit_source(names: list[str]) -> str:
    """Concatenated toolkit source to inline into the generated agent ('' if none)."""
    blocks = [TOOLKITS[name].path.read_text(encoding="utf-8").strip() for name in _known(names)]
    return "\n\n\n".join(blocks)


def toolkit_tool_names(names: list[str]) -> list[str]:
    """Every tool the selected toolkits contribute, in declaration order."""
    return [tool_name for name in _known(names) for tool_name, _ in _parsed(name)]


def toolkit_tool_descriptions(names: list[str]) -> dict[str, str]:
    """{tool name → default description} for the selected toolkits."""
    return {
        tool_name: description
        for name in _known(names)
        for tool_name, description in _parsed(name)
    }


def toolkit_default_system_prompt(name: str) -> str:
    """Wizard-offered default prompt for one toolkit ('' when unknown)."""
    toolkit = TOOLKITS.get(name)
    return toolkit.default_system_prompt if toolkit else ""
