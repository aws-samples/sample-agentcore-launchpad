"""Bounded PDF and raster-image inspection and rendering."""
from __future__ import annotations

import io
import json
import math
import os
import re
import stat
import subprocess
import threading
import time
import warnings
from contextlib import contextmanager

from PIL import Image as PillowImage
from PIL import ImageOps
from PIL import UnidentifiedImageError

from ._scratch import current_scratch_transaction
from .base import (
    InspectionError,
    RenderBudget,
    ResponseBudget,
    bounded_diagnostic,
    safe_run,
    validate_json_result,
)

MAX_IMAGE_FRAMES = 256
MAX_IMAGE_PIXELS = 100_000_000
MAX_IMAGE_DECODED_BYTES = 512 * 1024 * 1024
MAX_PDF_PAGES = 100_000
MAX_PDF_METADATA_FIELDS = 128
MAX_PDF_METADATA_VALUE_CHARS = 2_048
MAX_PDF_INDEX_PAGES = 256
MAX_PDF_EXTRACT_PAGES = 32
MAX_PDF_SCAN_BYTES = 64 * 1024 * 1024
MAX_PDF_SCAN_SECONDS = 600
PDF_TOOL_TIMEOUT_SECONDS = 120

_PROC_FD_RE = re.compile(r"^/proc/self/fd/([0-9]+)(?:/|$)")
_PAGE_SELECTOR_RE = re.compile(
    r"^page:([1-9][0-9]*)(?:-([1-9][0-9]*))?$"
)
_FRAME_SELECTOR_RE = re.compile(
    r"^frame:([1-9][0-9]*)(?:-([1-9][0-9]*))?$"
)
_TRUNCATED_OUTPUT_MARKER = "\n...[truncated "
_PILLOW_FORMAT_ERRORS = (
    OSError,
    ValueError,
    TypeError,
    SyntaxError,
    EOFError,
    IndexError,
    UnidentifiedImageError,
    PillowImage.DecompressionBombWarning,
    PillowImage.DecompressionBombError,
)
_PILLOW_WARNING_LOCK = threading.RLock()


def _acquire_pillow_warning_lock_before_fork() -> None:
    _PILLOW_WARNING_LOCK.acquire()


def _release_pillow_warning_lock_after_fork_parent() -> None:
    _PILLOW_WARNING_LOCK.release()


def _reinitialize_pillow_warning_lock_after_fork_child() -> None:
    global _PILLOW_WARNING_LOCK

    _PILLOW_WARNING_LOCK = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_acquire_pillow_warning_lock_before_fork,
        after_in_parent=_release_pillow_warning_lock_after_fork_parent,
        after_in_child=_reinitialize_pillow_warning_lock_after_fork_child,
    )


@contextmanager
def _pillow_warning_guard():
    with _PILLOW_WARNING_LOCK:
        with warnings.catch_warnings():
            warnings.simplefilter("error", PillowImage.DecompressionBombWarning)
            yield


def _json_bytes(value: object) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _path_descriptors(path: str) -> tuple[int, ...]:
    match = _PROC_FD_RE.match(path)
    return () if match is None else (int(match.group(1)),)


def _run_tool(
    command: list[str],
    *,
    path: str,
    scratch_dir: str,
    timeout: int | float = PDF_TOOL_TIMEOUT_SECONDS,
) -> str:
    try:
        completed = safe_run(
            command,
            timeout=timeout,
            cwd=scratch_dir,
            home=scratch_dir,
            pass_fds=_path_descriptors(path),
        )
    except InspectionError:
        raise
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise InspectionError(
            f"PDF tool failed: {bounded_diagnostic(exc)}"
        ) from exc
    returncode = getattr(completed, "returncode", None)
    if returncode is not None and returncode != 0:
        diagnostic = getattr(completed, "stderr", None) or getattr(
            completed,
            "stdout",
            "",
        )
        raise InspectionError(
            f"PDF tool exited {returncode}: "
            f"{bounded_diagnostic(diagnostic)}"
        )
    stdout = getattr(completed, "stdout", None)
    if isinstance(stdout, bytes):
        try:
            stdout = stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise InspectionError("PDF tool output is not valid UTF-8") from exc
    if not isinstance(stdout, str):
        raise InspectionError("PDF tool returned invalid output")
    if "\ufffd" in stdout:
        raise InspectionError("PDF tool output is not valid UTF-8")
    if _TRUNCATED_OUTPUT_MARKER in stdout:
        raise InspectionError("PDF tool output exceeded the command output limit")
    return stdout


