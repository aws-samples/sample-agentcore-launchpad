"""AgentCore Runtime entrypoint that executes one exec-backend call per invoke.

Counterpart of ``skillopt.model.agentcore_runner``: downloads the seeded
work_dir tarball from S3, runs the *same* ``run_claude_code_exec`` /
``run_codex_exec`` harness (CLI mode, Bedrock-direct model access via the
execution role), keeps a best-effort progress sync to S3 while the task runs,
and finally flushes ``out.tar.gz`` + ``result.json``.

Importable without ``bedrock_agentcore`` so the payload handling is unit
testable; the container CMD is ``python -m skillopt.model.agentcore_worker``.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
import traceback
from typing import Any

from skillopt.model import backend_config
from skillopt.model.agentcore_runner import TAR_ROOT
from skillopt.model.backend_config import configure_claude_code_exec, configure_codex_exec

SESSION_STORAGE_ROOT = "/mnt/workspace"


def _work_root() -> str:
    if os.path.isdir(SESSION_STORAGE_ROOT) and os.access(SESSION_STORAGE_ROOT, os.W_OK):
        return SESSION_STORAGE_ROOT
    return tempfile.gettempdir()


def _s3_client():
    import boto3

    return boto3.client("s3")


class _ProgressSync(threading.Thread):
    """Best-effort periodic upload of changed work_dir files to S3.

    Gives the orchestrator mid-task visibility; the final flush is the
    authoritative copy, so every failure here is swallowed on purpose.
    """

    def __init__(self, s3, bucket: str, prefix: str, work_dir: str, interval: int):
        super().__init__(daemon=True)
        self._s3 = s3
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._work_dir = work_dir
        self._interval = interval
        self._stop = threading.Event()
        self._uploaded: dict[str, float] = {}

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:  # pragma: no cover - timing-dependent
        while not self._stop.wait(self._interval):
            self.sync_once()

    def sync_once(self) -> None:
        try:
            for dirpath, _dirnames, filenames in os.walk(self._work_dir):
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    try:
                        if os.path.islink(path):
                            continue
                        mtime = os.path.getmtime(path)
                        rel = os.path.relpath(path, self._work_dir)
                        if self._uploaded.get(rel) == mtime:
                            continue
                        self._s3.upload_file(path, self._bucket, f"{self._prefix}/{rel}")
                        self._uploaded[rel] = mtime
                    except Exception:  # noqa: BLE001
                        continue
        except Exception:  # noqa: BLE001
            pass


def _apply_exec_config(backend: str, cfg: dict[str, Any]) -> None:
    # The image never has the SDKs and never uses a proxy profile; forcing
    # cli/"" here also keeps the CLAUDE_CODE_USE_BEDROCK=0 settings injection
    # (profile-gated in codex_harness) away from the Bedrock-direct CLI.
    retries = cfg.get("empty_response_retries")
    if retries is not None:
        backend_config.EXEC_EMPTY_RESPONSE_RETRIES = max(0, int(retries))
    if backend == "claude_code_exec":
        configure_claude_code_exec(
            use_sdk="cli",
            profile="",
            effort=cfg.get("effort"),
            max_thinking_tokens=cfg.get("max_thinking_tokens"),
        )
    else:
        configure_codex_exec(
            use_sdk="cli",
            profile="",
            sandbox=cfg.get("sandbox"),
            full_auto=cfg.get("full_auto"),
            reasoning_effort=cfg.get("reasoning_effort"),
            network_access=cfg.get("network_access"),
            web_search=cfg.get("web_search"),
            approval_policy=cfg.get("approval_policy"),
        )


def _run_backend(backend: str, **kwargs) -> tuple[str, str]:
    from skillopt.model.codex_harness import run_claude_code_exec, run_codex_exec

    if backend == "claude_code_exec":
        return run_claude_code_exec(**kwargs)
    if backend == "codex_exec":
        return run_codex_exec(**kwargs)
    raise ValueError(f"Unsupported backend: {backend!r}")


# Hooks into the BedrockAgentCoreApp task tracker (set by build_app). While a
# job runs in its background thread, the tracked task makes /ping report
# HealthyBusy so the session survives the idle timeout (docs: runtime-long-run).
_TASK_HOOKS: dict[str, Any] = {"add": None, "complete": None}


def _run_exec_job(payload: dict) -> dict:
    """The actual job: download, exec, final flush. Runs in a background
    thread after the invoke has already returned; result.json is the only
    completion channel, so EVERY failure must end up in it."""
    backend = str(payload["backend"])
    bucket = str(payload["bucket"])
    job_prefix = str(payload["job_prefix"]).strip("/")
    job_id = job_prefix.rsplit("/", 1)[-1]
    sync_interval = int(payload.get("sync_interval", 0) or 0)

    s3 = _s3_client()
    s3.put_object(
        Bucket=bucket,
        Key=f"{job_prefix}/started.json",
        Body=json.dumps({"job_id": job_id, "backend": backend}).encode("utf-8"),
    )

    response, raw, error = "", "", ""
    work_dir = ""
    sync = None
    try:
        job_dir = os.path.join(_work_root(), "exec", job_id)
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        os.makedirs(job_dir)
        work_dir = os.path.join(job_dir, TAR_ROOT)

        obj = s3.get_object(Bucket=bucket, Key=f"{job_prefix}/in.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(obj["Body"].read()), mode="r:gz") as tar:
            tar.extractall(job_dir, filter="data")
        if not os.path.isdir(work_dir):
            raise ValueError(f"in.tar.gz is missing the {TAR_ROOT!r} root directory")

        _apply_exec_config(backend, dict(payload.get("exec_config") or {}))

        if sync_interval > 0:
            sync = _ProgressSync(s3, bucket, f"{job_prefix}/progress", work_dir, sync_interval)
            sync.start()

        response, raw = _run_backend(
            backend,
            work_dir=work_dir,
            prompt=str(payload.get("prompt") or ""),
            model=str(payload.get("model") or ""),
            timeout=int(payload.get("timeout", 600)),
            **dict(payload.get("exec_kwargs") or {}),
        )
    except Exception:  # noqa: BLE001 - shipped back and surfaced locally
        error = traceback.format_exc()
    finally:
        if sync is not None:
            sync.stop()

    # Final flush. out.tar.gz first: result.json is the completion marker the
    # runner polls for, and it fetches the tarball right after seeing it.
    if work_dir and os.path.isdir(work_dir):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(work_dir, arcname=TAR_ROOT)
        s3.put_object(Bucket=bucket, Key=f"{job_prefix}/out.tar.gz", Body=buf.getvalue())
    elif not error:
        error = f"work_dir missing after exec: {work_dir!r}"
    result = {"response": response, "raw": raw, "error": error}
    s3.put_object(
        Bucket=bucket,
        Key=f"{job_prefix}/result.json",
        Body=json.dumps(result).encode("utf-8"),
    )
    return {
        "ok": True,
        "result_key": f"{job_prefix}/result.json",
        "out_key": f"{job_prefix}/out.tar.gz",
        "response_chars": len(response),
        "raw_chars": len(raw),
        "harness_error": bool(error),
    }


def _handle_exec(payload: dict) -> dict:
    """Validate, then run the job asynchronously and return immediately.

    The entrypoint must not block (a blocked handler also blocks /ping —
    docs: runtime-long-run), so the work happens in a daemon thread tracked
    via add_async_task/complete_async_task to keep the session alive.
    Payload key ``wait: true`` runs inline instead (tests, debugging)."""
    backend = str(payload.get("backend") or "")
    if backend not in {"claude_code_exec", "codex_exec"}:
        raise ValueError(f"Unsupported backend: {backend!r}")
    bucket = str(payload.get("bucket") or "")
    job_prefix = str(payload.get("job_prefix") or "").strip("/")
    job_id = job_prefix.rsplit("/", 1)[-1]
    if not bucket or not job_id or "/" in job_id or ".." in job_id:
        raise ValueError(f"Unsafe bucket/job prefix: {bucket!r} {job_prefix!r}")

    if payload.get("wait"):
        return _run_exec_job(payload)

    add_task = _TASK_HOOKS.get("add")
    complete_task = _TASK_HOOKS.get("complete")
    task_id = add_task(f"exec:{job_id}") if callable(add_task) else None

    def _background() -> None:
        try:
            _run_exec_job(payload)
        except Exception:  # noqa: BLE001 - last resort; flush already tried
            traceback.print_exc()
        finally:
            if task_id is not None and callable(complete_task):
                complete_task(task_id)

    threading.Thread(target=_background, daemon=True, name=f"exec-{job_id}").start()
    return {"ok": True, "accepted": True, "job_prefix": job_prefix}


def _cli_version(cmd: list[str]) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else "no output"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


def _handle_ping(payload: dict) -> dict:
    root = _work_root()
    probe = os.path.join(root, ".skillopt-ping")
    writable = True
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError:
        writable = False
    info = {
        "ok": True,
        "work_root": root,
        "session_storage": root == SESSION_STORAGE_ROOT,
        "work_root_writable": writable,
        "region": os.environ.get("AWS_REGION", ""),
        "claude_use_bedrock": os.environ.get("CLAUDE_CODE_USE_BEDROCK", ""),
    }
    if payload.get("versions"):
        info["claude_version"] = _cli_version(["claude", "--version"])
        info["codex_version"] = _cli_version(["codex", "--version"])
    return info


def handle_payload(payload: dict) -> dict:
    """Entrypoint body; never raises so the invoke always gets a JSON verdict."""
    try:
        action = (payload or {}).get("action", "")
        if action == "ping":
            return _handle_ping(payload)
        if action == "exec":
            return _handle_exec(payload)
        return {"ok": False, "error": f"unknown action: {action!r}"}
    except Exception:  # noqa: BLE001
        return {"ok": False, "error": traceback.format_exc()}


def build_app():  # pragma: no cover - requires bedrock_agentcore
    from bedrock_agentcore.runtime import BedrockAgentCoreApp

    app = BedrockAgentCoreApp()
    # While a background exec job is tracked, the SDK reports HealthyBusy on
    # /ping, which keeps the session alive past the idle timeout.
    _TASK_HOOKS["add"] = app.add_async_task
    _TASK_HOOKS["complete"] = app.complete_async_task

    @app.entrypoint
    def invocations(payload):  # noqa: ANN001
        return handle_payload(payload)

    return app


if __name__ == "__main__":  # pragma: no cover
    build_app().run()
