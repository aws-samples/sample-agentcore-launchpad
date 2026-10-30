"""Studio artifact adaptation — platform contracts around studio-generated code.

Studio canvas output is a standalone script: module-level setup (model config,
MCP clients, custom tools), an ``async def main(user_input_arg, messages_arg)``
that streams the answer to stdout, and an argparse ``__main__`` block. Rather
than re-extracting sections from it (upstream's keyword-heuristic adapter drops
MCP client definitions), the platform keeps the module VERBATIM, cuts the
argparse block, and appends a BedrockAgentCoreApp entrypoint that drives
``main()``. The reply is the final text of the outermost agent's result (N17-obs405 — the
captured stdout carried Strands' ``PrintingCallbackHandler`` stream: reasoning, the pre-tool
preamble, ``Tool #1: …`` lines, then the answer printed a second time by the canvas); what
``main()`` prints still goes to the process stdout (the runtime's log stream), and is the
reply only for a module whose ``main()`` runs no Strands agent.

Automatic system-prompt override for arbitrary studio code is intentionally
NOT attempted (rewriting user code is unsafe); the shim exposes
``launchpad_config_bundle()`` (the CA-2 bundle pointer → S3 reader) so studio authors
can opt in — documented in docs/studio-integration.md.

The CN model prelude (spec C3/C4, N-R6-obs2) rebinds the ``BedrockModel`` name the
canvas emits to a deferred catalog model: the SAME model block every generated runtime
runs (``templates/strands_agent/main.py.tmpl`` between the ``launchpad-model-*`` markers —
``MODEL_PROVIDERS`` / ``MODEL_INFERENCE_PARAMS`` / ``_launchpad_chat_model``) is rendered
into the module, and ``_LaunchpadStudioModel`` calls ``_launchpad_chat_model(model_id,
**config)`` on first use, after the request's workload token has been exchanged for the
provider's key — so a node may carry any catalog model, Bedrock or OpenAI-compatible
(``docs/cn/interfaces/model-providers.md`` §2).
"""

import re

from app.optimization.bundles import RUNTIME_BUNDLE_READER
from app.schemas.agent import (
    DEFAULT_MODEL_CREDENTIAL,
    DEFAULT_MODEL_REGION,
    AgentSpec,
    agent_default_model_id,
    default_runtime_max_tokens,
)
from app.templates.attachment_support import has_attachment_contract, render_attachment_source
from app.templates.model_support import render_model_block
from app.templates.strands_agent import rendered_max_tokens

BUNDLE_SHIM = (
    '''
# ─── Launchpad platform contract: config bundles (A/B experiments) ───────────
'''
    + RUNTIME_BUNDLE_READER
    + '''

def launchpad_config_bundle():
    """Active Launchpad config bundle for this request ({} when none routed; CA-2)."""
    try:
        ref = _launchpad_bundle_ref()
        return _launchpad_fetch_bundle(ref) if ref else {}
    except Exception:
        return {}
# ──────────────────────────────────────────────────────────────────────────────
'''
)

