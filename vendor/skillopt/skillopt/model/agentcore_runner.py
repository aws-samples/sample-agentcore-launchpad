"""Remote exec runner: ship a prepared work_dir to Bedrock AgentCore Runtime.

When ``SKILLOPT_EXEC_RUNNER=agentcore``, ``run_claude_code_exec`` /
``run_codex_exec`` delegate here instead of spawning a local CLI. The local
work_dir (already seeded by ``prepare_workspace``) is tarred to S3, executed
inside a per-task AgentCore microVM by ``skillopt.model.agentcore_worker``,
and the mutated work_dir is downloaded back in place, so manifest diffing,
``codex_last_message.txt`` readback, and the judge all keep working on local
paths. The judge transport (``policy`` calls) never reaches this module.

Design spec: docs/superpowers/specs/agentcore-exec-runner.md
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tarfile
import tempfile
import time
import uuid
from typing import Any

_ENABLE_VALUES = {"agentcore"}
_LOCAL_VALUES = {"", "local"}

# Base name for the remote work_dir inside the tarball; constant so the worker
# never has to trust a path from the payload.
TAR_ROOT = "work"


class AgentCoreRunnerError(RuntimeError):
    """Infrastructure-level failure of the remote exec runner."""


def is_enabled() -> bool:
    value = os.environ.get("SKILLOPT_EXEC_RUNNER", "").strip().lower()
    if value in _LOCAL_VALUES:
        return False
    if value in _ENABLE_VALUES:
        return True
    raise AgentCoreRunnerError(
        f"Unsupported SKILLOPT_EXEC_RUNNER: {value!r} (expected 'local' or 'agentcore')"
    )


def get_runner_config() -> dict[str, Any]:
    arn = os.environ.get("SKILLOPT_AGENTCORE_RUNTIME_ARN", "").strip()
    bucket = os.environ.get("SKILLOPT_AGENTCORE_S3_BUCKET", "").strip()
    if not arn or not bucket:
        raise AgentCoreRunnerError(
            "SKILLOPT_EXEC_RUNNER=agentcore requires SKILLOPT_AGENTCORE_RUNTIME_ARN "
            "and SKILLOPT_AGENTCORE_S3_BUCKET (see scripts/agentcore/setup_infra.py)"
        )
    region = os.environ.get("SKILLOPT_AGENTCORE_REGION", "").strip()
    if not region:
        # arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>
        parts = arn.split(":")
        if len(parts) < 4 or not parts[3]:
            raise AgentCoreRunnerError(f"Cannot parse region from runtime ARN: {arn!r}")
        region = parts[3]
    prefix = os.environ.get("SKILLOPT_AGENTCORE_S3_PREFIX", "exec-jobs").strip().strip("/")
    try:
        sync_interval = int(os.environ.get("SKILLOPT_AGENTCORE_SYNC_INTERVAL", "30").strip())
    except ValueError:
        sync_interval = 30
    return {
        "runtime_arn": arn,
        "bucket": bucket,
        "prefix": prefix or "exec-jobs",
        "region": region,
        "qualifier": os.environ.get("SKILLOPT_AGENTCORE_QUALIFIER", "DEFAULT").strip() or "DEFAULT",
        "sync_interval": max(0, sync_interval),
    }


def _make_clients(region: str, read_timeout: int):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise AgentCoreRunnerError(
            "boto3 is required for SKILLOPT_EXEC_RUNNER=agentcore (pip install boto3)"
        ) from exc
    invoke_cfg = Config(
        connect_timeout=30,
        read_timeout=read_timeout,
        retries={"total_max_attempts": 1},
    )
    runtime = boto3.client("bedrock-agentcore", region_name=region, config=invoke_cfg)
    s3 = boto3.client("s3", region_name=region)
    return runtime, s3


def _tar_work_dir(work_dir: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(work_dir, arcname=TAR_ROOT)
    return buf.getvalue()


def _extract_tar_over_work_dir(data: bytes, work_dir: str) -> None:
    """Replace work_dir contents with the TAR_ROOT subtree of the tarball."""
    staging = tempfile.mkdtemp(prefix="skillopt-agentcore-out-")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(staging, filter="data")
        extracted = os.path.join(staging, TAR_ROOT)
        if not os.path.isdir(extracted):
            raise AgentCoreRunnerError(
                f"Remote output tarball is missing the {TAR_ROOT!r} root directory"
            )
        old = work_dir.rstrip(os.sep) + ".agentcore-old"
        if os.path.exists(old):
            shutil.rmtree(old)
        os.rename(work_dir, old)
        try:
            shutil.move(extracted, work_dir)
        except Exception:
            os.rename(old, work_dir)  # restore on failure
            raise
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _read_invoke_response(response: dict) -> dict:
    body = response.get("response")
    if body is None:
        raise AgentCoreRunnerError("InvokeAgentRuntime returned no response body")
    if hasattr(body, "read"):
        data = body.read()
    elif isinstance(body, (bytes, bytearray)):
        data = bytes(body)
    else:  # EventStream / iterable of chunks
        data = b"".join(
            chunk if isinstance(chunk, (bytes, bytearray)) else str(chunk).encode("utf-8")
            for chunk in body
        )
    text = data.decode("utf-8", errors="replace").strip()
    # SSE framing ("data: {...}") appears when the runtime streams; take the
    # last data payload, which carries the entrypoint return value.
    if text.startswith("data:"):
        payloads = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
        text = payloads[-1] if payloads else text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentCoreRunnerError(
            f"Worker returned non-JSON response: {text[:500]!r}"
        ) from exc
    if not isinstance(parsed, dict):
        raise AgentCoreRunnerError(f"Worker returned non-object response: {parsed!r}")
    return parsed


def _fetch_json(s3, bucket: str, key: str) -> dict:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(obj["Body"].read().decode("utf-8"))


def _poll_interval() -> float:
    try:
        return max(0.05, float(os.environ.get("SKILLOPT_AGENTCORE_POLL_INTERVAL", "10")))
    except ValueError:
        return 10.0


def _poll_json(s3, bucket: str, key: str, deadline: float) -> dict | None:
    """Poll S3 for a JSON object until the deadline (async completion channel)."""
    interval = _poll_interval()
    while True:
        try:
            return _fetch_json(s3, bucket, key)
        except Exception:  # noqa: BLE001 - includes NoSuchKey
            if time.time() >= deadline:
                return None
            time.sleep(interval)


def run_remote_exec(
    *,
    backend: str,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    exec_kwargs: dict[str, Any],
    exec_config: dict[str, Any],
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
) -> tuple[str, str]:
    """Execute one exec-backend call remotely; returns (response, raw).

    The caller persists artifacts afterwards, exactly like the local paths.
    """
    if images or data_dirs:
        raise ValueError(
            "SKILLOPT_EXEC_RUNNER=agentcore does not support images/data_dirs yet "
            "(they reference host paths outside the work_dir)"
        )
    if backend not in {"claude_code_exec", "codex_exec"}:
        raise ValueError(f"Unsupported backend for remote exec: {backend!r}")
    work_dir = os.path.abspath(work_dir)
    if not os.path.isdir(work_dir):
        raise AgentCoreRunnerError(f"work_dir does not exist: {work_dir}")

    cfg = get_runner_config()
    # Async invocation contract (docs: runtime-long-run): the worker accepts
    # the job and returns immediately; completion is signalled by result.json
    # appearing in S3. The invoke itself only needs a short read timeout.
    runtime, s3 = _make_clients(cfg["region"], read_timeout=120)

    job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
    job_prefix = f"{cfg['prefix']}/{job_id}"
    in_key = f"{job_prefix}/in.tar.gz"
    started_key = f"{job_prefix}/started.json"
    result_key = f"{job_prefix}/result.json"
    out_key = f"{job_prefix}/out.tar.gz"

    s3.put_object(Bucket=cfg["bucket"], Key=in_key, Body=_tar_work_dir(work_dir))

    # Remote side re-applies the local exec config, minus host-specific bits.
    remote_config = {
        k: v for k, v in exec_config.items() if k not in {"path", "use_sdk", "profile"}
    }
    payload = {
        "action": "exec",
        "backend": backend,
        "bucket": cfg["bucket"],
        "job_prefix": job_prefix,
        "prompt": prompt,
        "model": model,
        "timeout": int(timeout),
        "exec_kwargs": exec_kwargs,
        "exec_config": remote_config,
        "sync_interval": cfg["sync_interval"],
    }
    session_id = f"skillopt-exec-{job_id}-{uuid.uuid4().hex}"

    # The worker enforces the harness timeout per attempt; wall clock can reach
    # attempts x timeout plus cold-start/transfer overhead before result.json
    # lands. Sessions cap out at AgentCore's 8h maxLifetime.
    attempts = max(1, int(exec_config.get("empty_response_retries", 1) or 0) + 1)
    result_deadline = time.time() + attempts * int(timeout) + 600

    def _stop_session() -> None:
        try:
            runtime.stop_runtime_session(
                agentRuntimeArn=cfg["runtime_arn"],
                qualifier=cfg["qualifier"],
                runtimeSessionId=session_id,
            )
        except Exception:  # noqa: BLE001 - best-effort cost saving
            pass

    accepted = False
    invoke_error: Exception | None = None
    try:
        response = runtime.invoke_agent_runtime(
            agentRuntimeArn=cfg["runtime_arn"],
            qualifier=cfg["qualifier"],
            runtimeSessionId=session_id,
            payload=json.dumps(payload).encode("utf-8"),
        )
        worker_reply = _read_invoke_response(response)
        if not worker_reply.get("ok"):
            _stop_session()
            raise AgentCoreRunnerError(
                f"AgentCore worker rejected job {job_id}: "
                f"{worker_reply.get('error') or worker_reply}"
            )
        accepted = True
    except AgentCoreRunnerError:
        raise
    except Exception as exc:  # noqa: BLE001 - connection drop / read timeout
        invoke_error = exc

    if not accepted:
        # The invoke reply was lost, but the job may still have been delivered
        # and started; started.json is the worker's acceptance marker.
        if _poll_json(s3, cfg["bucket"], started_key, time.time() + 120) is None:
            _stop_session()
            raise AgentCoreRunnerError(
                f"InvokeAgentRuntime failed and the worker never started job "
                f"{job_id} (no {started_key}): {invoke_error}"
            ) from invoke_error

    # Async completion: the microVM keeps working (ping=HealthyBusy) after the
    # invoke returns; result.json is uploaded by its final flush.
    result = _poll_json(s3, cfg["bucket"], result_key, result_deadline)
    _stop_session()
    if result is None:
        raise AgentCoreRunnerError(
            f"AgentCore job {job_id} produced no result.json within "
            f"{attempts * int(timeout) + 600}s (s3://{cfg['bucket']}/{result_key})"
        )

    out_obj = s3.get_object(Bucket=cfg["bucket"], Key=out_key)
    _extract_tar_over_work_dir(out_obj["Body"].read(), work_dir)

    if result.get("error"):
        # The remote harness raised (e.g. codex nonzero exit) — mirror the
        # local behaviour where that exception propagates to the caller.
        raise RuntimeError(f"Remote {backend} failed (job {job_id}): {result['error']}")
    return str(result.get("response") or ""), str(result.get("raw") or "")
