"""Studio local-debug execution — run un-deployed generated agent code in a
subprocess and stream its stdout.

Ported from strands_studio_ui ``backend/main.py`` (origin/main). The one
substantive change from upstream is the interpreter: upstream runs generated
code with ``sys.executable`` (its backend env carries strands); the launchpad
control-plane backend is lean, so we spawn the dedicated interpreter provisioned
by ``scripts/setup_exec_env.sh`` (``settings.studio_exec_python``). Skills are
bundled into the run's temp workdir so ``Path(__file__).parent/"skills"``
resolves for local runs the same way the deploy-time packager arranges them.
"""

import asyncio
import logging
import os
import resource
import shutil
import signal
import stat
import tempfile
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger("launchpad.local_exec")


class ExecInterpreterUnavailable(RuntimeError):
    """The configured studio_exec_python does not exist on disk."""


class ExecUserUnavailable(RuntimeError):
    """`studio_exec_user` is configured but no such account exists."""


def local_exec_enabled(settings: Any = None) -> bool:
    """Whether the local-debug execution endpoints are served at all.

    Unset (`None`) derives from the run mode: this surface runs caller-supplied
    Python, so production refuses it unless an operator opts back in.
    """
    current = settings or get_settings()
    if current.studio_local_exec_enabled is None:
        return current.run_mode != "prod"
    return current.studio_local_exec_enabled


def disabled_message() -> str:
    return (
        "Local code execution is disabled in this deployment. It runs "
        "caller-supplied Python on the server, so it is off by default in "
        "production mode; set LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true to "
        "accept that risk and re-enable it."
    )


def interpreter_path() -> str:
    return get_settings().studio_exec_python


def interpreter_available() -> bool:
    return os.path.isfile(interpreter_path())


def missing_interpreter_message() -> str:
    return (
        f"Local execution interpreter not found at {interpreter_path()}. "
        "Run scripts/setup_exec_env.sh to provision it "
        "(or set LAUNCHPAD_STUDIO_EXEC_PYTHON to a python that has "
        "strands-agents installed)."
    )


# Host environment the execution subprocess may see. Everything else is dropped:
# the backend's own environment carries the ledger URL, LAUNCHPAD_* settings and
# whatever else the operator's shell holds (tokens, keys, CI secrets), none of
# which caller-supplied code has any business reading.
_ENV_ALLOWLIST = frozenset({
    "PATH", "HOME", "TMPDIR", "TZ", "LANG", "LC_ALL", "LC_CTYPE",
    # TLS trust stores — omitting these breaks HTTPS on some hosts.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    # Where the deploy-time packager arranges bundled skills.
    "STUDIO_SKILLS_DIR",
})

# Forwarded only when `studio_exec_forward_aws_credentials` is on. The default
# Mantle path genuinely needs these: generated code builds
# OpenAIResponsesModel(bedrock_mantle_config=...) and the SDK mints a bearer token
# from the ambient credentials, which is why local debug works off the operator's
# AWS profile with no BEDROCK_API_KEY (see scripts/setup_exec_env.sh). Turning the
# forward off is the hardened posture and requires the caller to supply a key.
_AWS_CREDENTIAL_ENV = frozenset({
    "AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME", "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
})


def build_execution_env(
    openai_api_key: str | None = None,
    bedrock_api_key: str | None = None,
    settings: Any = None,
) -> dict[str, str]:
    """Environment for the execution subprocess.

    Built from an allowlist rather than `os.environ.copy()` — this process runs
    caller-supplied Python, so it starts from nothing and is handed only what the
    generated code needs.
    """
    current = settings or get_settings()
    forward_aws = current.studio_exec_forward_aws_credentials
    allowed = _ENV_ALLOWLIST | (_AWS_CREDENTIAL_ENV if forward_aws else frozenset())
    env = {
        name: value for name, value in os.environ.items()
        if name in allowed and value
    }
    # Skip strands tool consent prompts (would hang headless subprocess runs)
    env["BYPASS_TOOL_CONSENT"] = "true"
    env["STRANDS_NON_INTERACTIVE"] = "true"
    # Generated BedrockModel calls need a region; fall back to the platform
    # default when the ambient env has none.
    region = (
        os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        or current.region
    )
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region
    if not forward_aws:
        # Belt to the firewall rule's braces: botocore honors this, though code
        # that reaches for the metadata endpoint itself does not — which is why
        # the uid egress rule, not this flag, is what actually blocks IMDS.
        env["AWS_EC2_METADATA_DISABLED"] = "true"
    if openai_api_key:
        env["OPENAI_API_KEY"] = openai_api_key
    if bedrock_api_key:
        env["BEDROCK_API_KEY"] = bedrock_api_key
    return env