def _reject_controls(text: str, *, allow_form_feed: bool = False) -> str:
    allowed = {"\n", "\r", "\t"}
    if allow_form_feed:
        allowed.add("\f")
    if any(
        (
            ord(char) < 0x20
            and char not in allowed
        )
        or 0x7F <= ord(char) <= 0x9F
        for char in text
    ):
        raise InspectionError("PDF tool output contains disallowed control characters")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_pdfinfo(stdout: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for raw_line in _reject_controls(stdout).splitlines():
        if not raw_line:
            continue
        key, separator, value = raw_line.partition(":")
        key = key.strip()
        value = value.strip()
        if (
            not separator
            or not key
            or len(key) > 128
            or key in metadata
        ):
            raise InspectionError("pdfinfo returned malformed metadata")
        metadata[key] = value
    raw_pages = metadata.get("Pages")
    if raw_pages is None or re.fullmatch(r"[1-9][0-9]*", raw_pages) is None:
        raise InspectionError("pdfinfo Pages must be a positive integer")
    pages = int(raw_pages)
    if pages > MAX_PDF_PAGES:
        raise InspectionError(
            f"pdfinfo page count exceeds maximum {MAX_PDF_PAGES}"
        )
    return metadata


def _pdf_metadata(
    path: str,
    scratch_dir: str,
    *,
    timeout: int | float = PDF_TOOL_TIMEOUT_SECONDS,
) -> tuple[int, dict[str, str]]:
    stdout = _run_tool(
        ["pdfinfo", path],
        path=path,
        scratch_dir=scratch_dir,
        timeout=timeout,
    )
    metadata = _parse_pdfinfo(stdout)
    return int(metadata["Pages"]), metadata


def _bounded_pdf_inspection(
    pages: int,
    metadata: dict[str, str],
    budget: ResponseBudget,
) -> dict:
    result = {
        "kind": "pdf",
        "opens": True,
        "pages": pages,
        "metadata": {},
        "metadata_omitted": max(0, len(metadata) - 1),
        "metadata_truncated": 0,
        "page_index": {
            "items": [],
            "returned": 0,
            "omitted": pages,
        },
    }
    for key, value in metadata.items():
        if key == "Pages":
            continue
        if len(result["metadata"]) >= MAX_PDF_METADATA_FIELDS:
            break
        bounded_value = value[:MAX_PDF_METADATA_VALUE_CHARS]
        value_truncated = len(value) > len(bounded_value)
        candidate = dict(result)
        candidate["metadata"] = {
            **result["metadata"],
            key: bounded_value,
        }
        candidate["metadata_truncated"] = (
            result["metadata_truncated"] + int(value_truncated)
        )
        if _json_bytes(candidate) > budget.max_bytes:
            break
        result["metadata"][key] = bounded_value
        result["metadata_omitted"] -= 1
        result["metadata_truncated"] = candidate["metadata_truncated"]

    for page in range(1, min(pages, MAX_PDF_INDEX_PAGES) + 1):
        candidate = dict(result)
        candidate["page_index"] = {
            "items": [*result["page_index"]["items"], page],
            "returned": result["page_index"]["returned"] + 1,
            "omitted": pages - result["page_index"]["returned"] - 1,
        }
        if _json_bytes(candidate) > budget.max_bytes:
            break
        result["page_index"] = candidate["page_index"]
    return validate_json_result(result, budget)


def _selected_pdf_pages(selectors: list[str], pages: int) -> list[int]:
    if not selectors:
        return [1]
    selected: list[int] = []
    seen: set[int] = set()
    for selector in selectors:
        match = _PAGE_SELECTOR_RE.fullmatch(selector)
        if match is None:
            raise InspectionError(
                "PDF selector must be 'page:<n>' or 'page:<first>-<last>'"
            )
        first = int(match.group(1))
        last = int(match.group(2) or match.group(1))
        if last < first:
            raise InspectionError("PDF page selector range must be ascending")
        if last > pages:
            raise InspectionError(
                f"PDF page selector exceeds page count {pages}"
            )
        for page in range(first, last + 1):
            if page in seen:
                raise InspectionError(f"PDF page selector repeats page {page}")
            seen.add(page)
            selected.append(page)
    return selected


def _selected_image_frames(selectors: list[str], frames: int) -> list[int]:
    if not selectors:
        return [1]
    selected: list[int] = []
    for selector in selectors:
        if re.fullmatch(r"[1-9][0-9]*", selector):
            frame = int(selector)
        else:
            match = re.fullmatch(r"frame:([1-9][0-9]*)", selector)
            if match is None:
                raise InspectionError(
                    "image render selector must be a positive integer "
                    "or 'frame:<n>'"
                )
            frame = int(match.group(1))
        if frame > frames:
            raise InspectionError(
                f"image frame selector exceeds frame count {frames}"
            )
        if frame in selected:
            raise InspectionError(
                f"image frame selector repeats frame {frame}"
            )
        selected.append(frame)
    return sorted(selected)


def _selected_pdf_render_pages(
    selectors: list[str],
    pages: int,
) -> list[int]:
    if not selectors:
        return [1]
    selected: list[int] = []
    for selector in selectors:
        if re.fullmatch(r"[1-9][0-9]*", selector):
            page = int(selector)
        else:
            match = re.fullmatch(r"page:([1-9][0-9]*)", selector)
            if match is None:
                raise InspectionError(
                    "PDF render selector must be a positive integer "
                    "or 'page:<n>'"
                )
            page = int(match.group(1))
        if page > pages:
            raise InspectionError(
                f"PDF render selector exceeds page count {pages}"
            )
        if page in selected:
            raise InspectionError(
                f"PDF render selector repeats page {page}"
            )
        selected.append(page)
    return sorted(selected)


def _contiguous_ranges(pages: list[int]) -> list[tuple[int, int]]:
    if not pages:
        return []
    ranges: list[tuple[int, int]] = []
    first = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append((first, previous))
        first = previous = page
    ranges.append((first, previous))
    return ranges


def _split_page_text(stdout: str, expected: int) -> list[str]:
    normalized = _reject_controls(stdout, allow_form_feed=True)
    chunks = normalized.split("\f")
    if chunks and chunks[-1] == "":
        chunks.pop()
    if len(chunks) > expected:
        if any(chunk.strip() for chunk in chunks[expected:]):
            raise InspectionError("pdftotext returned too many pages")
        chunks = chunks[:expected]
    chunks.extend("" for _ in range(expected - len(chunks)))
    return [chunk.strip("\n") for chunk in chunks]


def _text_chars(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(_text_chars(item) for item in value)
    if isinstance(value, dict):
        return sum(
            len(key) + _text_chars(child)
            for key, child in value.items()
        )
    return 0


def _fits_extract(value: dict, budget: ResponseBudget) -> bool:
    return (
        _json_bytes(value) <= budget.max_bytes
        and _text_chars(value) <= budget.max_extract_chars
    )


def _image_error(exc: BaseException) -> InspectionError:
    return InspectionError(
        f"image could not be decoded safely: {bounded_diagnostic(exc)}"
    )


def _decoded_frame_bytes(image) -> int:
    if image.mode in {"I", "F"}:
        bytes_per_sample = 4
    elif image.mode.startswith("I;16"):
        bytes_per_sample = 2
    else:
        bytes_per_sample = 1
    return (
        image.width
        * image.height
        * max(1, len(image.getbands()))
        * bytes_per_sample
    )


def _normalized_frame(image) -> PillowImage.Image:
    image.load()
    detached = image.copy()
    oriented = None
    try:
        oriented = ImageOps.exif_transpose(detached)
        has_alpha = (
            "A" in oriented.getbands()
            or "transparency" in oriented.info
        )
        normalized = oriented.convert("RGBA" if has_alpha else "RGB")
        normalized.info.clear()
        return normalized
    finally:
        if oriented is not None and oriented is not detached:
            oriented.close()
        detached.close()


def _remove_outputs(outputs: list[str]) -> None:
    for output in outputs:
        try:
            os.unlink(output)
        except FileNotFoundError:
            pass


class _BoundedScratchSink(io.RawIOBase):
    """Write one output without ever growing past its transaction allowance."""

    def __init__(self, transaction, name: str) -> None:
        super().__init__()
        usage = transaction.check()
        if usage.entries >= transaction.requested_limits.max_entries:
            raise InspectionError("scratch entry budget exceeded")
        self._max_size = transaction.requested_limits.max_bytes - usage.bytes
        self._descriptor = -1
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=transaction.descriptor,
            )
        except FileExistsError as exc:
            raise InspectionError("image render output already exists") from exc
        except OSError as exc:
            raise InspectionError(
                f"image render output could not be created: "
                f"{bounded_diagnostic(exc)}"
            ) from exc
        try:
            transaction.check()
        except BaseException:
            os.close(self._descriptor)
            self._descriptor = -1
            os.unlink(name, dir_fd=transaction.descriptor)
            raise

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def fileno(self) -> int:
        if self.closed or self._descriptor < 0:
            raise ValueError("I/O operation on closed file")
        return self._descriptor

    def tell(self) -> int:
        return os.lseek(self.fileno(), 0, os.SEEK_CUR)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        descriptor = self.fileno()
        if whence == os.SEEK_SET:
            target = offset
        elif whence == os.SEEK_CUR:
            target = os.lseek(descriptor, 0, os.SEEK_CUR) + offset
        elif whence == os.SEEK_END:
            target = os.fstat(descriptor).st_size + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if target < 0 or target > self._max_size:
            raise InspectionError("scratch byte budget exceeded during image render")
        return os.lseek(descriptor, target, os.SEEK_SET)

    def write(self, data) -> int:
        descriptor = self.fileno()
        view = memoryview(data)
        length = view.nbytes
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        projected_size = max(
            os.fstat(descriptor).st_size,
            position + length,
        )
        if projected_size > self._max_size:
            raise InspectionError("scratch byte budget exceeded during image render")
        written = 0
        while written < length:
            count = os.write(descriptor, view[written:])
            if count <= 0:
                raise InspectionError("image render output could not be written")
            written += count
        return written

    def flush(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        if self.closed:
            return
        descriptor = self._descriptor
        try:
            super().close()
        finally:
            self._descriptor = -1
            if descriptor >= 0:
                os.close(descriptor)


def _verify_rendered_png(
    path: str,
    *,
    remaining_pixels: int,
) -> tuple[int, int]:
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise InspectionError(
                "rendered image must be a regular non-symlink file"
            )
        with _pillow_warning_guard():
            with PillowImage.open(path) as image:
                if image.format != "PNG":
                    raise InspectionError("rendered image must be PNG")
                width, height = image.size
                if width <= 0 or height <= 0:
                    raise InspectionError(
                        "rendered image dimensions must be positive"
                    )
                if width * height > remaining_pixels:
                    raise InspectionError(
                        "rendered image pixel budget exceeded"
                    )
                image.verify()
            with PillowImage.open(path) as decoded:
                if decoded.format != "PNG":
                    raise InspectionError("rendered image must be PNG")
                if decoded.size != (width, height):
                    raise InspectionError(
                        "rendered image dimensions changed during validation"
                    )
                decoded.load()
                if decoded.size != (width, height):
                    raise InspectionError(
                        "rendered image dimensions changed during decoding"
                    )
        return width, height
    except InspectionError:
        raise
    except _PILLOW_FORMAT_ERRORS as exc:
        raise InspectionError(
            f"rendered PNG is invalid: {bounded_diagnostic(exc)}"
        ) from exc


def _inspect_image_metadata(path: str) -> tuple[dict, list[dict]]:
    with _pillow_warning_guard():
        return _inspect_image_metadata_locked(path)


def _inspect_image_metadata_locked(path: str) -> tuple[dict, list[dict]]:
    try:
        with _pillow_warning_guard():
            with PillowImage.open(path) as image:
                image.verify()

        frames: list[dict] = []
        total_pixels = 0
        total_decoded_bytes = 0
        has_transparency = False
        with _pillow_warning_guard():
            with PillowImage.open(path) as image:
                image_format = image.format
                if not image_format:
                    raise InspectionError("image metadata is invalid")
                first_size: tuple[int, int] | None = None
                first_mode = ""
                for index in range(MAX_IMAGE_FRAMES + 1):
                    try:
                        image.seek(index)
                    except EOFError:
                        break
                    if index == MAX_IMAGE_FRAMES:
                        raise InspectionError(
                            "image frame count exceeds maximum "
                            f"{MAX_IMAGE_FRAMES}"
                        )
                    width, height = image.size
                    if width <= 0 or height <= 0:
                        raise InspectionError("image dimensions must be positive")
                    frame_pixels = width * height
                    pillow_limit = PillowImage.MAX_IMAGE_PIXELS
                    if (
                        frame_pixels > MAX_IMAGE_PIXELS
                        or (
                            pillow_limit is not None
                            and frame_pixels > pillow_limit
                        )
                    ):
                        raise InspectionError("image pixel limit exceeded")
                    total_pixels += frame_pixels
                    total_decoded_bytes += _decoded_frame_bytes(image)
                    if total_pixels > MAX_IMAGE_PIXELS:
                        raise InspectionError("image pixel limit exceeded")
                    if total_decoded_bytes > MAX_IMAGE_DECODED_BYTES:
                        raise InspectionError(
                            "image decoded-byte limit exceeded"
                        )
                    image.load()
                    transparent = (
                        "A" in image.getbands()
                        or "transparency" in image.info
                    )
                    has_transparency = has_transparency or transparent
                    frame = {
                        "frame": index + 1,
                        "width": width,
                        "height": height,
                        "mode": image.mode,
                        "has_transparency": transparent,
                    }
                    frames.append(frame)
                    if first_size is None:
                        first_size = (width, height)
                        first_mode = image.mode
                if first_size is None:
                    raise InspectionError("image metadata is invalid")
        result = {
            "kind": "image",
            "opens": True,
            "format": image_format,
            "width": first_size[0],
            "height": first_size[1],
            "mode": first_mode,
            "frames": len(frames),
            "has_transparency": has_transparency,
        }
        return result, frames
    except InspectionError:
        raise
    except _PILLOW_FORMAT_ERRORS as exc:
        raise _image_error(exc) from exc


def _image_metadata_fields(path: str, summary: dict) -> dict:
    metadata = {
        "format": summary["format"],
        "width": summary["width"],
        "height": summary["height"],
        "mode": summary["mode"],
        "frames": summary["frames"],
        "has_transparency": summary["has_transparency"],
    }
    try:
        with _pillow_warning_guard():
            with PillowImage.open(path) as image:
                orientation = image.getexif().get(274)
                if orientation is not None:
                    if (
                        isinstance(orientation, bool)
                        or not isinstance(orientation, int)
                        or not 1 <= orientation <= 8
                    ):
                        raise InspectionError(
                            "image EXIF orientation is invalid"
                        )
                    metadata["orientation"] = orientation
                for source_name, result_name in (
                    ("icc_profile", "icc_profile_bytes"),
                    ("exif", "exif_bytes"),
                ):
                    blob = image.info.get(source_name)
                    if blob is None:
                        continue
                    if not isinstance(blob, bytes):
                        raise InspectionError(
                            f"image {source_name} metadata is invalid"
                        )
                    metadata[result_name] = len(blob)
                duration = image.info.get("duration")
                if duration is not None:
                    if (
                        isinstance(duration, bool)
                        or not isinstance(duration, (int, float))
                        or not math.isfinite(duration)
                        or duration < 0
                    ):
                        raise InspectionError(
                            "image duration metadata is invalid"
                        )
                    metadata["duration_ms"] = duration
                loop = image.info.get("loop")
                if loop is not None:
                    if (
                        isinstance(loop, bool)
                        or not isinstance(loop, int)
                        or loop < 0
                    ):
                        raise InspectionError(
                            "image loop metadata is invalid"
                        )
                    metadata["loop"] = loop
        return metadata
    except InspectionError:
        raise
    except _PILLOW_FORMAT_ERRORS as exc:
        raise _image_error(exc) from exc


class ImageInspector:
    """Inspect raster images with bounded Pillow decoding."""

    def inspect(
        self,
        path: str,
        scratch_dir: str,
        *,
        response_budget: ResponseBudget,
    ):
        del scratch_dir
        result, frames = _inspect_image_metadata(path)
        if len(frames) > 1:
            index = {
                "items": [],
                "returned": 0,
                "omitted": len(frames),
            }
            for frame in frames:
                candidate = {
                    **result,
                    "frame_index": {
                        "items": [*index["items"], frame],
                        "returned": index["returned"] + 1,
                        "omitted": len(frames) - index["returned"] - 1,
                    },
                }
                if _json_bytes(candidate) > response_budget.max_bytes:
                    break
                index = candidate["frame_index"]
            result["frame_index"] = index
        return validate_json_result(result, response_budget)

    def extract(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        *,
        response_budget: ResponseBudget,
    ):
        del scratch_dir
        summary, frames = _inspect_image_metadata(path)
        metadata_fields = _image_metadata_fields(path, summary)
        selected_frames: list[int] = []
        selected_metadata: list[str] = []
        units: list[str] = []
        default_selection = not selectors
        if not selectors:
            selectors = ["frame:1", "metadata"]
        for selector in selectors:
            frame_match = _FRAME_SELECTOR_RE.fullmatch(selector)
            if frame_match is not None:
                first = int(frame_match.group(1))
                last = int(frame_match.group(2) or frame_match.group(1))
                if last < first:
                    raise InspectionError(
                        "image frame selector range must be ascending"
                    )
                if last > len(frames):
                    raise InspectionError(
                        f"image frame selector exceeds frame count {len(frames)}"
                    )
                for frame in range(first, last + 1):
                    if frame in selected_frames:
                        raise InspectionError(
                            f"image frame selector repeats frame {frame}"
                        )
                    selected_frames.append(frame)
                    units.append(f"frame:{frame}")
                continue
            if selector == "metadata":
                names = list(metadata_fields)
            elif selector.startswith("metadata:"):
                name = selector.removeprefix("metadata:")
                if name not in metadata_fields:
                    raise InspectionError(
                        f"unknown image metadata selector {name!r}"
                    )
                names = [name]
            else:
                raise InspectionError(
                    "image extract selector must be 'frame:<n>', "
                    "'frame:<first>-<last>', 'metadata', or "
                    "'metadata:<field>'"
                )
            for name in names:
                if name in selected_metadata:
                    raise InspectionError(
                        f"image metadata selector repeats field {name!r}"
                    )
                selected_metadata.append(name)
            units.append(selector)

        result = {
            "kind": "image",
            "opens": True,
            "frame_data": [],
            "metadata": {},
            "units_inspected": [],
            "omitted": {
                "frames": len(frames) - len(selected_frames),
                "metadata": len(metadata_fields) - len(selected_metadata),
            },
            "truncated": False,
            "next_cursor": None,
        }
        for frame_number in selected_frames:
            candidate = {
                **result,
                "frame_data": [
                    *result["frame_data"],
                    frames[frame_number - 1],
                ],
                "units_inspected": [
                    *result["units_inspected"],
                    f"frame:{frame_number}",
                ],
            }
            if not _fits_extract(candidate, response_budget):
                result["truncated"] = True
                result["next_cursor"] = f"frame:{frame_number}"
                break
            result = candidate
        if not result["truncated"] and selected_metadata:
            metadata_units = [
                unit for unit in units
                if unit.startswith("metadata")
            ]
            candidate = {
                **result,
                "metadata": {
                    name: metadata_fields[name]
                    for name in selected_metadata
                },
                "units_inspected": [
                    *result["units_inspected"],
                    *metadata_units,
                ],
            }
            if _fits_extract(candidate, response_budget):
                result = candidate
            else:
                result["truncated"] = True
                result["next_cursor"] = "metadata"
        result["omitted"] = {
            "frames": len(frames) - len(result["frame_data"]),
            "metadata": (
                len(metadata_fields) - len(result["metadata"])
                if selected_metadata
                else len(metadata_fields)
            ),
        }
        if (
            default_selection
            and not result["truncated"]
            and result["omitted"]["frames"] > 0
        ):
            result["truncated"] = True
            result["next_cursor"] = (
                f"frame:{result['frame_data'][-1]['frame'] + 1}"
            )
        return validate_json_result(
            result,
            response_budget,
            enforce_extract_chars=True,
        )

    def render(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        budget: RenderBudget,
    ) -> list[str]:
        transaction = current_scratch_transaction()
        if transaction is None or transaction.proc_path != scratch_dir:
            raise InspectionError(
                "image rendering requires an active scratch transaction"
            )
        normalized_frames: list[tuple[int, PillowImage.Image]] = []
        outputs: list[str] = []
        try:
            with _pillow_warning_guard():
                _summary, frame_metadata = _inspect_image_metadata(path)
                selected = _selected_image_frames(
                    selectors,
                    len(frame_metadata),
                )
                total_pixels = 0
                with PillowImage.open(path) as image:
                    for frame_number in selected:
                        image.seek(frame_number - 1)
                        normalized = _normalized_frame(image)
                        width, height = normalized.size
                        total_pixels += width * height
                        if total_pixels > budget.max_pixels:
                            normalized.close()
                            raise InspectionError(
                                "image render pixel budget exceeded"
                            )
                        normalized_frames.append(
                            (frame_number, normalized)
                        )

                for frame_number, normalized in normalized_frames:
                    output_name = f"frame-{frame_number:04d}.png"
                    output = os.path.join(
                        scratch_dir,
                        output_name,
                    )
                    sink = _BoundedScratchSink(transaction, output_name)
                    outputs.append(output)
                    try:
                        normalized.save(sink, format="PNG")
                    finally:
                        sink.close()
                    transaction.check()

                rendered_pixels = 0
                for output in outputs:
                    width, height = _verify_rendered_png(
                        output,
                        remaining_pixels=(
                            budget.max_pixels - rendered_pixels
                        ),
                    )
                    rendered_pixels += width * height
                    if rendered_pixels > budget.max_pixels:
                        raise InspectionError(
                            "image render pixel budget exceeded"
                        )
                transaction.check()
                return outputs
        except InspectionError:
            _remove_outputs(outputs)
            raise
        except _PILLOW_FORMAT_ERRORS as exc:
            _remove_outputs(outputs)
            raise _image_error(exc) from exc
        finally:
            for _frame_number, normalized in normalized_frames:
                normalized.close()


class PdfInspector:
    """Inspect and extract PDFs through bounded Poppler subprocesses."""

    def inspect(
        self,
        path: str,
        scratch_dir: str,
        *,
        response_budget: ResponseBudget,
    ):
        pages, metadata = _pdf_metadata(path, scratch_dir)
        return _bounded_pdf_inspection(pages, metadata, response_budget)

    def extract(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        *,
        response_budget: ResponseBudget,
    ):
        pages, _metadata = _pdf_metadata(path, scratch_dir)
        selected = _selected_pdf_pages(selectors, pages)
        reply_pages = selected[:MAX_PDF_EXTRACT_PAGES]
        extracted: dict[int, str] = {}
        for first, last in _contiguous_ranges(reply_pages):
            stdout = _run_tool(
                [
                    "pdftotext",
                    "-layout",
                    "-f",
                    str(first),
                    "-l",
                    str(last),
                    path,
                    "-",
                ],
                path=path,
                scratch_dir=scratch_dir,
            )
            for page, text in zip(
                range(first, last + 1),
                _split_page_text(stdout, last - first + 1),
            ):
                extracted[page] = text

        result = {
            "kind": "pdf",
            "opens": True,
            "pages": pages,
            "page_text": [],
            "units_inspected": [],
            "omitted": {
                "pages": pages,
                "characters": 0,
            },
            "truncated": (
                len(selected) > len(reply_pages)
                or (not selectors and pages > len(reply_pages))
            ),
            "next_cursor": (
                f"page:{selected[len(reply_pages)]}"
                if len(selected) > len(reply_pages)
                else (
                    f"page:{reply_pages[-1] + 1}"
                    if not selectors and reply_pages[-1] < pages
                    else None
                )
            ),
        }
        for page_index, page in enumerate(reply_pages):
            text = extracted[page]
            entry = {
                "page": page,
                "text": text,
                "truncated": False,
                "omitted_characters": 0,
            }
            candidate = {
                **result,
                "page_text": [*result["page_text"], entry],
                "units_inspected": [
                    *result["units_inspected"],
                    f"page:{page}",
                ],
                "omitted": {
                    **result["omitted"],
                    "pages": pages - len(result["page_text"]) - 1,
                },
            }
            if _fits_extract(candidate, response_budget):
                result = candidate
                continue
            next_page = (
                reply_pages[page_index + 1]
                if page_index + 1 < len(reply_pages)
                else (
                    selected[len(reply_pages)]
                    if len(selected) > len(reply_pages)
                    else (page + 1 if page < pages else None)
                )
            )
            low = 0
            high = len(text)
            best: dict | None = None
            while low <= high:
                middle = (low + high) // 2
                omitted_characters = len(text) - middle
                partial_entry = {
                    "page": page,
                    "text": text[:middle],
                    "truncated": True,
                    "omitted_characters": omitted_characters,
                }
                partial = {
                    **result,
                    "page_text": [
                        *result["page_text"],
                        partial_entry,
                    ],
                    "units_inspected": [
                        *result["units_inspected"],
                        f"page:{page}",
                    ],
                    "omitted": {
                        "pages": pages - len(result["page_text"]) - 1,
                        "characters": (
                            result["omitted"]["characters"]
                            + omitted_characters
                        ),
                    },
                    "truncated": True,
                    "next_cursor": (
                        f"page:{next_page}"
                        if next_page is not None
                        else None
                    ),
                }
                if _fits_extract(partial, response_budget):
                    best = partial
                    low = middle + 1
                else:
                    high = middle - 1
            if best is not None:
                result = best
            else:
                result["truncated"] = True
                result["next_cursor"] = f"page:{page}"
                result["omitted"]["characters"] += len(text)
            break
        return validate_json_result(
            result,
            response_budget,
            enforce_extract_chars=True,
        )

    def contains_text(
        self,
        path: str,
        scratch_dir: str,
        text: str,
        *,
        max_pages: int = MAX_PDF_PAGES,
        max_bytes: int = MAX_PDF_SCAN_BYTES,
        timeout: int | float = MAX_PDF_SCAN_SECONDS,
    ) -> bool:
        if (
            not isinstance(text, str)
            or not text
            or len(text) > 10_000
            or "\x00" in text
            or any(
                ord(char) < 32 and char not in {"\n", "\r", "\t"}
                for char in text
            )
        ):
            raise InspectionError(
                "contains_text query must be non-empty, bounded text"
            )
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or max_pages <= 0
            or max_pages > MAX_PDF_PAGES
        ):
            raise InspectionError("PDF text scan page budget is invalid")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
            or max_bytes > MAX_PDF_SCAN_BYTES
        ):
            raise InspectionError("PDF text scan byte budget is invalid")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
            or timeout > MAX_PDF_SCAN_SECONDS
        ):
            raise InspectionError("PDF text scan timeout budget is invalid")

        deadline = time.monotonic() + timeout
        pages, _metadata = _pdf_metadata(
            path,
            scratch_dir,
            timeout=min(PDF_TOOL_TIMEOUT_SECONDS, timeout),
        )
        if pages > max_pages:
            raise InspectionError(
                f"PDF text scan page budget exceeded: {pages} > {max_pages}"
            )
        extracted_bytes = 0
        carry = ""
        for page in range(1, pages + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InspectionError("PDF text scan time budget exceeded")
            stdout = _run_tool(
                [
                    "pdftotext",
                    "-layout",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    path,
                    "-",
                ],
                path=path,
                scratch_dir=scratch_dir,
                timeout=min(PDF_TOOL_TIMEOUT_SECONDS, remaining),
            )
            page_text = _split_page_text(stdout, 1)[0]
            extracted_bytes += len(page_text.encode("utf-8"))
            if extracted_bytes > max_bytes:
                raise InspectionError(
                    "PDF text scan byte budget exceeded"
                )
            searchable = carry + page_text
            if text in searchable:
                return True
            carry = (
                searchable[-(len(text) - 1):]
                if len(text) > 1
                else ""
            )
        return False

    def render(
        self,
        path: str,
        scratch_dir: str,
        selectors: list[str],
        budget: RenderBudget,
    ) -> list[str]:
        transaction = current_scratch_transaction()
        if transaction is None or transaction.proc_path != scratch_dir:
            raise InspectionError(
                "PDF rendering requires an active scratch transaction"
            )
        pages, _metadata = _pdf_metadata(path, scratch_dir)
        selected = _selected_pdf_render_pages(selectors, pages)
        outputs: list[str] = []
        rendered_pixels = 0
        try:
            for page in selected:
                if rendered_pixels >= budget.max_pixels:
                    raise InspectionError(
                        "PDF render pixel budget exceeded"
                    )
                prefix = os.path.join(
                    scratch_dir,
                    f"page-{page:04d}",
                )
                output = f"{prefix}.png"
                if os.path.lexists(output):
                    raise InspectionError(
                        "PDF render output already exists"
                    )
                _run_tool(
                    [
                        "pdftoppm",
                        "-f",
                        str(page),
                        "-singlefile",
                        "-png",
                        "-r",
                        "144",
                        path,
                        prefix,
                    ],
                    path=path,
                    scratch_dir=scratch_dir,
                )
                outputs.append(output)
                width, height = _verify_rendered_png(
                    output,
                    remaining_pixels=budget.max_pixels - rendered_pixels,
                )
                rendered_pixels += width * height
                if rendered_pixels > budget.max_pixels:
                    raise InspectionError(
                        "PDF render pixel budget exceeded"
                    )
                transaction.check()
            return outputs
        except InspectionError:
            _remove_outputs(outputs)
            raise


__all__ = ["ImageInspector", "PdfInspector"]
