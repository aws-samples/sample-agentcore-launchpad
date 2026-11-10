"""AgentCore Runtime entrypoint that executes one exec-backend call per invoke.

Counterpart of ``skillopt.model.agentcore_runner``: downloads the seeded
work_dir tarball from S3, runs the *same* ``run_opencode_exec`` harness (the
OpenCode CLI against Bedrock's OpenAI-compatible endpoint; LAUNCHPAD PATCH D16),
keeps a best-effort progress sync to S3 while the task runs, and finally flushes
``out.tar.gz`` + ``result.json``.

Model credential (docs/cn/interfaces/model-credential.md): the Global Bedrock API
key is never baked into the image or the runtime env. The platform sends
``runtimeUserId`` on every invoke, so the Runtime injects a workload access token;
``resolve_model_api_key`` exchanges it for the key through AgentCore Identity
(``GetResourceApiKey`` on ``LAUNCHPAD_MODEL_CREDENTIAL``) and exports it as
``AWS_BEARER_TOKEN_BEDROCK`` for the CLI child only. The exchange happens ON THE
REQUEST THREAD (``_handle_exec`` → ``_prefetch_model_api_key``, the N12-bug8 pattern):
that is the only thread that sees the SDK's per-request token, and the key is cached
per process so nested threads and the CLI child's environment find it.

Job markers (aligned with the N12 evaluation-worker protocol where cheap):
``status.json`` on start, ``done.json`` beside ``result.json`` at the end, and a
``cancel`` marker the backend may write under the job prefix — checked before the
CLI is launched.

Importable without ``bedrock_agentcore`` so the payload handling is unit
testable; the container CMD is ``python -m skillopt.model.agentcore_worker``.
"""
from __future__ import annotations

import contextvars
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

from skillopt.model import opencode_backend
from skillopt.model.agentcore_runner import TAR_ROOT
from skillopt.model.opencode_backend import OPENCODE_BACKEND, configure_opencode_exec

SESSION_STORAGE_ROOT = "/mnt/workspace"
SUPPORTED_BACKENDS = frozenset({OPENCODE_BACKEND})
MODEL_CREDENTIAL_ENV = "LAUNCHPAD_MODEL_CREDENTIAL"
CANCEL_MARKER = "cancel"

# Process-wide key cache (memory only — never written anywhere) and the per-job failure
# of the request-thread exchange, consumed by the job so it is reported through
# result.json / done.json as an *infrastructure* error (N10v3-bug3 semantics).
_MODEL_API_KEY: dict[str, str] = {}
# LAUNCHPAD PATCH (N-R6): keys of extra OpenAI-compatible providers, per credential provider.
_PROVIDER_API_KEYS: dict[str, str] = {}
_MODEL_KEY_ERRORS: dict[str, Exception] = {}


def _utc_now() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def resolve_model_api_key(
    *,
    token_getter=None,
    exchange=None,
    provider_name: str | None = None,
    region: str | None = None,
) -> str:
    """The Global Bedrock API key for this worker session, via Identity.

    Mirrors the runtime template's ``_resolve_bedrock_api_key()``: the Runtime-injected
    workload access token (the invoke carries ``runtimeUserId``) is exchanged for the
    API-key credential provider named by ``LAUNCHPAD_MODEL_CREDENTIAL``. Raises
    ``RuntimeError`` (never returns "") when either step fails; the key value is only
    ever placed in the CLI child's environment.
    """
    provider = (provider_name or os.environ.get(MODEL_CREDENTIAL_ENV, "")).strip()
    if not provider:
        raise RuntimeError(f"{MODEL_CREDENTIAL_ENV} is not set on the worker runtime")
    region = (region or os.environ.get("AWS_REGION", "")).strip()
    if token_getter is None or exchange is None:
        from bedrock_agentcore.runtime.context import BedrockAgentCoreContext
        from bedrock_agentcore.services.identity import IdentityClient

        token_getter = token_getter or BedrockAgentCoreContext.get_workload_access_token
        if exchange is None:
            identity = IdentityClient(region)

            def exchange(token: str) -> str:  # noqa: F811 - late binding by design
                response = identity.dp_client.get_resource_api_key(
                    workloadIdentityToken=token, resourceCredentialProviderName=provider
                )
                return str(response.get("apiKey") or "")

    token = token_getter()
    if not token:
        raise RuntimeError(
            "no workload access token on this invocation — the platform must send "
            "runtimeUserId so the Runtime injects one"
        )
    api_key = exchange(token)
    if not api_key:
        raise RuntimeError(f"Identity returned no apiKey for credential provider {provider!r}")
    return api_key


def _job_models(payload: dict) -> list[str]:
    """Every model id a job may address (target + judge), from the payload."""
    models = []
    for key in ("model", "target_model", "judge_model"):
        value = str(payload.get(key) or "").strip()
        if value:
            models.append(value)
    for value in payload.get("models") or []:
        if str(value).strip():
            models.append(str(value).strip())
    return list(dict.fromkeys(models))


