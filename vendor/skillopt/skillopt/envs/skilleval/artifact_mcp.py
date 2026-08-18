"""Networkless stdio MCP facade for trusted artifact inspection."""
from __future__ import annotations

import argparse
import os
from typing import Annotated, cast

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import CallToolResult, Tool as MCPTool
from pydantic import Field, WithJsonSchema

from ._artifact_mcp_media import (
    DEFAULT_MAX_MEDIA_BYTES,
    MAX_MEDIA_BYTES,
    preflight_png_response as _preflight_png_response,
    read_png as _read_png,
)
from ._artifact_mcp_results import (
    envelope as _envelope,
    guarded_tool as _guarded_tool,
    positive_maximum as _positive_maximum,
    request_budget as _request_budget,
    tool_result as _tool_result,
)
from .inspectors import (
    _render_artifact_files,
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
)
from .inspectors.base import (
    DEFAULT_EXTRACT_CHARS,
    DEFAULT_RESPONSE_BYTES,
    DEFAULT_SCRATCH_BYTES,
    DEFAULT_SCRATCH_DEPTH,
    DEFAULT_SCRATCH_ENTRIES,
    InspectionError,
    MAX_EXTRACT_CHARS,
    MAX_LOGICAL_COMPONENTS,
    MAX_LOGICAL_COMPONENT_BYTES,
    MAX_LOGICAL_PATH_BYTES,
    MAX_LOGICAL_PATH_CHARS,
    MAX_RENDER_PIXELS,
    MAX_RESPONSE_BYTES,
    MAX_SCRATCH_BYTES,
    MAX_SCRATCH_DEPTH,
    MAX_SCRATCH_ENTRIES,
    MIN_RESPONSE_BYTES,
    normalize_selectors,
    validate_logical_path,
    validate_roots,
)

_PATH_ARGUMENT_DESCRIPTION = (
    "Normalized POSIX-relative evidence path; at most "
    f"{MAX_LOGICAL_PATH_CHARS} characters, {MAX_LOGICAL_PATH_BYTES} UTF-8 "
    f"bytes, {MAX_LOGICAL_COMPONENTS} components, and "
    f"{MAX_LOGICAL_COMPONENT_BYTES} UTF-8 bytes per component."
)
LogicalPath = Annotated[
    object,
    WithJsonSchema({"type": "string"}),
    Field(description=_PATH_ARGUMENT_DESCRIPTION),
]
SelectorList = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "null"},
            ]
        }
    ),
]
IntegerBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ]
        }
    ),
]
ScratchBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ],
            "description": (
                "Maximum total regular-file bytes allowed under scratch."
            ),
        }
    ),
]
ScratchEntriesBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ],
            "description": "Maximum entries in one scratch transaction.",
        }
    ),
]
ScratchDepthBudget = Annotated[
    object,
    WithJsonSchema(
        {
            "anyOf": [
                {"type": "integer"},
                {"type": "null"},
            ],
            "description": "Maximum depth in one scratch transaction.",
        }
    ),
]
_PATH_REQUIRED_TOOLS = frozenset(
    {"artifact_inspect", "artifact_render", "artifact_extract"}
)
_TOOL_ARGUMENTS = {
    "artifact_inventory": frozenset(
        {
            "max_response_bytes",
            "max_scratch_bytes",
            "max_scratch_entries",
            "max_scratch_depth",
        }
    ),
    "artifact_inspect": frozenset(
        {
            "path",
            "max_response_bytes",
            "max_scratch_bytes",
            "max_scratch_entries",
            "max_scratch_depth",
        }
    ),
    "artifact_render": frozenset(
        {
            "path",
            "selectors",
            "max_pixels",
            "max_response_bytes",
            "max_scratch_bytes",
            "max_scratch_entries",
            "max_scratch_depth",
        }
    ),
    "artifact_extract": frozenset(
        {
            "path",
            "selectors",
            "max_extract_chars",
            "max_response_bytes",
            "max_scratch_bytes",
            "max_scratch_entries",
            "max_scratch_depth",
        }
    ),
}


class _ArtifactMCP(FastMCP):
    def __init__(self, *args, response_maximum: int, **kwargs):
        self._response_maximum = response_maximum
        super().__init__(*args, **kwargs)

    async def list_tools(self) -> list[MCPTool]:
        """Advertise required paths while runtime validation stays guarded."""
        tools = await super().list_tools()
        advertised: list[MCPTool] = []
        for tool in tools:
            schema = dict(tool.inputSchema)
            schema["additionalProperties"] = False
            if tool.name in _PATH_REQUIRED_TOOLS:
                required = list(schema.get("required", []))
                if "path" not in required:
                    required.append("path")
                schema["required"] = required
            advertised.append(
                tool.model_copy(update={"inputSchema": schema})
            )
        return advertised

    async def call_tool(self, name: str, arguments: dict):
        allowed = _TOOL_ARGUMENTS.get(name)
        if allowed is not None:
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                def reject(_response_limit: int) -> CallToolResult:
                    raise InspectionError("unknown tool argument")

                return _guarded_tool(
                    name,
                    arguments.get("max_response_bytes"),
                    self._response_maximum,
                    reject,
                )
        return await super().call_tool(name, arguments)


