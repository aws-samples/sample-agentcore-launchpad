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
"""

from app.templates.attachment_support import has_attachment_contract, render_attachment_source

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