def _extra_provider_keys(payload: dict) -> dict[str, str]:
    """LAUNCHPAD PATCH (N-R6): ``{api_key_env: credential_provider}`` for every extra
    OpenAI-compatible provider one of the job's models is served by."""
    needed: dict[str, str] = {}
    for model in _job_models(payload):
        _pid, spec, _bare = opencode_backend.provider_for_model(model)
        if spec and spec.get("api_key_env") and spec.get("credential_provider"):
            needed[str(spec["api_key_env"])] = str(spec["credential_provider"])
    return needed


def _prefetch_model_api_key(job_id: str, payload: dict | None = None) -> None:
    """N12-bug8 pattern: resolve the key(s) on the REQUEST thread and cache them per process.

    Returns nothing; a failure is parked for ``job_id`` so ``_run_exec_job`` raises it at
    the key step and the job's result.json / done.json carry the readable cause with
    ``error_kind: infrastructure`` — the runner's existing failure channel. N-R6: the key
    of an extra provider (SiliconFlow …) one of the job's models needs is resolved the same
    way, from ITS Identity credential provider, into ``_PROVIDER_API_KEYS``."""
    try:
        if not _MODEL_API_KEY.get("key"):
            _MODEL_API_KEY["key"] = resolve_model_api_key()
        for env_name, credential_provider in _extra_provider_keys(payload or {}).items():
            if not _PROVIDER_API_KEYS.get(credential_provider):
                _PROVIDER_API_KEYS[credential_provider] = resolve_model_api_key(
                    provider_name=credential_provider
                )
    except Exception as exc:  # noqa: BLE001 - reported by the job, never raised here
        _MODEL_KEY_ERRORS[job_id] = exc


def _model_api_key_for_job(job_id: str) -> str:
    """The cached key; the parked request-thread failure, if any, is raised first."""
    error = _MODEL_KEY_ERRORS.pop(job_id, None)
    if error is not None:
        raise error
    key = _MODEL_API_KEY.get("key")
    if key:
        return key
    key = resolve_model_api_key()
    _MODEL_API_KEY["key"] = key
    return key


def _session_storage_writable() -> bool:
    return os.path.isdir(SESSION_STORAGE_ROOT) and os.access(SESSION_STORAGE_ROOT, os.W_OK)


def _work_root(persistent: bool = False) -> str:
    """Where a job's work_dir lives. N10v4-bug1: LOCAL container disk by default — the
    Runtime's session-storage mount (``/mnt/workspace``) is a network filesystem on which
    sqlite locking does not work, and OpenCode keeps its db beside the work_dir. Only a
    job that asks for the persistent mount (``payload.persistent_workspace``) gets it,
    and only when it is mounted and writable."""
    if persistent and _session_storage_writable():
        return SESSION_STORAGE_ROOT
    return tempfile.gettempdir()


def _opencode_home(job_id: str) -> str:
    """The per-job OpenCode home (XDG_DATA/CACHE/CONFIG/STATE_HOME, TMPDIR, opencode.json):
    always on local disk, never on a network/session mount — see :func:`_work_root`."""
    return os.path.join(tempfile.gettempdir(), "opencode-homes", job_id)


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
    """Re-apply the client's exec config minus host-specific bits (``path``: the
    image's own binary is used)."""
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported backend: {backend!r}")
    retries = cfg.get("empty_response_retries")
    if retries is not None:
        opencode_backend.set_empty_response_retries(int(retries))
    configure_opencode_exec(effort=cfg.get("effort"))


def _run_backend(backend: str, **kwargs) -> tuple[str, str]:
    if backend in SUPPORTED_BACKENDS:
        return opencode_backend.run_opencode_exec(**kwargs)
    raise ValueError(f"Unsupported backend: {backend!r}")


