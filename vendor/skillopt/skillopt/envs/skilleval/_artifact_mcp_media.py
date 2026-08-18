"""Stable-descriptor media validation for Artifact MCP renders."""
from __future__ import annotations

import io
import json
import os
import stat
from contextlib import ExitStack

from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image as PillowImage
from PIL import UnidentifiedImageError

from ._artifact_mcp_results import envelope
from .inspectors.base import InspectionError, bounded_diagnostic
from .inspectors._secure_files import open_scratch_file

DEFAULT_MAX_MEDIA_BYTES = 25 * 1024 * 1024
MAX_MEDIA_BYTES = 100 * 1024 * 1024


def preflight_png_response(
    descriptors: list[int],
    *,
    response_limit: int,
    media_byte_limit: int,
    max_pixels: int,
) -> None:
    """Reject PNG payloads that cannot fit before reading or base64 encoding."""
    metadata = []
    images = []
    encoded_bytes = 0
    media_bytes = 0
    for index, descriptor in enumerate(descriptors):
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InspectionError("rendered media must be a regular file")
        if info.st_size <= 0:
            raise InspectionError("rendered media byte budget exceeded")
        media_bytes += info.st_size
        if media_bytes > media_byte_limit:
            raise InspectionError("rendered media byte budget exceeded")
        encoded_bytes += 4 * ((info.st_size + 2) // 3)
        metadata.append(
            {
                "index": index,
                "mime": "image/png",
                "width": max_pixels,
                "height": max_pixels,
                "bytes": info.st_size,
            }
        )
        images.append(
            ImageContent(type="image", data="", mimeType="image/png")
        )

    payload = envelope("artifact_render", result={"images": metadata})
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    skeleton = CallToolResult(
        content=[TextContent(type="text", text=text), *images],
        structuredContent=payload,
        isError=False,
    )
    fixed_bytes = len(
        skeleton.model_dump_json(
            by_alias=True,
            exclude_none=True,
        ).encode("utf-8")
    )
    required_bytes = fixed_bytes + encoded_bytes
    if required_bytes > response_limit:
        raise InspectionError(
            "MCP response byte budget exceeded: "
            f"{required_bytes} > {response_limit}"
        )


def read_png(
    source: int | str,
    scratch_dir: str,
    *,
    remaining_pixels: int,
    remaining_bytes: int,
) -> tuple[bytes, dict]:
    with ExitStack() as stack:
        if isinstance(source, int) and not isinstance(source, bool):
            descriptor = os.dup(source)
            stack.callback(os.close, descriptor)
        else:
            try:
                descriptor = stack.enter_context(
                    open_scratch_file(scratch_dir, source)
                )
            except (OSError, InspectionError) as exc:
                raise InspectionError(
                    "rendered media could not be opened: "
                    f"{bounded_diagnostic(exc)}"
                ) from exc
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InspectionError("rendered media must be a regular file")
        if info.st_size <= 0 or info.st_size > remaining_bytes:
            raise InspectionError("rendered media byte budget exceeded")
        with os.fdopen(os.dup(descriptor), "rb") as media:
            data = media.read(remaining_bytes + 1)
    if len(data) != info.st_size or len(data) > remaining_bytes:
        raise InspectionError("rendered media byte budget exceeded")
    try:
        with PillowImage.open(io.BytesIO(data)) as image:
            if image.format != "PNG":
                raise InspectionError("rendered media must be PNG")
            width, height = image.size
            image.verify()
    except InspectionError:
        raise
    except (OSError, ValueError, UnidentifiedImageError) as exc:
        raise InspectionError(
            f"rendered media is not a valid PNG: {bounded_diagnostic(exc)}"
        ) from exc
    pixels = width * height
    if width <= 0 or height <= 0 or pixels > remaining_pixels:
        raise InspectionError("rendered media pixel budget exceeded")
    return data, {
        "mime": "image/png",
        "width": width,
        "height": height,
        "bytes": len(data),
        "pixels": pixels,
    }
