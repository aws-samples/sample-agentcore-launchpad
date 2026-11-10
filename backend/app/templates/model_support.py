"""One model block for every generated runtime (spec C3/C4; plan R4).

The Strands template owns the text; the self-built Harness slices it out between
marker comments, so `model/load.py` in a Harness bundle is byte-identical to the block
a zip runtime runs — no copy, no graft in either direction.
"""

from pathlib import Path

from app.schemas.agent import AgentSpec
from app.templates.strands_agent import (
    rendered_max_tokens,
    rendered_model_catalog_version,
    rendered_model_descriptor,
    rendered_model_inference_params,
    rendered_model_providers,
    rendered_tool_schema_source,
)

STRANDS_TEMPLATE = Path(__file__).parent / "strands_agent" / "main.py.tmpl"
MODEL_CONSTANTS_MARKERS = ("# <launchpad-model-constants>", "# </launchpad-model-constants>")
MODEL_HELPERS_MARKERS = ("# <launchpad-model-helpers>", "# </launchpad-model-helpers>")
_LOAD_HEADER = '''"""model/load.py — Launchpad-owned (docs/cn/interfaces/model-credential.md).

Replaces the agentcore export's model loader: the catalog model — a Global Bedrock profile
or an OpenAI-compatible endpoint's model (N-R6) — entered with the workspace's API key for
that provider, fetched from AgentCore Identity once per process."""

import os
from typing import Any

from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
from strands.models import BedrockModel

'''


def _slice(src: str, markers: tuple[str, str]) -> str:
    """The lines strictly between the two marker comment lines."""
    start, end = markers
    i = src.index(start)
    j = src.index(end, i)
    return src[src.index("\n", i) + 1 : j].strip("\n")


def render_model_block(
    *, model_id: str, model_region: str, model_credential: str, max_tokens: int
) -> str:
    """The Strands template's model constants + helpers with the N2 placeholders rendered
    from scalars — the one text every runtime's model code is sliced from (zip runtime,
    Harness `model/load.py`, eval worker and, since N-R6-obs2, the Studio adapter)."""
    src = STRANDS_TEMPLATE.read_text(encoding="utf-8")
    block = _slice(src, MODEL_CONSTANTS_MARKERS) + "\n\n\n" + _slice(src, MODEL_HELPERS_MARKERS)
    return (
        block.replace("__LAUNCHPAD_MODEL_ID__", model_id)
        .replace("__LAUNCHPAD_MODEL_REGION__", model_region)
        .replace("__LAUNCHPAD_MODEL_CREDENTIAL__", model_credential)
        .replace("__LAUNCHPAD_MODEL_MAX_TOKENS__", repr(max_tokens))
        .replace("__LAUNCHPAD_MODEL_INFERENCE_PARAMS__", rendered_model_inference_params())
        .replace("__LAUNCHPAD_MODEL_PROVIDERS__", rendered_model_providers())
        # D21 (M1-d): the catalog version + MODEL_ID's descriptor the block was rendered from
        .replace("__LAUNCHPAD_MODEL_CATALOG_VERSION__", rendered_model_catalog_version())
        .replace("__LAUNCHPAD_MODEL_DESCRIPTOR__", rendered_model_descriptor(model_id))
        # N17-obs302: the tool input-schema normalizer `_launchpad_chat_model` applies
        .replace("__LAUNCHPAD_TOOL_SCHEMA_SOURCE__", rendered_tool_schema_source())
    )


def render_model_source(spec: AgentSpec) -> str:
    """`render_model_block` for a spec (its model, region, credential and ceiling)."""
    return render_model_block(
        model_id=spec.model_id,
        model_region=spec.model_region,
        model_credential=spec.model_credential,
        max_tokens=rendered_max_tokens(spec),
    )


def render_model_load(spec: AgentSpec) -> str:
    """The Harness bundle's `model/load.py`: header + sliced block + `load_model()`."""
    return (
        _LOAD_HEADER
        + render_model_source(spec)
        + "\n\n\ndef load_model() -> Any:\n    return build_model()\n"
    )