ENTRYPOINT_WRAPPER = '''
# ─── Launchpad AgentCore wrapper (studio module above is verbatim) ────────────
import contextlib as _lp_contextlib
import contextvars as _lp_contextvars
import inspect as _lp_inspect
import io as _lp_io
import sys as _lp_sys

from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# N17-obs405: the reply is the final result of the OUTERMOST agent call of this request,
# recorded by a wrapper around Strands' Agent.stream_async (every invocation path —
# __call__, invoke_async, stream_async — runs through it). An agent used as a tool runs
# nested and is never the reply. Contextvars: one list per request, copied into the thread
# Agent.__call__ runs on.
_LP_RESULTS = _lp_contextvars.ContextVar("_lp_results", default=None)
_LP_DEPTH = _lp_contextvars.ContextVar("_lp_agent_depth", default=0)


def _launchpad_record_agent_results():
    """Install the recorder once per process; a module loaded again (a test, a reload)
    shares the installed recorder's contextvars."""
    global _LP_RESULTS, _LP_DEPTH
    try:
        from strands.agent.agent import Agent as _LpAgent
    except Exception:  # noqa: BLE001 — a module without Strands keeps the stdout reply
        return
    installed = getattr(_LpAgent.stream_async, "_launchpad_recording", None)
    if installed is not None:
        _LP_RESULTS, _LP_DEPTH = installed
        return
    original = _LpAgent.stream_async
    results_var, depth_var = _LP_RESULTS, _LP_DEPTH

    async def stream_async(self, *args, **kwargs):
        depth = depth_var.get()
        token = depth_var.set(depth + 1)
        try:
            async for event in original(self, *args, **kwargs):
                results = results_var.get()
                if depth == 0 and results is not None and isinstance(event, dict) \\
                        and "result" in event:
                    results.append(event["result"])
                yield event
        finally:
            depth_var.reset(token)

    stream_async._launchpad_recording = (results_var, depth_var)
    _LpAgent.stream_async = stream_async


_launchpad_record_agent_results()


class _LaunchpadTee(_lp_io.StringIO):
    """main()'s stdout: kept for the fallback reply AND passed through to the process
    stdout (the runtime's log stream)."""

    def __init__(self, target):
        super().__init__()
        self._lp_target = target

    def write(self, text):
        try:
            self._lp_target.write(text)
        except Exception:  # noqa: BLE001 — logging must never break the reply
            pass
        return super().write(text)

    def flush(self):
        try:
            self._lp_target.flush()
        except Exception:  # noqa: BLE001
            pass


@app.entrypoint
async def invoke(payload, context=None):
    _launchpad_capture_bundle_ref(payload)  # CA-2 channel 3: payload["launchpad"]["bundle"]
    # Eager, like the strands template's `_prefetch_bedrock_api_key`: copy the token the
    # Runtime injected for THIS request out of the SDK context while the request thread
    # still owns it, so a model first used from another thread (judge pools, executors)
    # finds it. Absent when the module carries no model prelude (no BedrockModel).
    globals().get("_capture_workload_token", lambda: None)()
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
    buffer = _LaunchpadTee(_lp_sys.stdout)
    results = []
    token = _LP_RESULTS.set(results)
    try:
        with _lp_contextlib.redirect_stdout(buffer):
            outcome = main(*call_args)
            if _lp_inspect.isawaitable(outcome):
                outcome = await outcome
    finally:
        _LP_RESULTS.reset(token)
    # N17-obs405: the agent's final answer, exactly once; stdout only when no agent ran
    text = str(results[-1]).strip() if results else ""
    if not text:
        text = buffer.getvalue().strip()
    if not text and outcome is not None:
        text = str(outcome)
    if attachments:
        return {"attachment_contract": "v1", "result": text}
    return {"result": text}


if __name__ == "__main__":
    app.run()
'''


class StudioAdaptError(ValueError):
    """The studio module cannot be adapted for the CN runtime (see message)."""


# Canvas generators emit exactly `from strands.models import BedrockModel, CacheConfig`
# (frontend/src/studio/lib/*code-generator.ts). The name is rebound to the deferred catalog
# model below; a module that calls BedrockModel( without that anchored import is refused
# rather than deployed to fail at its first model call.
_BEDROCK_IMPORT_RE = re.compile(
    r"^(from strands\.models(?:\.bedrock)? import .*?\b)BedrockModel\b(?! as )(.*)$", re.MULTILINE
)
_STOCK_ALIAS = "BedrockModel as _LaunchpadStockBedrockModel"
_STOCK_NAME = "_LaunchpadStockBedrockModel"

# The shared block constructs the stock class by the name `BedrockModel`; inside a studio
# module that name is the deferred model (the alias line below), so the one construction
# site is pointed at the aliased import instead — nothing else in the block is touched.
_SHARED_STOCK_CONSTRUCTION = "return BedrockModel(**_supported_model_config(config))"
_STUDIO_STOCK_CONSTRUCTION = f"return {_STOCK_NAME}(**_supported_model_config(config))"

MODEL_PRELUDE_HEADER = '''
# ─── Launchpad platform contract: catalog model + provider API key via AgentCore Identity ──
# Spec C3/C4, N-R6, N-R6-obs2. The block between the two rules is the SAME model block every
# platform-generated runtime runs (templates/strands_agent/main.py.tmpl, markers
# `launchpad-model-constants` / `launchpad-model-helpers`): MODEL_PROVIDERS says which
# provider serves each catalog model, MODEL_INFERENCE_PARAMS which inference fields it
# accepts, `_launchpad_chat_model` dispatches — Global Bedrock (bearer from MODEL_CREDENTIAL)
# or an OpenAI-compatible endpoint (its own Identity API-key provider). Studio code builds
# its models at import time, before any request carries a workload token, so `BedrockModel`
# below is a DEFERRED model: the dispatch runs on first use, inside a request.
import logging as _lp_logging
import os
import threading as _lp_threading
from typing import Any

from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from strands.models.model import Model as _LaunchpadModelBase

'''