def _cancel_requested(s3, bucket: str, job_prefix: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=f"{job_prefix}/{CANCEL_MARKER}")
        return True
    except Exception:  # noqa: BLE001 - NoSuchKey / 404 == not cancelled
        return False


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
    s3.put_object(
        Bucket=bucket,
        Key=f"{job_prefix}/status.json",
        Body=json.dumps(
            {"state": "RUNNING", "phase": "exec", "job_id": job_id, "updated_at": _utc_now()}
        ).encode("utf-8"),
    )

    response, raw, error = "", "", ""
    # N10v3-bug3: the host must tell an *infrastructure* failure (this worker could not
    # download, configure or obtain the model key — nothing of the task ran) from an
    # *exec* failure (the CLI ran and failed): the former is an invalid, excluded row,
    # the latter a scored artifact failure.
    error_kind = "infrastructure"
    work_dir = ""
    sync = None
    home = _opencode_home(job_id)
    try:
        # `_work_root()` stays a zero-arg seam (tests stub it); the mount is opt-in per job
        persistent = bool(payload.get("persistent_workspace"))
        root = _work_root(persistent=True) if persistent else _work_root()
        job_dir = os.path.join(root, "exec", job_id)
        if os.path.exists(job_dir):
            shutil.rmtree(job_dir)
        os.makedirs(job_dir)
        if os.path.exists(home):
            shutil.rmtree(home)
        os.makedirs(home)
        work_dir = os.path.join(job_dir, TAR_ROOT)

        obj = s3.get_object(Bucket=bucket, Key=f"{job_prefix}/in.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(obj["Body"].read()), mode="r:gz") as tar:
            tar.extractall(job_dir, filter="data")
        if not os.path.isdir(work_dir):
            raise ValueError(f"in.tar.gz is missing the {TAR_ROOT!r} root directory")

        _apply_exec_config(backend, dict(payload.get("exec_config") or {}))
        if _cancel_requested(s3, bucket, job_prefix):
            raise RuntimeError("job cancelled before the CLI was launched (cancel marker present)")
        # The CLI child reads the Global Bedrock key from its environment only; the key
        # was resolved on the request thread (``_prefetch_model_api_key``) and is cached.
        os.environ[opencode_backend.API_KEY_ENV] = _model_api_key_for_job(job_id)
        # LAUNCHPAD PATCH (N-R6): an extra provider's key (resolved on the request thread
        # from its own Identity credential provider) reaches the CLI child the same way.
        for env_name, credential_provider in _extra_provider_keys(payload).items():
            key = _PROVIDER_API_KEYS.get(credential_provider)
            if not key:
                key = resolve_model_api_key(provider_name=credential_provider)
                _PROVIDER_API_KEYS[credential_provider] = key
            os.environ[env_name] = key

        if sync_interval > 0:
            sync = _ProgressSync(s3, bucket, f"{job_prefix}/progress", work_dir, sync_interval)
            sync.start()

        error_kind = "exec"
        response, raw = _run_backend(
            backend,
            work_dir=work_dir,
            prompt=str(payload.get("prompt") or ""),
            model=str(payload.get("model") or ""),
            timeout=int(payload.get("timeout", 600)),
            home=home,
            **dict(payload.get("exec_kwargs") or {}),
        )
    except Exception:  # noqa: BLE001 - shipped back and surfaced locally
        error = traceback.format_exc()
    finally:
        if sync is not None:
            sync.stop()
        # the OpenCode home is per job and never part of out.tar.gz: drop it
        shutil.rmtree(home, ignore_errors=True)

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
    if error:
        result["error_kind"] = error_kind
    s3.put_object(
        Bucket=bucket,
        Key=f"{job_prefix}/result.json",
        Body=json.dumps(result).encode("utf-8"),
    )
    s3.put_object(
        Bucket=bucket,
        Key=f"{job_prefix}/done.json",
        Body=json.dumps(
            {
                "state": "FAILED" if error else "COMPLETED",
                "job_id": job_id,
                "error": error[-2000:] if error else "",
                "error_kind": error_kind if error else "",
                "response_chars": len(response),
                "finished_at": _utc_now(),
            }
        ).encode("utf-8"),
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
    if backend not in SUPPORTED_BACKENDS:
        raise ValueError(f"Unsupported backend: {backend!r}")
    bucket = str(payload.get("bucket") or "")
    job_prefix = str(payload.get("job_prefix") or "").strip("/")
    job_id = job_prefix.rsplit("/", 1)[-1]
    if not bucket or not job_id or "/" in job_id or ".." in job_id:
        raise ValueError(f"Unsafe bucket/job prefix: {bucket!r} {job_prefix!r}")

    # N12-bug8 pattern: the Runtime-injected workload token is a per-request contextvar
    # visible on THIS thread only — exchange it for the key here and cache it, so the
    # job's threads and the CLI child never depend on inheriting the request context.
    _prefetch_model_api_key(job_id, payload)

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

    # N10v3-bug2: BedrockAgentCoreApp hands the Runtime-injected workload access token
    # (and the request headers) to the entrypoint through per-request ``contextvars``.
    # A bare Thread starts with an EMPTY context, so ``resolve_model_api_key`` in the
    # job saw no token and every rollout failed. The key is now exchanged above, on the
    # request thread; the job still runs inside a copy of THIS request's context so the
    # headers and anything else request-scoped stay visible on its own thread.
    request_context = contextvars.copy_context()
    threading.Thread(
        target=request_context.run, args=(_background,), daemon=True, name=f"exec-{job_id}"
    ).start()
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
        "session_storage_writable": _session_storage_writable(),
        "opencode_home_root": os.path.dirname(_opencode_home("probe")),
        "work_root_writable": writable,
        "region": os.environ.get("AWS_REGION", ""),
        "backend": OPENCODE_BACKEND,
        "opencode_base_url_set": bool(os.environ.get(opencode_backend.BASE_URL_ENV)),
        "model_credential_set": bool(os.environ.get(MODEL_CREDENTIAL_ENV)),
    }
    if payload.get("versions"):
        info["opencode_version"] = _cli_version(
            [str(opencode_backend.get_opencode_exec_config()["path"]), "--version"]
        )
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
