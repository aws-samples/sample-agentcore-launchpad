"""Codegen orchestration service — AI-fix pipeline.

Ported from strands_studio_ui ``backend/codegen/service.py`` (origin/main),
keeping only the fix half. The generate pipeline (cache lookup, golden-example
workspace, template fallback) was dropped — launchpad ports AI-fix only.

Fix pipeline (AI fix for failed local executions): build a fix workspace
(failing code + error tail + flow) -> backend.generate(mode="fix") ->
diagnosis.json -> if code changed: validation + repair loop; validation
exhausted -> revert to the original code (never ship broken code). No caching
(errors are one-off).

Exposes an async generator of SSE-ready event dicts:
  {"event": "progress"|"agent_activity"|"validation"|"done"|"error", "data": {...}}
"""

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from app.codegen import config
from app.codegen.backends import registry
from app.codegen.backends.base import CodingAgentBackend, GenerationTask
from app.codegen.backends.registry import UnknownBackendError
from app.codegen.validators import ValidationReport, validate_generated_code
from app.codegen.workspace_builder import build_fix_workspace, cleanup_workspace

logger = logging.getLogger(__name__)

GENERATED_FILENAME = config.GENERATED_FILENAME
DIAGNOSIS_FILENAME = "diagnosis.json"
DIAGNOSIS_CATEGORIES = {"code", "config", "environment"}


async def get_status() -> dict[str, Any]:
    """Availability of the configured coding-agent backend (for UI Fix-button gating)."""
    backend_name = config.get_backend_name()
    try:
        backend = registry.get_backend()
    except UnknownBackendError as e:
        return {"backend": backend_name, "available": False, "reason": str(e)}

    try:
        available, reason = await backend.check_available()
    except Exception as e:
        logger.error(f"Backend availability check failed: {e}", exc_info=True)
        return {"backend": backend.name, "available": False, "reason": str(e)}
    finally:
        await backend.close()

    return {
        "backend": backend.name,
        "model": config.get_model(),
        "available": available,
        "reason": reason or None,
    }


class _EventEmitter:
    """Bridges the worker coroutine to the SSE generator via a queue."""

    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()

    async def emit(self, event: str, data: dict[str, Any]) -> None:
        await self.queue.put({"event": event, "data": data})

    async def close(self) -> None:
        await self.queue.put(None)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


async def _acquire_backend() -> CodingAgentBackend:
    """Instantiate the configured backend and verify availability."""
    backend = registry.get_backend()
    available, reason = await backend.check_available()
    if not available:
        raise RuntimeError(f"Codegen backend '{backend.name}' unavailable: {reason}")
    return backend


def _read_generated_code(workspace) -> str:
    generated_file = workspace / GENERATED_FILENAME
    if generated_file.exists():
        return generated_file.read_text(encoding="utf-8")
    return ""


async def _validate_and_emit(
    emitter: _EventEmitter,
    workspace,
    flow_data: dict,
    round_index: int,
) -> ValidationReport:
    """Run the validation pipeline and emit progress + validation events."""
    await emitter.emit("progress", {"message": "Validating generated code"})
    report = await validate_generated_code(workspace, flow_data)
    await emitter.emit(
        "validation",
        {"round": round_index, "errors": report.to_dict()["errors"]},
    )
    return report


async def _repair_loop(
    emitter: _EventEmitter,
    backend: CodingAgentBackend,
    workspace,
    flow_data: dict,
    on_progress: Callable[[str], Awaitable[None]],
    max_rounds: int,
    make_repair_task: Callable[[str, list[str]], GenerationTask],
    initial_report: ValidationReport,
) -> ValidationReport:
    """Run repair rounds (same agent session) until validation passes or exhausted."""
    report = initial_report
    round_index = 0
    while not report.passed and round_index < max_rounds:
        round_index += 1
        await emitter.emit(
            "progress",
            {"message": f"Validation failed, repair round {round_index}/{max_rounds}"},
        )
        task = make_repair_task(_read_generated_code(workspace), report.error_messages())
        await backend.generate(workspace, task, on_progress)
        report = await _validate_and_emit(emitter, workspace, flow_data, round_index)
    return report


async def _pump_events(
    emitter: _EventEmitter,
    worker: Callable[[], Awaitable[None]],
) -> AsyncIterator[dict[str, Any]]:
    """Drive a worker coroutine and yield its emitted events until closed."""
    worker_task = asyncio.create_task(worker())
    try:
        while True:
            item = await emitter.queue.get()
            if item is None:
                break
            yield item
    finally:
        if not worker_task.done():
            worker_task.cancel()
        try:
            await worker_task
        except (asyncio.CancelledError, Exception):
            pass


# ---------------------------------------------------------------------------
# Fix pipeline (AI fix for failed executions)
# ---------------------------------------------------------------------------

