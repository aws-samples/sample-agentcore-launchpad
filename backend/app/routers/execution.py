"""Studio local-debug execution endpoints.

Run un-deployed studio-generated code in a subprocess (dedicated exec
interpreter) and stream its stdout. Separate namespace from the platform Chat
(`/api/chat/*` = deployed AgentCore runtimes); these run local, un-deployed
flows. Ported from strands_studio_ui backend/main.py (origin/main).
"""

import asyncio
import codecs
import logging
import shutil
import time

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.core.errors import AppError
from app.services import local_exec

logger = logging.getLogger("launchpad.execution")

router = APIRouter(prefix="/api", tags=["studio-local-debug"])


class ExecutionRequest(BaseModel):
    code: str = Field(min_length=1, max_length=400000)
    input_data: str | None = None
    openai_api_key: str | None = None
    bedrock_api_key: str | None = None


def _require_local_execution() -> None:
    """Gate the surface before anything is written to disk or spawned.

    Three refusals, most restrictive first: the deployment may not offer local
    execution at all (production default — this runs caller-supplied Python), the
    dedicated interpreter may not be provisioned, and a configured dedicated
    execution user may not be usable by this process.
    """
    if not local_exec.local_exec_enabled():
        raise AppError(
            "studio.exec.disabled",
            local_exec.disabled_message(),
            status_code=403,
        )
    if not local_exec.interpreter_available():
        raise AppError(
            "studio.exec.interpreter_unavailable",
            local_exec.missing_interpreter_message(),
            status_code=503,
        )
    user_problem = local_exec.exec_user_error()
    if user_problem:
        raise AppError(
            "studio.exec.user_unavailable", user_problem, status_code=503
        )


@router.post("/execute")
async def execute_code(request: ExecutionRequest) -> dict:
    """One-shot execution: run the code and return its captured stdout."""
    _require_local_execution()
    start = time.monotonic()
    try:
        output = await local_exec.execute_strands_code(
            request.code, request.input_data, request.openai_api_key, request.bedrock_api_key
        )
        return {
            "success": True,
            "output": output,
            "execution_time_ms": int((time.monotonic() - start) * 1000),
        }
    except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a result
        logger.error("execute failed: %s", exc)
        return {
            "success": False,
            "error": str(exc),
            "execution_time_ms": int((time.monotonic() - start) * 1000),
        }


@router.post("/execute/stream")
async def execute_code_stream(request: ExecutionRequest) -> StreamingResponse:
    """Streaming execution: forward subprocess stdout as SSE, ending with a
    `[STREAM_COMPLETE:<seconds>]` sentinel. Uses the multiline framing so
    newlines survive `data:` splitting (an empty `data: ` line = newline)."""
    _require_local_execution()
    timeout = get_settings().execute_timeout_s

    async def generate_stream():
        process = None
        workdir = None
        stderr_task = None
        stderr_chunks: list[bytes] = []
        start = time.monotonic()
        try:
            process, workdir = await local_exec.spawn_execution_subprocess(
                request.code, request.input_data,
                request.openai_api_key, request.bedrock_api_key,
            )
            logger.info("streaming subprocess started — pid %s, timeout %ss", process.pid, timeout)

            # Drain stderr concurrently so a chatty subprocess cannot block on a
            # full stderr pipe while we read stdout.
            async def drain_stderr():
                while True:
                    data = await process.stderr.read(4096)
                    if not data:
                        break
                    stderr_chunks.append(data)

            stderr_task = asyncio.create_task(drain_stderr())

            # Incremental decoder: a read() boundary may split a multi-byte
            # UTF-8 character.
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout

            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError()
                data = await asyncio.wait_for(process.stdout.read(4096), timeout=remaining)
                if not data:
                    break
                chunk_str = decoder.decode(data)
                if not chunk_str:
                    continue
                if not chunk_str.startswith("data: "):
                    sse_event = local_exec.chunk_to_sse(chunk_str)
                    if sse_event:
                        yield sse_event
                else:
                    # Pre-formatted SSE data printed by the code is forwarded as-is.
                    yield f"{chunk_str}\n\n" if not chunk_str.endswith("\n\n") else chunk_str

            # Flush any buffered partial character.
            tail = decoder.decode(b"", final=True)
            if tail:
                sse_event = local_exec.chunk_to_sse(tail)
                if sse_event:
                    yield sse_event

            remaining = max(deadline - loop.time(), 1.0)
            await asyncio.wait_for(
                asyncio.gather(stderr_task, process.wait()), timeout=remaining
            )
            elapsed = time.monotonic() - start

            if process.returncode != 0:
                stderr_text = b"".join(stderr_chunks).decode("utf-8", errors="replace").strip()
                error_msg = (
                    stderr_text
                    or f"Code execution failed with exit code {process.returncode}"
                )
                logger.error("streaming subprocess failed — exit code %s", process.returncode)
                yield local_exec.chunk_to_sse(f"Error: {error_msg}")
                yield f"data: [STREAM_COMPLETE:{elapsed}]\n\n"
                return

            yield f"data: [STREAM_COMPLETE:{elapsed}]\n\n"

        except TimeoutError:
            elapsed = time.monotonic() - start
            logger.error("streaming execution timed out after %ss — killing process group", timeout)
            if process is not None:
                local_exec.kill_process_group(process)
            yield f"data: Error: Code execution timed out after {timeout:g} seconds\n\n"
            yield f"data: [STREAM_COMPLETE:{elapsed}]\n\n"
        except Exception as exc:  # noqa: BLE001 — reported to the client as an Error frame
            elapsed = time.monotonic() - start
            logger.error("streaming execution error: %s", exc)
            yield f"data: Error: {exc}\n\n"
            yield f"data: [STREAM_COMPLETE:{elapsed}]\n\n"
        finally:
            if process is not None and process.returncode is None:
                local_exec.kill_process_group(process)
                try:
                    await process.wait()
                except Exception:  # noqa: BLE001
                    pass
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)

    return StreamingResponse(
        generate_stream(),
        media_type="text/plain",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Transfer-Encoding": "chunked",
        },
    )
