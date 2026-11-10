"""Strip the platform-appended Knowledge-bases section from a live system prompt.

The 方式B / zip deployers render ``spec.system_prompt + kb_prompt_section(mounted_kbs(spec))``
(``deployer/harness_local.py``, ``templates/strands_agent``), so a prompt revised from the
LIVE agent — an AI recommendation — carries that section too. Storing it back as the spec
prompt would make the next deploy append it twice. Exact block first; otherwise the marker
through the section's closing line, in case the revision reflowed the KB bullet lines.
(CN twin of the Global ``deployer/harness.strip_generated_prompt``, which read the
managed-Harness prompt.)
"""

from app.schemas.agent import AgentSpec
from app.templates.kb_support import kb_prompt_section, mounted_kbs

_KB_MARKER = "\n## Knowledge bases\nTwo retrieval tools are mounted for you."
_KB_END = "explicitly when retrieval returns nothing relevant."


def strip_generated_prompt(spec: AgentSpec, text: str) -> str:
    if not spec.knowledge_bases:
        return text
    block = kb_prompt_section(mounted_kbs(spec))
    if block and block in text:
        return text.replace(block, "").rstrip()
    start = text.find(_KB_MARKER)
    if start < 0:
        return text
    end = text.find(_KB_END, start)
    tail = text[end + len(_KB_END):] if end >= 0 else ""
    return (text[:start] + tail).rstrip()
