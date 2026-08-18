"""Trusted JSON command-line interface for artifact inspection."""
from __future__ import annotations

import argparse
import json
import sys

from . import (
    extract_artifact,
    inspect_artifact,
    inventory_artifacts,
    render_artifact,
)
from .base import (
    DEFAULT_EXTRACT_CHARS,
    DEFAULT_RESPONSE_BYTES,
    DEFAULT_SCRATCH_BYTES,
    DEFAULT_SCRATCH_DEPTH,
    DEFAULT_SCRATCH_ENTRIES,
    InspectionError,
    MAX_EXTRACT_CHARS,
    MAX_RENDER_PIXELS,
    MAX_RESPONSE_BYTES,
    MAX_SCRATCH_BYTES,
    MAX_SCRATCH_DEPTH,
    MAX_SCRATCH_ENTRIES,
    MIN_RESPONSE_BYTES,
    bounded_diagnostic,
    normalize_selectors,
    validate_logical_path,
    validate_roots,
)


class _HelpRequested(Exception):
    def __init__(self, help_text: str):
        super().__init__("help requested")
        self.help_text = help_text


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise InspectionError(f"argument error: {message}")

    def print_help(self, file=None) -> None:
        raise _HelpRequested(self.format_help())

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status == 0:
            raise _HelpRequested(self.format_help())
        raise InspectionError(
            f"argument error: {message or f'parser exited {status}'}"
        )


