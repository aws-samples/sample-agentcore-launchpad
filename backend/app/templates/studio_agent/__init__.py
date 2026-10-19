"""Studio artifact adaptation — platform contracts around studio-generated code.

Studio canvas output is a standalone script: module-level setup (model config,
MCP clients, custom tools), an ``async def main(user_input_arg, messages_arg)``
that streams the answer to stdout, and an argparse ``__main__`` block. Rather
than re-extracting sections from it (upstream's keyword-heuristic adapter drops
MCP client definitions), the platform keeps the module VERBATIM, cuts the
argparse block, and appends a BedrockAgentCoreApp entrypoint that drives
``main()`` and captures its streamed stdout as the result.

Automatic system-prompt override for arbitrary studio code is intentionally
NOT attempted (rewriting user code is unsafe); the shim exposes
``launchpad_config_bundle()`` so studio authors can opt in — documented in
docs/studio-integration.md.

The one edit made to the module is generator-owned: canvas code generated before
0b380650 (2026-08-10) imports ``strands_tools.mem0_memory``, which the package
stage no longer installs the ``mem0`` extra for, so it is dropped from the
``strands_tools`` import line and from any tool list before packaging.
"""

import io
import re
import tokenize

from app.templates.attachment_support import has_attachment_contract, render_attachment_source

# strands_tools the platform stopped packaging; older canvas code still names them
_DROPPED_STRANDS_TOOLS = frozenset({"mem0_memory"})
_STRANDS_TOOLS_IMPORT = re.compile(r"^(\s*)from strands_tools import (.+?)\s*$")
_NON_CODE_TOKENS = (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)

BUNDLE_SHIM = '''
# ─── Launchpad platform contract: config bundles (A/B experiments) ───────────
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext as _LPContext


def launchpad_config_bundle():
    """Active Launchpad config bundle for this request ({} when none routed)."""
    try:
        return _LPContext.get_config_bundle() or {}
    except Exception:
        return {}
# ──────────────────────────────────────────────────────────────────────────────
'''

ENTRYPOINT_WRAPPER = '''
# ─── Launchpad AgentCore wrapper (studio module above is verbatim) ────────────
import contextlib as _lp_contextlib
import inspect as _lp_inspect
import io as _lp_io

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload, context=None):
    prompt = str((payload or {}).get("prompt", "")).strip()
    attachments = (payload or {}).get("attachments")
    if attachments and not __LAUNCHPAD_STUDIO_NATIVE_INPUT__:
        return {"error": "studio code lacks native attachments; regenerate and republish"}
    if not prompt and not attachments:
        return {"error": "payload must include a non-empty 'prompt'"}
    if "main" not in globals():
        return {"error": "studio module does not define main()"}
    try:  # generators vary: main(), main(user_input), main(user_input, messages)
        arity = len(_lp_inspect.signature(main).parameters)
    except (TypeError, ValueError):
        arity = 2
    if attachments and arity < 1:
        return {"error": "studio main() cannot receive native attachments"}
    model_input = _launchpad_strands_input(prompt, attachments) if attachments else prompt
    call_args = [model_input, None][: min(2, arity)]
    buffer = _lp_io.StringIO()
    with _lp_contextlib.redirect_stdout(buffer):
        outcome = main(*call_args)
        if _lp_inspect.isawaitable(outcome):
            outcome = await outcome
    text = buffer.getvalue().strip()
    if not text and outcome is not None:
        text = str(outcome)
    if attachments:
        return {"attachment_contract": "v1", "result": text}
    return {"result": text}


if __name__ == "__main__":
    app.run()
'''


def _drop_tool_imports(code: str) -> str:
    """Remove dropped tools from the generator's one-line ``strands_tools`` import."""
    lines = []
    for line in io.StringIO(code).readlines():  # `\n` only, like tokenize below
        match = _STRANDS_TOOLS_IMPORT.match(line)
        if match:
            names = [name.strip() for name in match.group(2).split(",")]
            kept = [name for name in names if name not in _DROPPED_STRANDS_TOOLS]
            if not kept:
                continue
            if len(kept) < len(names):
                line = f"{match.group(1)}from strands_tools import {', '.join(kept)}\n"
        lines.append(line)
    return "".join(lines)


def _drop_tool_list_refs(code: str) -> str:
    """Remove dropped tools where they are an element of a list literal (``tools=[…]``)."""
    try:
        tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(code).readline)
            if token.type not in _NON_CODE_TOKENS
        ]
    except (tokenize.TokenError, SyntaxError):
        return code  # code that doesn't tokenize fails at the runtime on its own terms
    line_starts = [0]  # same line split as tokenize's rows (str.splitlines adds U+2028 etc.)
    for line in io.StringIO(code).readlines():
        line_starts.append(line_starts[-1] + len(line))

    def offset(position: tuple[int, int]) -> int:
        return line_starts[position[0] - 1] + position[1]

    spans: list[tuple[int, int]] = []
    for index in range(1, len(tokens) - 1):
        token = tokens[index]
        if token.type != tokenize.NAME or token.string not in _DROPPED_STRANDS_TOOLS:
            continue
        before, after = tokens[index - 1], tokens[index + 1]
        if before.string not in ("[", ",") or after.string not in ("]", ","):
            continue
        if after.string == ",":  # `[x, tool, y]` → `[x, y]`
            spans.append((offset(token.start), offset(tokens[index + 2].start)))
        elif before.string == ",":  # `[x, tool]` → `[x]`
            spans.append((offset(before.start), offset(token.end)))
        else:  # `[tool]` → `[]`
            spans.append((offset(token.start), offset(token.end)))
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        code = code[:start] + code[end:]
    return code


def _drop_removed_tools(code: str) -> str:
    """Strip strands_tools the platform no longer packages (pre-0b380650 canvas code)."""
    if not any(name in code for name in _DROPPED_STRANDS_TOOLS):
        return code
    return _drop_tool_list_refs(_drop_tool_imports(code))


def _wrap_studio_module(code: str) -> str:
    """Studio script → agentcore module: drop argparse block, append entrypoint."""
    lines = code.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("if __name__"):
            lines = lines[:index]
            break
    module = "\n".join(lines).rstrip()
    native_input = has_attachment_contract(module)
    helper = "\n" + render_attachment_source() if native_input else ""
    wrapper = ENTRYPOINT_WRAPPER.replace("__LAUNCHPAD_STUDIO_NATIVE_INPUT__", repr(native_input))
    return module + "\n" + helper + wrapper


def adapt_studio_code(code: str) -> str:
    """Studio canvas code → agentcore module with the platform bundle shim."""
    code = _drop_removed_tools(code)
    if "@app.entrypoint" in code:
        adapted = code  # already an agentcore module — use as-is
    else:
        adapted = _wrap_studio_module(code)
    if "get_config_bundle" in adapted:
        return adapted
    lines = adapted.splitlines()
    insert_at = 0
    for index, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            insert_at = index + 1
    lines.insert(insert_at, BUNDLE_SHIM)
    return "\n".join(lines)
