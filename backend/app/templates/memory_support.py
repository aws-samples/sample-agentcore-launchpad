"""Render the self-hosted memory runtime library into generated agents (N6, ruling R6).

`memory_runtime.py.tmpl` is inlined through `__LAUNCHPAD_MEMORY_SOURCE__` into both
Strands templates (and grafted by N7's harness template): S3 session snapshots via
Strands' own `S3SessionManager`, an `AfterInvocationEvent` hook that writes one
AgentCore-shaped turn event and enqueues extraction, and k-NN recall over the
`launchpad-memory` OpenSearch index. The library is env-driven (the `ENV_*` keys below,
written by `deployer/environment.py`) and a no-op without `LAUNCHPAD_MEMORY_ID`, so it is
rendered unconditionally. Contract: docs/cn/interfaces/memory-runtime.md.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec

MEMORY_TEMPLATE = Path(__file__).with_name("memory_runtime.py.tmpl")
MEMORY_SOURCE_MARKER = "LAUNCHPAD_MEMORY_RUNTIME = 'v1'"
# Env keys: written by deployer/environment.py, read by the rendered library.
ENV_MEMORY_ID = "LAUNCHPAD_MEMORY_ID"
ENV_MEMORY_BUCKET = "LAUNCHPAD_MEMORY_BUCKET"
ENV_MEMORY_PREFIX = "LAUNCHPAD_MEMORY_PREFIX"
ENV_MEMORY_OS_ENDPOINT = "LAUNCHPAD_MEMORY_OS_ENDPOINT"
ENV_MEMORY_QUEUE_URL = "LAUNCHPAD_MEMORY_QUEUE_URL"
# F-LOAD-02 (N17-obs533): compact JSON of `embedding_backend.extract_model_block()` at deploy
# time — the Runtime's own `memory_extract` messages carry it as `extract_model`; absent (no
# row for the role) or unparseable → the message omits the block, the Lambda env fallback runs
ENV_MEMORY_EXTRACT_MODEL = "LAUNCHPAD_MEMORY_EXTRACT_MODEL"
ENV_MEMORY_LONG_TERM = "LAUNCHPAD_MEMORY_LONG_TERM"
ENV_KB_OS_ENDPOINT = "LAUNCHPAD_KB_OS_ENDPOINT"


def uses_memory(spec: AgentSpec) -> bool:
    return bool(spec.memory.short_term or spec.memory.long_term)


def render_memory_source() -> str:
    """The library source, placeholder-free: the same text for every spec."""
    return MEMORY_TEMPLATE.read_text(encoding="utf-8")
