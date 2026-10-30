"""The skills an agent was offered, in every catalogue format the platform's harnesses emit.

``strands_evals.extractors.skills.parse_available_skills`` reads the ``<available_skills>``
XML block and the ``### Available skills`` Markdown list. LangGraph Deep Agents writes its
catalogue into the system prompt as a bold heading with bold names::

    **Available Skills:**

    - **haiku-writer**: Write a haiku (5-7-5 syllables) on a topic the user names.
      -> Read `/skills/haiku-writer/SKILL.md` for full instructions

which the library does not read, so a Deep Agents session had no offered skills: the skill
judges saw "(not recorded by this harness)" and ``{available_skills}`` rendered empty.
:func:`available_skills` is the one hook the engine (``engine_core.tool_cases``) and the
skill evaluators (``evaluators.PerLoadSkillSelectionAccuracyEvaluator``) call: the library's
parse first, then the Deep Agents list.
"""

from __future__ import annotations

import re
from typing import Any

from strands_evals.extractors.skills import (
    AvailableSkill,
    advertised_a_catalog,
    parse_available_skills,
)
from strands_evals.types.trace import AgentInvocationSpan, Session

# the list runs until the next bold section heading ("**How to Use Skills …:**") or the end
_DEEPAGENTS_BLOCK = re.compile(
    r"^\*\*Available Skills:\*\*[ \t]*$\n(?P<body>.*?)(?=^\*\*[^*\n]+:\*\*[ \t]*$|\Z)",
    re.MULTILINE | re.DOTALL)
_DEEPAGENTS_ENTRY = re.compile(
    r"^[ \t]*[-*][ \t]+\*\*(?P<name>[^*\n]+)\*\*:[ \t]*(?P<desc>.*?)[ \t]*$", re.MULTILINE)


def _texts(trajectory: Any) -> list[str]:
    """Where a harness puts its catalogue: the agent spans' system prompts, or a bare prompt."""
    if isinstance(trajectory, str):
        return [trajectory]
    if isinstance(trajectory, Session):
        return [span.system_prompt for trace in trajectory.traces for span in trace.spans
                if isinstance(span, AgentInvocationSpan) and span.system_prompt]
    return []


def parse_deepagents(text: str) -> list[AvailableSkill]:
    """The skills of a Deep Agents ``**Available Skills:**`` list ([] without one)."""
    block = _DEEPAGENTS_BLOCK.search(text or "")
    if not block:
        return []
    return [AvailableSkill(name=m["name"].strip(), description=m["desc"].strip())
            for m in _DEEPAGENTS_ENTRY.finditer(block["body"]) if m["name"].strip()]


def available_skills(trajectory: Any) -> list[AvailableSkill]:
    """The offered skills: the library's formats, else the first Deep Agents list found."""
    skills = parse_available_skills(trajectory)
    if skills:
        return skills
    for text in _texts(trajectory):
        if found := parse_deepagents(text):
            return found
    return []


def advertised(trajectory: Any) -> bool:
    """Whether the run recorded a catalogue at all (empty or not), in any format."""
    return advertised_a_catalog(trajectory) or any(
        _DEEPAGENTS_BLOCK.search(text) for text in _texts(trajectory))