def create_server(
    evidence_dir: str,
    scratch_dir: str,
    *,
    max_render_pixels: int = MAX_RENDER_PIXELS,
    max_response_bytes: int = DEFAULT_RESPONSE_BYTES,
    max_extract_chars: int = DEFAULT_EXTRACT_CHARS,
    max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
    max_scratch_bytes: int = DEFAULT_SCRATCH_BYTES,
    max_scratch_entries: int = DEFAULT_SCRATCH_ENTRIES,
    max_scratch_depth: int = DEFAULT_SCRATCH_DEPTH,
) -> FastMCP:
    """Create an in-memory or stdio Artifact MCP server."""
    evidence, scratch = validate_roots(evidence_dir, scratch_dir)
    server_render_max = _positive_maximum(
        max_render_pixels,
        "server render pixel maximum",
        MAX_RENDER_PIXELS,
    )
    server_response_max = _positive_maximum(
        max_response_bytes,
        "server response byte maximum",
        MAX_RESPONSE_BYTES,
    )
    if server_response_max < MIN_RESPONSE_BYTES:
        raise InspectionError(
            f"server response byte maximum must be at least "
            f"{MIN_RESPONSE_BYTES}"
        )
    server_extract_max = _positive_maximum(
        max_extract_chars,
        "server extraction character maximum",
        MAX_EXTRACT_CHARS,
    )
    server_media_max = _positive_maximum(
        max_media_bytes,
        "server media byte maximum",
        MAX_MEDIA_BYTES,
    )
    server_scratch_max = _positive_maximum(
        max_scratch_bytes,
        "server scratch byte maximum",
        MAX_SCRATCH_BYTES,
    )
    server_scratch_entries_max = _positive_maximum(
        max_scratch_entries,
        "server scratch entry maximum",
        MAX_SCRATCH_ENTRIES,
    )
    server_scratch_depth_max = _positive_maximum(
        max_scratch_depth,
        "server scratch depth maximum",
        MAX_SCRATCH_DEPTH,
    )

    server = _ArtifactMCP(
        "skillopt-artifact",
        instructions=None,
        response_maximum=server_response_max,
    )

    @server.tool(
        name="artifact_inventory",
        description="List immutable evidence artifacts with bounded metadata.",
    )
    def artifact_inventory(
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
        max_scratch_entries: ScratchEntriesBudget = None,
        max_scratch_depth: ScratchDepthBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            scratch_entries_limit = _request_budget(
                max_scratch_entries,
                default=server_scratch_entries_max,
                maximum=server_scratch_entries_max,
                name="scratch entry budget",
            )
            scratch_depth_limit = _request_budget(
                max_scratch_depth,
                default=server_scratch_depth_max,
                maximum=server_scratch_depth_max,
                name="scratch depth budget",
            )
            result = inventory_artifacts(
                evidence,
                scratch,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
                max_scratch_entries=scratch_entries_limit,
                max_scratch_depth=scratch_depth_limit,
            )
            return _tool_result(
                _envelope("artifact_inventory", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_inventory",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_inspect",
        description=(
            "Inspect one logical evidence artifact. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_inspect(
        path: LogicalPath = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
        max_scratch_entries: ScratchEntriesBudget = None,
        max_scratch_depth: ScratchDepthBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            scratch_entries_limit = _request_budget(
                max_scratch_entries,
                default=server_scratch_entries_max,
                maximum=server_scratch_entries_max,
                name="scratch entry budget",
            )
            scratch_depth_limit = _request_budget(
                max_scratch_depth,
                default=server_scratch_depth_max,
                maximum=server_scratch_depth_max,
                name="scratch depth budget",
            )
            result = inspect_artifact(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
                max_scratch_entries=scratch_entries_limit,
                max_scratch_depth=scratch_depth_limit,
            )
            return _tool_result(
                _envelope("artifact_inspect", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_inspect",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_render",
        description=(
            "Render selected artifact units as bounded PNG content. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_render(
        path: LogicalPath = None,
        selectors: SelectorList = None,
        max_pixels: IntegerBudget = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
        max_scratch_entries: ScratchEntriesBudget = None,
        max_scratch_depth: ScratchDepthBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            normalized_selectors = normalize_selectors(selectors)
            pixel_limit = _request_budget(
                max_pixels,
                default=server_render_max,
                maximum=server_render_max,
                name="render pixel budget",
            )
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            scratch_entries_limit = _request_budget(
                max_scratch_entries,
                default=server_scratch_entries_max,
                maximum=server_scratch_entries_max,
                name="scratch entry budget",
            )
            scratch_depth_limit = _request_budget(
                max_scratch_depth,
                default=server_scratch_depth_max,
                maximum=server_scratch_depth_max,
                name="scratch depth budget",
            )
            with _render_artifact_files(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                selectors=normalized_selectors,
                max_pixels=pixel_limit,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
                max_scratch_entries=scratch_entries_limit,
                max_scratch_depth=scratch_depth_limit,
            ) as outputs:
                _preflight_png_response(
                    [output.descriptor for output in outputs],
                    response_limit=response_limit,
                    media_byte_limit=server_media_max,
                    max_pixels=pixel_limit,
                )
                metadata = []
                images = []
                used_pixels = 0
                used_bytes = 0
                for index, output in enumerate(outputs):
                    data, details = _read_png(
                        output.descriptor,
                        scratch,
                        remaining_pixels=pixel_limit - used_pixels,
                        remaining_bytes=server_media_max - used_bytes,
                    )
                    used_pixels += details.pop("pixels")
                    used_bytes += details["bytes"]
                    metadata.append({"index": index, **details})
                    images.append(
                        Image(data=data, format="png").to_image_content()
                    )
                result = {"images": metadata}
                return _tool_result(
                    _envelope("artifact_render", result=result),
                    response_limit=response_limit,
                    images=images,
                )

        return _guarded_tool(
            "artifact_render",
            max_response_bytes,
            server_response_max,
            run,
        )

    @server.tool(
        name="artifact_extract",
        description=(
            "Extract bounded content from selected artifact units. "
            + _PATH_ARGUMENT_DESCRIPTION
        ),
    )
    def artifact_extract(
        path: LogicalPath = None,
        selectors: SelectorList = None,
        max_extract_chars: IntegerBudget = None,
        max_response_bytes: IntegerBudget = None,
        max_scratch_bytes: ScratchBudget = None,
        max_scratch_entries: ScratchEntriesBudget = None,
        max_scratch_depth: ScratchDepthBudget = None,
    ) -> CallToolResult:
        def run(response_limit: int) -> CallToolResult:
            validate_logical_path(path)
            normalized_selectors = normalize_selectors(selectors)
            extract_limit = _request_budget(
                max_extract_chars,
                default=server_extract_max,
                maximum=server_extract_max,
                name="extraction character budget",
            )
            scratch_limit = _request_budget(
                max_scratch_bytes,
                default=server_scratch_max,
                maximum=server_scratch_max,
                name="scratch byte budget",
            )
            scratch_entries_limit = _request_budget(
                max_scratch_entries,
                default=server_scratch_entries_max,
                maximum=server_scratch_entries_max,
                name="scratch entry budget",
            )
            scratch_depth_limit = _request_budget(
                max_scratch_depth,
                default=server_scratch_depth_max,
                maximum=server_scratch_depth_max,
                name="scratch depth budget",
            )
            result = extract_artifact(
                cast(str, path),
                evidence_dir=evidence,
                scratch_dir=scratch,
                selectors=normalized_selectors,
                max_extract_chars=extract_limit,
                max_response_bytes=response_limit,
                max_scratch_bytes=scratch_limit,
                max_scratch_entries=scratch_entries_limit,
                max_scratch_depth=scratch_depth_limit,
            )
            return _tool_result(
                _envelope("artifact_extract", result=result),
                response_limit=response_limit,
            )

        return _guarded_tool(
            "artifact_extract",
            max_response_bytes,
            server_response_max,
            run,
        )

    return server


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise InspectionError(f"{name} must be an integer") from exc


def main(argv: list[str] | None = None) -> None:
    """Run only the stdio transport inside the caller-provided sandbox."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence",
        default=os.environ.get("SKILLOPT_ARTIFACT_EVIDENCE", "/evidence"),
    )
    parser.add_argument(
        "--scratch",
        default=os.environ.get("SKILLOPT_ARTIFACT_SCRATCH", "/scratch"),
    )
    parser.add_argument(
        "--max-render-pixels",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_RENDER_PIXELS",
            MAX_RENDER_PIXELS,
        ),
    )
    parser.add_argument(
        "--max-response-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_RESPONSE_BYTES",
            DEFAULT_RESPONSE_BYTES,
        ),
    )
    parser.add_argument(
        "--max-extract-chars",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_EXTRACT_CHARS",
            DEFAULT_EXTRACT_CHARS,
        ),
    )
    parser.add_argument(
        "--max-media-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_MEDIA_BYTES",
            DEFAULT_MAX_MEDIA_BYTES,
        ),
    )
    parser.add_argument(
        "--max-scratch-bytes",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_SCRATCH_BYTES",
            DEFAULT_SCRATCH_BYTES,
        ),
    )
    parser.add_argument(
        "--max-scratch-entries",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_SCRATCH_ENTRIES",
            DEFAULT_SCRATCH_ENTRIES,
        ),
    )
    parser.add_argument(
        "--max-scratch-depth",
        type=int,
        default=_env_int(
            "SKILLOPT_ARTIFACT_MAX_SCRATCH_DEPTH",
            DEFAULT_SCRATCH_DEPTH,
        ),
    )
    args = parser.parse_args(argv)
    server = create_server(
        args.evidence,
        args.scratch,
        max_render_pixels=args.max_render_pixels,
        max_response_bytes=args.max_response_bytes,
        max_extract_chars=args.max_extract_chars,
        max_media_bytes=args.max_media_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        max_scratch_entries=args.max_scratch_entries,
        max_scratch_depth=args.max_scratch_depth,
    )
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