MODEL_PRELUDE_FOOTER = '''


# Bedrock-only `BedrockModel(...)` kwargs the canvas may emit regardless of the node's model
# (prompt-caching toggles, the Anthropic thinking block, the region line); an OpenAI-compatible
# endpoint would reject them as chat-completions fields, so they are dropped for that kind.
_LAUNCHPAD_STUDIO_BEDROCK_ONLY_KWARGS = frozenset({
    "region_name", "boto_session", "boto_client_config", "cache_config",
    "additional_request_fields", "additional_response_field_paths", "guardrail_id",
    "guardrail_version", "guardrail_trace", "guardrail_stream_processing_mode",
    "guardrail_redact_input", "guardrail_redact_input_message", "guardrail_redact_output",
    "guardrail_redact_output_message", "streaming",
})
_lp_log = _lp_logging.getLogger("launchpad.studio.model")


class _LaunchpadStudioModel(_LaunchpadModelBase):
    """The canvas's `BedrockModel(model_id=…, …)`: a catalog model built through
    `_launchpad_chat_model` on FIRST USE (`stream` / `structured_output`), when the request
    carries the workload token the key exchange needs — construction at module scope touches
    neither Identity nor the network. `config` / `get_config` / `update_config` answer from
    the pending kwargs until then. The platform owns the model region (region boundary): a
    canvas `region_name` / `boto_session` is ignored and MODEL_REGION applies. The first
    use is serialised by a lock (one Agent, several concurrent requests → one inner model,
    one key exchange); every dropped canvas kwarg is named once in a WARNING."""

    def __init__(self, *, model_id: str = MODEL_ID, **config: Any):
        self._lp_model_id = str(model_id)
        self._lp_config: dict[str, Any] = dict(config)
        self._lp_inner: Any = None
        self._lp_lock = _lp_threading.Lock()

    def _lp_build(self) -> Any:
        config = dict(self._lp_config)
        kind = _model_provider(self._lp_model_id)["kind"]
        dropped = {k for k in ("region_name", "boto_session") if k in config}
        if kind != "bedrock":
            dropped |= {k for k in _LAUNCHPAD_STUDIO_BEDROCK_ONLY_KWARGS if k in config}
        for key in dropped:
            config.pop(key, None)
        if dropped:
            why = (
                "the platform owns the model region (MODEL_REGION applies)"
                if kind == "bedrock"
                else f"the {kind} provider serving this row rejects Bedrock-only fields"
            )
            _lp_log.warning(
                "studio model %s: dropped canvas kwargs %s — %s",
                self._lp_model_id, sorted(dropped), why,
            )
        return _launchpad_chat_model(model_id=self._lp_model_id, **config)

    def _lp_model(self) -> Any:
        if self._lp_inner is None:
            with self._lp_lock:
                if self._lp_inner is None:
                    self._lp_inner = self._lp_build()
        return self._lp_inner

    @property
    def config(self) -> Any:
        if self._lp_inner is not None:
            return self._lp_inner.config
        return {"model_id": self._lp_model_id, **self._lp_config}

    def get_config(self) -> Any:
        if self._lp_inner is not None:
            return self._lp_inner.get_config()
        return dict(self.config)

    def update_config(self, **model_config: Any) -> None:
        if self._lp_inner is not None:
            self._lp_inner.update_config(**model_config)
            return
        if "model_id" in model_config:
            self._lp_model_id = str(model_config.pop("model_id"))
        self._lp_config.update(model_config)

    def stream(self, *args: Any, **kwargs: Any) -> Any:
        return self._lp_model().stream(*args, **kwargs)

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        return self._lp_model().structured_output(*args, **kwargs)

    async def count_tokens(self, *args: Any, **kwargs: Any) -> Any:
        # Forwarded to the built model (strands' `Model.count_tokens` is async); like
        # `stream`, a first call here builds the inner model under the lock.
        return await self._lp_model().count_tokens(*args, **kwargs)


def launchpad_resolve_bedrock_api_key() -> str:
    """Studio-author opt-in kept from the Bedrock-only prelude: the Global Bedrock API key,
    also exported as AWS_BEARER_TOKEN_BEDROCK for a bedrock-runtime client built by hand."""
    key = _resolve_bedrock_api_key()
    os.environ[BEDROCK_BEARER_ENV] = key
    return key
# ──────────────────────────────────────────────────────────────────────────────────────
'''
STUDIO_MODEL_ALIAS_LINE = "BedrockModel = _LaunchpadStudioModel\n"
_PRELUDE_MARKER = "class _LaunchpadStudioModel("