def _read_diagnosis(workspace, fallback_summary: str) -> dict[str, Any]:
    """Load and normalize diagnosis.json; degrade gracefully when missing/invalid."""
    raw: dict[str, Any] | None = None
    diagnosis_file = workspace / DIAGNOSIS_FILENAME
    try:
        parsed = json.loads(diagnosis_file.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            raw = parsed
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"diagnosis.json missing or unparseable: {e}")

    if raw is None:
        return {
            "category": "code",
            "summary": fallback_summary
            or "The AI agent did not produce a structured diagnosis.",
            "suggestions": [],
        }

    category = raw.get("category")
    if category not in DIAGNOSIS_CATEGORIES:
        logger.warning(f"diagnosis.json has invalid category {category!r}; using 'code'")
        category = "code"

    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        summary = fallback_summary or "The AI agent did not provide a summary."

    suggestions: list[dict[str, str]] = []
    for item in raw.get("suggestions") or []:
        if not isinstance(item, dict):
            continue
        suggestion = {
            key: item[key]
            for key in ("node_label", "property", "action")
            if isinstance(item.get(key), str)
        }
        if suggestion:
            suggestions.append(suggestion)

    return {"category": category, "summary": summary, "suggestions": suggestions}


async def _run_fix(
    emitter: _EventEmitter,
    original_code: str,
    error: str,
    flow_data: dict,
    graph_mode: bool,
    input_data: str | None,
    started_at: float,
) -> None:
    """Diagnose + (maybe) fix + validate. Reverts to the original code on any doubt."""
    backend = await _acquire_backend()

    await emitter.emit("progress", {"message": "Building fix workspace"})
    workspace = build_fix_workspace(original_code, error, flow_data, graph_mode, input_data)

    # Track the agent's last prose message as a fallback diagnosis summary
    last_text = {"value": ""}

    async def on_progress(summary: str) -> None:
        if (
            summary
            and not summary.startswith("[tool]")
            and not summary.startswith("Agent round finished")
        ):
            last_text["value"] = summary
        await emitter.emit("agent_activity", {"summary": summary})

    try:
        max_rounds = config.get_max_repair_rounds()
        task = GenerationTask(flow_data=flow_data, graph_mode=graph_mode, mode="fix")

        await emitter.emit(
            "progress",
            {"message": f"Diagnosing failure with backend '{backend.name}'"},
        )
        await backend.generate(workspace, task, on_progress)

        diagnosis = _read_diagnosis(workspace, fallback_summary=last_text["value"])

        new_code = _read_generated_code(workspace) or original_code
        changed = new_code != original_code

        if changed and diagnosis["category"] == "environment":
            # Environment issues must never be "fixed" in code (e.g. hardcoded keys)
            logger.warning(
                "Fix agent modified code for an environment-category diagnosis; "
                "reverting to the original code"
            )
            changed = False

        validation_report = None
        if changed:
            report = await _validate_and_emit(emitter, workspace, flow_data, 0)
            report = await _repair_loop(
                emitter, backend, workspace, flow_data, on_progress, max_rounds,
                make_repair_task=lambda code, errors: GenerationTask(
                    flow_data=flow_data,
                    graph_mode=graph_mode,
                    previous_code=code,
                    validation_errors=errors,
                    mode="fix",
                ),
                initial_report=report,
            )
            validation_report = report.to_dict()

            if report.passed:
                new_code = _read_generated_code(workspace)
                changed = new_code != original_code
            else:
                logger.warning(
                    "AI-fixed code failed validation after repair rounds; "
                    "reverting to the original code"
                )
                changed = False
                diagnosis["summary"] += (
                    " [Note: the AI attempted a code fix, but the fixed code "
                    "failed contract validation, so the original code is "
                    "returned unchanged.]"
                )

        await emitter.emit(
            "done",
            {
                "code": new_code if changed else original_code,
                "changed": changed,
                "diagnosis": diagnosis,
                "validation_report": validation_report,
                "duration_ms": _duration_ms(started_at),
            },
        )
    finally:
        await backend.close()
        cleanup_workspace(workspace)


async def fix_code_events(
    code: str,
    error: str,
    flow_data: dict,
    graph_mode: bool = False,
    input_data: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Async generator of SSE events for one AI-fix request (no caching)."""
    started_at = time.monotonic()
    emitter = _EventEmitter()
    timeout_s = config.get_timeout_s()

    async def worker() -> None:  # nosemgrep: useless-inner-function
        # The task group below schedules this local coroutine explicitly.
        try:
            await asyncio.wait_for(
                _run_fix(
                    emitter, code, error, flow_data, graph_mode,
                    input_data, started_at,
                ),
                timeout=timeout_s,
            )
        except TimeoutError:
            await emitter.emit(
                "error",
                {"message": f"AI fix timed out after {timeout_s:.0f}s"},
            )
        except UnknownBackendError as e:
            await emitter.emit("error", {"message": str(e)})
        except Exception as e:
            logger.error(f"AI fix failed: {e}", exc_info=True)
            await emitter.emit("error", {"message": str(e)})
        finally:
            await emitter.close()

    async for item in _pump_events(emitter, worker):
        yield item