def _bounded_int(name: str, maximum: int, minimum: int = 1):
    def parse(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if parsed <= 0:
            raise argparse.ArgumentTypeError(f"{name} must be positive")
        if parsed < minimum:
            raise argparse.ArgumentTypeError(
                f"{name} must be at least {minimum}"
            )
        if parsed > maximum:
            raise argparse.ArgumentTypeError(
                f"{name} exceeds maximum {maximum}"
            )
        return parsed

    return parse


def _add_roots(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--scratch", required=True)
    parser.add_argument(
        "--max-response-bytes",
        type=_bounded_int(
            "max response bytes",
            MAX_RESPONSE_BYTES,
            MIN_RESPONSE_BYTES,
        ),
        default=DEFAULT_RESPONSE_BYTES,
    )
    parser.add_argument(
        "--max-scratch-bytes",
        type=_bounded_int("max scratch bytes", MAX_SCRATCH_BYTES),
        default=DEFAULT_SCRATCH_BYTES,
    )
    parser.add_argument(
        "--max-scratch-entries",
        type=_bounded_int("max scratch entries", MAX_SCRATCH_ENTRIES),
        default=DEFAULT_SCRATCH_ENTRIES,
    )
    parser.add_argument(
        "--max-scratch-depth",
        type=_bounded_int("max scratch depth", MAX_SCRATCH_DEPTH),
        default=DEFAULT_SCRATCH_DEPTH,
    )


def _add_artifact(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("artifact")
    parser.add_argument("--selector", action="append", default=[])
    _add_roots(parser)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = _JSONArgumentParser(
        prog="skillopt-artifactctl",
        description="Inspect immutable evidence artifacts.",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        parser_class=_JSONArgumentParser,
    )

    inventory = subparsers.add_parser("inventory")
    _add_roots(inventory)

    inspect_parser = subparsers.add_parser("inspect")
    _add_artifact(inspect_parser)

    render = subparsers.add_parser("render")
    _add_artifact(render)
    render.add_argument(
        "--max-pixels",
        type=_bounded_int("max pixels", MAX_RENDER_PIXELS),
        default=MAX_RENDER_PIXELS,
    )

    extract = subparsers.add_parser("extract")
    _add_artifact(extract)
    extract.add_argument(
        "--max-extract-chars",
        type=_bounded_int("max extract chars", MAX_EXTRACT_CHARS),
        default=DEFAULT_EXTRACT_CHARS,
    )
    return parser.parse_args(argv)


def _response_limit_from_argv(argv: list[str]) -> int:
    parser = _JSONArgumentParser(add_help=False)
    parser.add_argument(
        "--max-response-bytes",
        type=_bounded_int(
            "max response bytes",
            MAX_RESPONSE_BYTES,
            MIN_RESPONSE_BYTES,
        ),
        default=DEFAULT_RESPONSE_BYTES,
    )
    args, _ = parser.parse_known_args(argv)
    return args.max_response_bytes


def _serialize(payload: dict, maximum: int) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > maximum:
        raise InspectionError("CLI response byte budget exceeded")
    return encoded


def _serialize_help(help_text: str, maximum: int) -> str:
    def render(text: str) -> str:
        return _serialize(
            {"status": "ok", "result": {"help": text}},
            maximum,
        )

    try:
        return render(help_text)
    except InspectionError:
        pass

    marker = "...[truncated]"
    low = 0
    high = len(help_text)
    best = render(marker)
    while low <= high:
        middle = (low + high) // 2
        try:
            candidate = render(help_text[:middle] + marker)
        except InspectionError:
            high = middle - 1
        else:
            best = candidate
            low = middle + 1
    return best


def _serialize_error(exc: Exception, maximum: int) -> str:
    full_error = f"{type(exc).__name__}: {bounded_diagnostic(exc)}"

    def render(error: str) -> str:
        return json.dumps(
            {"status": "error", "error": error},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )

    output = render(full_error)
    if len(output.encode("utf-8")) <= maximum:
        return output

    marker = "...[truncated]"
    low = 0
    high = len(full_error)
    best = render("InspectionError: bounded failure")
    while low <= high:
        middle = (low + high) // 2
        candidate = render(full_error[:middle] + marker)
        if len(candidate.encode("utf-8")) <= maximum:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    if len(best.encode("utf-8")) > maximum:  # pragma: no cover
        raise InspectionError("minimum response budget is too small")
    return best


def main(argv: list[str] | None = None) -> int:
    """Run one operation and emit exactly one JSON object on stdout."""
    response_limit = DEFAULT_RESPONSE_BYTES
    cli_args = list(sys.argv[1:] if argv is None else argv)
    try:
        response_limit = _response_limit_from_argv(cli_args)
        args = parse_args(cli_args)
        response_limit = args.max_response_bytes
        if args.command != "inventory":
            validate_logical_path(args.artifact)
        validate_roots(args.evidence, args.scratch)
        if args.command == "inventory":
            result = inventory_artifacts(
                args.evidence,
                args.scratch,
                max_response_bytes=args.max_response_bytes,
                max_scratch_bytes=args.max_scratch_bytes,
                max_scratch_entries=args.max_scratch_entries,
                max_scratch_depth=args.max_scratch_depth,
            )
        elif args.command == "inspect":
            normalize_selectors(args.selector)
            result = inspect_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                max_response_bytes=args.max_response_bytes,
                max_scratch_bytes=args.max_scratch_bytes,
                max_scratch_entries=args.max_scratch_entries,
                max_scratch_depth=args.max_scratch_depth,
            )
        elif args.command == "render":
            result = render_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                selectors=args.selector,
                max_pixels=args.max_pixels,
                max_response_bytes=args.max_response_bytes,
                max_scratch_bytes=args.max_scratch_bytes,
                max_scratch_entries=args.max_scratch_entries,
                max_scratch_depth=args.max_scratch_depth,
            )
        else:
            result = extract_artifact(
                args.artifact,
                evidence_dir=args.evidence,
                scratch_dir=args.scratch,
                selectors=args.selector,
                max_extract_chars=args.max_extract_chars,
                max_response_bytes=args.max_response_bytes,
                max_scratch_bytes=args.max_scratch_bytes,
                max_scratch_entries=args.max_scratch_entries,
                max_scratch_depth=args.max_scratch_depth,
            )
        output = _serialize(
            {"status": "ok", "result": result},
            response_limit,
        )
        exit_code = 0
    except _HelpRequested as exc:
        output = _serialize_help(exc.help_text, response_limit)
        exit_code = 0
    except Exception as exc:
        output = _serialize_error(exc, response_limit)
        exit_code = 2
    sys.stdout.write(output + "\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