def _exec_user_ids(settings: Any = None) -> tuple[int, int] | None:
    """`(uid, gid)` of the configured dedicated execution user, or None.

    Empty configuration means "keep the backend's uid" — the tier-1 posture, where
    resource limits and the environment allowlist apply but the child still shares
    the backend's identity and can reach the instance metadata service.
    """
    current = settings or get_settings()
    name = (current.studio_exec_user or "").strip()
    if not name:
        return None
    import pwd

    try:
        record = pwd.getpwnam(name)
    except KeyError:
        raise ExecUserUnavailable(
            f"studio_exec_user {name!r} does not exist on this host. Run "
            "scripts/setup_exec_env.sh --hardened to create it, or clear the "
            "setting to run the subprocess as the backend user."
        ) from None
    return record.pw_uid, record.pw_gid


def build_spawn_kwargs(settings: Any = None) -> dict[str, Any]:
    """Isolation arguments shared by every place the platform spawns
    caller-supplied code: its own session, resource ceilings, and a drop to the
    dedicated execution user when one is configured.

    The uid/gid drop uses subprocess's `user`/`group` arguments rather than doing
    it inside `preexec_fn`: those run in CPython's C child helper, whereas
    `preexec_fn` executes arbitrary Python after a fork and is documented as
    deadlock-prone in a process that has threads — and this backend runs deploy
    jobs on background threads. Only the `setrlimit` calls remain in `preexec_fn`
    (there is no keyword for them); their values are computed here, before the
    fork, so the child does nothing but issue syscalls.

    CPython applies `user`/`group` before `preexec_fn`, so the limits are lowered
    as the unprivileged user. That is fine — lowering a soft limit never needs
    privilege.
    """
    current = settings or get_settings()
    limits = [
        (resource.RLIMIT_AS, current.studio_exec_memory_mb * 1024 * 1024),
        (resource.RLIMIT_CPU, current.studio_exec_cpu_seconds),
        (resource.RLIMIT_NPROC, current.studio_exec_max_processes),
        (resource.RLIMIT_FSIZE, current.studio_exec_max_file_mb * 1024 * 1024),
    ]

    def apply_limits() -> None:  # pragma: no cover - runs in the forked child
        for which, value in limits:
            try:
                resource.setrlimit(which, (value, value))
            except (ValueError, OSError):
                # Below an existing hard cap, or unsupported on this platform —
                # the timeout and process-group kill still bound the run.
                pass

    kwargs: dict[str, Any] = {
        "start_new_session": True,  # own process group, so kill_process_group works
        "preexec_fn": apply_limits,
    }
    ids = _exec_user_ids(current)
    if ids is not None:
        uid, gid = ids
        kwargs["user"] = uid
        kwargs["group"] = gid
    return kwargs