def render_model_prelude(spec: AgentSpec | None = None) -> str:
    """Header + the shared model block (rendered for `spec`, or the platform defaults when
    the adapter is called without one) + the deferred-model footer."""
    if spec is not None:
        block = render_model_block(
            model_id=spec.model_id,
            model_region=spec.model_region,
            model_credential=spec.model_credential,
            max_tokens=rendered_max_tokens(spec),
        )
    else:
        # N17-obs252: the platform default is the `agent_default` model role at THIS render,
        # never the import-time constant
        fallback = agent_default_model_id()
        block = render_model_block(
            model_id=fallback,
            model_region=DEFAULT_MODEL_REGION,
            model_credential=DEFAULT_MODEL_CREDENTIAL,
            max_tokens=default_runtime_max_tokens(fallback),
        )
    if _SHARED_STOCK_CONSTRUCTION not in block:  # pragma: no cover - template drift guard
        raise StudioAdaptError(
            "the shared model block no longer constructs the stock BedrockModel where the "
            "studio adapter expects it — update templates/studio_agent"
        )
    block = block.replace(_SHARED_STOCK_CONSTRUCTION, _STUDIO_STOCK_CONSTRUCTION)
    return MODEL_PRELUDE_HEADER + block + MODEL_PRELUDE_FOOTER


def _graft_bedrock_import(code: str) -> tuple[str, int | None]:
    """(code with the import aliased, line index of that import).

    The index is None when the module never constructs ``BedrockModel(`` (nothing to
    graft) or is already adapted (the alias is present — ``adapt_studio_code`` is
    idempotent). A module that constructs it without the anchored import is refused.
    """
    if _STOCK_ALIAS in code:
        return code, None
    match = _BEDROCK_IMPORT_RE.search(code)
    if match is None:
        if re.search(r"\bBedrockModel\s*\(", code):
            raise StudioAdaptError(
                "studio code constructs BedrockModel without `from strands.models import "
                "BedrockModel…` — the CN runtime rebinds that import to the catalog model "
                "that fetches its provider's API key; regenerate the code from the canvas"
            )
        return code, None
    grafted = (
        code[: match.start()] + match.group(1) + _STOCK_ALIAS + match.group(2) + code[match.end():]
    )
    return grafted, code.count("\n", 0, match.start())


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


def adapt_studio_code(code: str, spec: AgentSpec | None = None) -> str:
    """Studio canvas code → agentcore module with the platform shims (bundle + model).

    `spec` renders the model block's constants (MODEL_ID / region / credential / ceiling)
    from the publishing spec; without one the platform defaults apply — every node names
    its own `model_id`, so the constants only matter to `build_model()`."""
    if "@app.entrypoint" in code:
        adapted = code  # already an agentcore module — use as-is
    else:
        adapted = _wrap_studio_module(code)
    adapted, anchor = _graft_bedrock_import(adapted)
    lines = adapted.splitlines()
    if anchor is not None and _PRELUDE_MARKER not in adapted:
        # Directly under the grafted import (R16): canvas code builds its models at module
        # scope, so the alias must be bound before the first `<label>_model = BedrockModel(`.
        lines[anchor + 1 : anchor + 1] = [render_model_prelude(spec) + STUDIO_MODEL_ALIAS_LINE]
    if "launchpad_config_bundle" not in adapted:
        insert_at = 0  # historical placement: after the last top-level import line
        for index, line in enumerate(lines):
            if line.startswith(("import ", "from ")):
                insert_at = index + 1
        lines.insert(insert_at, BUNDLE_SHIM)
    return "\n".join(lines)