def kill_process_group(process: "asyncio.subprocess.Process") -> None:
    """Kill the subprocess and everything it spawned (start_new_session=True
    makes the subprocess its own process-group leader)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        # Process already gone (or not ours) — fall back to a direct kill
        try:
            process.kill()
        except (ProcessLookupError, OSError):
            pass


def chunk_to_sse(chunk_str: str) -> str:
    """Encode a stdout chunk as one SSE event.

    Frontend decoding contract (debug-client): within an event, an empty
    ``data: `` line represents a newline character and non-empty ``data:``
    lines are concatenated as-is. So each ``\\n`` in the chunk becomes its own
    empty ``data: `` line and each text segment its own ``data: <segment>``
    line — the decoded event text is then exactly the chunk, regardless of
    where subprocess read() boundaries fall.
    """
    lines = []
    for i, segment in enumerate(chunk_str.split("\n")):
        if i > 0:
            lines.append("data: ")  # the newline separator itself
        if segment:
            lines.append(f"data: {segment}")
    if not lines:
        return ""
    return "\n".join(lines) + "\n\n"


def bundle_skills_for_workdir(code: str, workdir: str) -> None:
    """Download any APPROVED skills the code references into ``workdir/skills/``
    so a local run resolves them like a deployed one. Never raises — a skill
    problem must not sink a local debug run (mirrors the deploy-time bundler)."""
    from pathlib import Path

    try:
        from app.deployer.zip_runtime import bundle_skills_into

        bundle_skills_into(code, Path(workdir), lambda m: logger.info("skill bundle: %s", m))
    except Exception as exc:  # noqa: BLE001 — skills are best-effort for local debug
        logger.warning("skill bundling skipped (%s)", type(exc).__name__)


async def spawn_execution_subprocess(
    code: str,
    input_data: str | None,
    openai_api_key: str | None = None,
    bedrock_api_key: str | None = None,
) -> tuple["asyncio.subprocess.Process", str]:
    """Write code to a temp workspace and spawn it as ``python -u code.py
    [--user-input ...]`` with the studio exec interpreter. Returns
    ``(process, workdir)``. Caller owns cleanup."""
    if not interpreter_available():
        raise ExecInterpreterUnavailable(missing_interpreter_message())

    settings = get_settings()
    workdir = tempfile.mkdtemp(prefix="strands_exec_")
    code_file = os.path.join(workdir, "generated_agent.py")
    with open(code_file, "w", encoding="utf-8") as f:
        f.write(code)

    bundle_skills_for_workdir(code, workdir)
    grant_workdir_to_exec_user(workdir, settings)

    cmd = [interpreter_path(), "-u", code_file]
    if input_data is not None:
        cmd.extend(["--user-input", input_data])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_execution_env(openai_api_key, bedrock_api_key, settings),
        **build_spawn_kwargs(settings),
    )
    return process, workdir


def grant_workdir_to_exec_user(workdir: str, settings: Any = None) -> None:
    """Let the dedicated execution user read the run's workspace.

    `mkdtemp` is 0700 and owned by the backend user, so after the uid drop the
    child could not read its own code. Hands the tree to that user rather than
    widening the mode for everyone on the host.
    """
    ids = _exec_user_ids(settings)
    if ids is None:
        return
    uid, gid = ids
    for root, dirs, files in os.walk(workdir):
        for name in (*dirs, *files):
            os.chown(os.path.join(root, name), uid, gid)
    os.chown(workdir, uid, gid)
    os.chmod(workdir, stat.S_IRWXU)


async def execute_strands_code(
    code: str,
    input_data: str | None = None,
    openai_api_key: str | None = None,
    bedrock_api_key: str | None = None,
) -> str:
    """Run generated agent code in an isolated subprocess and return its stdout.

    The generated-code contract guarantees an argparse ``--user-input``
    entrypoint, so the code runs exactly as it would from the command line. A
    non-zero exit raises with stderr content; a missing strands install returns
    a friendly message (parity with upstream)."""
    timeout = get_settings().execute_timeout_s
    process = None
    workdir = None
    try:
        process, workdir = await spawn_execution_subprocess(
            code, input_data, openai_api_key, bedrock_api_key
        )
        logger.info("execution subprocess started — pid %s, timeout %ss", process.pid, timeout)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            logger.error("execution timed out after %ss — killing process group", timeout)
            kill_process_group(process)
            await process.wait()
            raise RuntimeError(
                f"Code execution timed out after {timeout:g} seconds"
            ) from None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if process.returncode != 0:
            logger.error("execution subprocess failed — exit code %s", process.returncode)
            # Parity with upstream: a missing strands install returns a friendly
            # message as output instead of raising.
            if ("ModuleNotFoundError" in stderr or "ImportError" in stderr) and "strands" in stderr:
                return (
                    "Strands Agent SDK not available in the local execution "
                    "interpreter. Run scripts/setup_exec_env.sh.\n"
                    f"Error: {stderr.strip()}"
                )
            raise RuntimeError(
                stderr.strip() or f"Code execution failed with exit code {process.returncode}"
            )

        logger.info("execution completed, output length %s", len(stdout))
        return stdout if stdout else "Code executed successfully (no output)"
    finally:
        if process is not None and process.returncode is None:
            kill_process_group(process)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
