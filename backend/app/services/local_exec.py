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
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.config import DATA_DIR, get_settings

logger = logging.getLogger("launchpad.local_exec")


class ExecInterpreterUnavailable(RuntimeError):
    """The configured studio_exec_python does not exist on disk."""


class ExecUserUnavailable(RuntimeError):
    """`studio_exec_user` is configured but cannot be used on this host.

    Either the account does not exist, or the backend is not privileged enough to
    become it. Both are configuration problems, and both must surface as one
    legible message instead of a `PermissionError` from deep inside a spawn.
    """


def docker_backend(settings: Any = None) -> bool:
    current = settings or get_settings()
    return current.studio_exec_backend == "docker"


def local_exec_enabled(settings: Any = None) -> bool:
    """Whether the local-debug execution endpoints are served at all.

    Unset (`None`) derives from the run mode: this surface runs caller-supplied
    Python, so production refuses it unless an operator opts back in. Selecting
    the docker backend is itself that opt-in — the code then runs in a one-shot
    container, not on the control-plane host — so it satisfies the prod default.
    An explicit `false` stays a kill switch for both backends.
    """
    current = settings or get_settings()
    if current.studio_local_exec_enabled is None:
        return current.run_mode != "prod" or docker_backend(current)
    return current.studio_local_exec_enabled


def disabled_message() -> str:
    return (
        "Local code execution is disabled in this deployment. It runs "
        "caller-supplied Python on the server, so it is off by default in "
        "production mode; set LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true to "
        "accept that risk and re-enable it, or set "
        "LAUNCHPAD_STUDIO_EXEC_BACKEND=docker to run the code in a local "
        "container sandbox instead (see scripts/setup_exec_docker.sh)."
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


def _run_docker_probe(argv: list[str]) -> tuple[bool, str]:
    """Run a short docker CLI query; (ok, stderr-or-reason). Module-level so
    tests can monkeypatch it and stay daemon-free."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            argv, capture_output=True, text=True, timeout=15
        )
    except FileNotFoundError:
        return False, "the `docker` CLI is not installed"
    except subprocess.TimeoutExpired:
        return False, "the docker CLI did not answer within 15s"
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()
    return True, ""


def docker_exec_error(settings: Any = None) -> str | None:
    """Why the docker exec backend cannot run right now, or None if it can.

    Three legible refusals: daemon unreachable (or CLI missing / permission
    denied), exec image not built, and the hardened-credentials combination
    missing its network. Probed per request — a debug surface can afford the
    ~30 ms, and caching would serve stale 'image missing' after a build.
    """
    current = settings or get_settings()
    ok, detail = _run_docker_probe(["docker", "version", "--format", "{{.Server.Version}}"])
    if not ok:
        return (
            f"Docker exec backend selected but the docker daemon is not usable: {detail}. "
            "Install/start docker and make sure the backend user may talk to it "
            "(docker group), or switch LAUNCHPAD_STUDIO_EXEC_BACKEND back to subprocess."
        )
    ok, detail = _run_docker_probe(
        ["docker", "image", "inspect", current.studio_exec_docker_image]
    )
    if not ok:
        return (
            f"Studio exec image {current.studio_exec_docker_image!r} not found. "
            "Build it with scripts/setup_exec_docker.sh "
            "(or point LAUNCHPAD_STUDIO_EXEC_DOCKER_IMAGE at an existing image)."
        )
    if not current.studio_exec_forward_aws_credentials and not current.studio_exec_docker_network:
        return (
            "studio_exec_forward_aws_credentials=false needs "
            "studio_exec_docker_network set to the hardened network created by "
            "scripts/setup_exec_docker.sh --harden-net — without it the container "
            "could still reach the instance metadata service (IMDS hop limit "
            "permitting) and the credential-less posture would be an illusion."
        )
    return None


def runner_error(settings: Any = None) -> str | None:
    """Backend-aware 'can generated code run right now' check (message or None).

    The docker backend replaces the host-interpreter and exec-user
    preconditions with its own daemon/image ones.
    """
    current = settings or get_settings()
    if docker_backend(current):
        return docker_exec_error(current)
    if not interpreter_available():
        return missing_interpreter_message()
    return exec_user_error(current)


def preflight_error(settings: Any = None) -> tuple[str, str, int] | None:
    """Full endpoint gate as ``(error_code, message, http_status)``, or None.

    Shared by the execution and conversation routers so disabling one entrance
    and not the other can't happen by drift. Returned as a tuple (not an
    AppError) to keep this module free of the web layer.
    """
    current = settings or get_settings()
    if not local_exec_enabled(current):
        return ("studio.exec.disabled", disabled_message(), 403)
    if docker_backend(current):
        problem = docker_exec_error(current)
        if problem:
            return ("studio.exec.docker_unavailable", problem, 503)
        return None
    if not interpreter_available():
        return (
            "studio.exec.interpreter_unavailable", missing_interpreter_message(), 503
        )
    problem = exec_user_error(current)
    if problem:
        return ("studio.exec.user_unavailable", problem, 503)
    return None


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


# Host-filesystem paths (and host-locale plumbing) that must not leak into a
# container: they describe the *host* tree. AWS_* file-path variables are host
# paths too — forwarding them into a container that doesn't mount them would
# just break credential resolution confusingly.
_DOCKER_HOST_ONLY_ENV = frozenset({
    "PATH", "HOME", "TMPDIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "STUDIO_SKILLS_DIR",
    "AWS_SHARED_CREDENTIALS_FILE", "AWS_CONFIG_FILE", "AWS_WEB_IDENTITY_TOKEN_FILE",
})

# What the docker *CLI process* itself needs from the host environment (finding
# the daemon and its config); the container never sees these.
_DOCKER_CLI_ENV = frozenset({
    "PATH", "HOME", "TMPDIR",
    "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT",
    "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
})


@dataclass
class ExecInvocation:
    """Everything one spawn of caller-supplied code needs, backend-resolved."""

    argv: list[str]
    env: dict[str, str]
    spawn_kwargs: dict[str, Any]
    container_name: str | None = None


@dataclass
class ExecRun:
    """A started run: the child process plus what cleanup must reach.

    In docker mode ``process`` is the docker *CLI* client — killing its process
    group does not stop the container, so termination goes container-first.
    """

    process: Any
    workdir: str | None
    container_name: str | None = None

    def kill(self) -> None:
        kill_container(self.container_name)
        kill_process_group(self.process)

    def cleanup(self) -> None:
        if self.process is not None and self.process.returncode is None:
            self.kill()
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)


def new_container_name() -> str:
    return f"strands-exec-{uuid.uuid4().hex[:12]}"


def exec_workdir_base(settings: Any = None) -> str | None:
    """Where run workspaces are created: None (tempfile default, /tmp) on the
    subprocess backend, ``data/exec-runs`` on the docker backend.

    Docker bind-mount sources are resolved by the *daemon* in the root mount
    namespace, so a workdir in the backend's /tmp silently mounts empty under
    systemd `PrivateTmp=true` (the prod posture — hit live 2026-08-11:
    "can't open file '/work/generated_agent.py'"). DATA_DIR is host-visible.
    """
    current = settings or get_settings()
    if not docker_backend(current):
        return None
    base = DATA_DIR / "exec-runs"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def reap_orphan_containers(settings: Any = None) -> int:
    """Startup janitor: kill `strands-exec-*` containers left by a backend
    crash (`--rm` removes them once killed) and clear stale run workspaces
    under data/exec-runs — every dir there is dead after a restart, since exec
    runs are transient and conversation sessions are in-memory. No-op (0) on
    the subprocess backend or when docker is unavailable; never raises."""
    current = settings or get_settings()
    if not docker_backend(current):
        return 0
    base = exec_workdir_base(current)
    if base:
        for entry in os.listdir(base):
            shutil.rmtree(os.path.join(base, entry), ignore_errors=True)
    try:
        listing = subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["docker", "ps", "-q", "--filter", "name=strands-exec-"],
            capture_output=True, text=True, timeout=15,
        )
        names = listing.stdout.split()
        if listing.returncode != 0 or not names:
            return 0
        subprocess.run(  # noqa: S603
            ["docker", "kill", *names], capture_output=True, timeout=30
        )
        logger.info("reaped %d orphaned exec container(s)", len(names))
        return len(names)
    except Exception:  # noqa: BLE001 — a janitor must never block startup
        return 0


def kill_container(container_name: str | None) -> None:
    """Best-effort `docker kill` — the container may already have exited
    (`--rm` then removes it), which is fine."""
    if not container_name:
        return
    try:
        subprocess.run(  # noqa: S603 — fixed argv, no shell
            ["docker", "kill", container_name], capture_output=True, timeout=15
        )
    except Exception:  # noqa: BLE001 — termination must never raise
        pass


def _docker_container_env(exec_env: dict[str, str]) -> dict[str, str]:
    """Variables the *container* receives (name→value).

    Derived from the subprocess allowlist result so the two backends cannot
    drift, minus everything that names a host path. The credential posture is
    inherited unchanged: `build_execution_env` already decided whether AWS vars
    are present and whether AWS_EC2_METADATA_DISABLED is set.
    """
    return {k: v for k, v in exec_env.items() if k not in _DOCKER_HOST_ONLY_ENV}


def _docker_run_argv(
    code_path: str,
    workdir: str,
    script_args: list[str],
    container_env: dict[str, str],
    container_name: str,
    settings: Any,
) -> list[str]:
    """`docker run` argv for one sandboxed execution.

    Values for `-e` flags are deliberately *not* inlined (they would show in
    `ps` / audit logs on the host — API keys included): bare `-e KEY` makes the
    docker CLI read each value from its own process environment, which
    `build_exec_invocation` populates.
    """
    mem = f"{settings.studio_exec_memory_mb}m"
    cpu = settings.studio_exec_cpu_seconds
    fsize = settings.studio_exec_max_file_mb * 1024 * 1024
    argv = [
        shutil.which("docker") or "docker", "run", "--rm", "-i",
        "--name", container_name,
        "-v", f"{workdir}:/work",
        "-w", "/work",
        # The backend's own ids, so the bind-mounted workdir is readable and
        # writable on both sides and nothing root-owned is left on the host.
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m",
        "--memory", mem, "--memory-swap", mem,
        "--pids-limit", str(settings.studio_exec_max_processes),
        "--ulimit", f"cpu={cpu}:{cpu}",
        "--ulimit", f"fsize={fsize}:{fsize}",
    ]
    if settings.studio_exec_docker_network:
        argv += ["--network", settings.studio_exec_docker_network]
    # Generated code and the SDKs want a writable HOME; /tmp is the tmpfs.
    # Inlined (it is not a secret) rather than forwarded via a bare `-e HOME`,
    # which would have to override the docker CLI's own HOME and break its
    # ~/.docker config resolution.
    argv += ["-e", "HOME=/tmp"]
    for key in sorted(container_env):
        argv += ["-e", key]
    argv += [
        settings.studio_exec_docker_image,
        "python", "-u", f"/work/{os.path.basename(code_path)}",
        *script_args,
    ]
    return argv


def build_exec_invocation(
    code_path: str,
    workdir: str,
    script_args: list[str] | None = None,
    openai_api_key: str | None = None,
    bedrock_api_key: str | None = None,
    extra_env: dict[str, str] | None = None,
    settings: Any = None,
) -> ExecInvocation:
    """Backend-resolved argv/env/kwargs for one run of ``code_path``.

    ``code_path`` must live inside ``workdir`` (both spawn surfaces already
    arrange that); the docker backend relies on it to address the file as
    ``/work/<basename>`` behind the bind mount.
    """
    current = settings or get_settings()
    args = list(script_args or [])
    exec_env = build_execution_env(openai_api_key, bedrock_api_key, current)
    if extra_env:
        exec_env.update(extra_env)

    if not docker_backend(current):
        return ExecInvocation(
            argv=[current.studio_exec_python, "-u", code_path, *args],
            env=exec_env,
            spawn_kwargs=build_spawn_kwargs(current),
        )

    name = new_container_name()
    container_env = _docker_container_env(exec_env)
    # The CLI process needs the host's docker plumbing plus every value the
    # bare `-e KEY` flags forward into the container.
    cli_env = {
        k: v for k, v in os.environ.items() if k in _DOCKER_CLI_ENV and v
    }
    cli_env.update(container_env)
    return ExecInvocation(
        argv=_docker_run_argv(code_path, workdir, args, container_env, name, current),
        env=cli_env,
        # No preexec rlimits, no uid drop: --ulimit/--pids-limit/--memory and
        # --user replace both. The docker CLI still gets its own process group
        # so a client-side kill can't orphan our reader tasks.
        spawn_kwargs={"start_new_session": True},
        container_name=name,
    )


def _exec_user_ids(settings: Any = None) -> tuple[int, int] | None:
    """`(uid, gid)` of the configured dedicated execution user, or None.

    Empty configuration means "keep the backend's uid" — the tier-1 posture, where
    resource limits and the environment allowlist apply but the child still shares
    the backend's identity and can reach the instance metadata service. The docker
    backend never drops uid host-side (isolation is the container's), so it is
    always None there.
    """
    current = settings or get_settings()
    if docker_backend(current):
        return None
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
    if os.geteuid() != 0:
        # Becoming another user needs privilege: `subprocess(user=…)` fails with
        # EPERM, and so does chowning the workdir to it. `make dev` and start.py
        # run the backend as the operator's own account, so this is the *common*
        # case — it has to be a stated precondition rather than a spawn-time
        # PermissionError with no explanation.
        raise ExecUserUnavailable(
            f"studio_exec_user {name!r} is configured, but this backend runs as "
            f"uid {os.geteuid()} and only root can switch to another user. Run the "
            "backend as root to use the dedicated execution user, or clear "
            "studio_exec_user — the subprocess then keeps the backend's uid, "
            "which leaves the instance metadata service reachable from it."
        )
    return record.pw_uid, record.pw_gid


def exec_user_error(settings: Any = None) -> str | None:
    """The reason `studio_exec_user` cannot be honored, or None if it can.

    Lets the execution endpoints refuse up front with an actionable message
    instead of spawning and failing halfway through a run.
    """
    try:
        _exec_user_ids(settings)
    except ExecUserUnavailable as exc:
        return str(exc)
    return None


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
    if docker_backend(current):
        # The child here is the docker CLI client; the container's ceilings come
        # from --memory/--ulimit/--pids-limit flags, not host rlimits.
        return {"start_new_session": True}
    ids = _exec_user_ids(current)
    limits = [
        (resource.RLIMIT_AS, current.studio_exec_memory_mb * 1024 * 1024),
        (resource.RLIMIT_CPU, current.studio_exec_cpu_seconds),
        (resource.RLIMIT_FSIZE, current.studio_exec_max_file_mb * 1024 * 1024),
    ]
    if ids is not None:
        # RLIMIT_NPROC counts processes AND threads per *uid*, not per process,
        # so it is only a ceiling worth setting under the dedicated execution
        # user, whose uid owns nothing else. Applied to the backend's own uid it
        # counts the operator's entire session (a dev box easily runs thousands
        # of threads), so every pthread_create in the child fails with "can't
        # start new thread" — and strands MCP clients each need one.
        limits.append((resource.RLIMIT_NPROC, current.studio_exec_max_processes))

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
) -> ExecRun:
    """Write code to a temp workspace and run it as ``python -u code.py
    [--user-input ...]`` on the configured backend (host exec interpreter or a
    one-shot docker container). Caller owns cleanup via the returned
    :class:`ExecRun`."""
    settings = get_settings()
    if not docker_backend(settings) and not interpreter_available():
        raise ExecInterpreterUnavailable(missing_interpreter_message())

    workdir = tempfile.mkdtemp(prefix="strands_exec_", dir=exec_workdir_base(settings))
    code_file = os.path.join(workdir, "generated_agent.py")
    with open(code_file, "w", encoding="utf-8") as f:
        f.write(code)

    bundle_skills_for_workdir(code, workdir)
    grant_workdir_to_exec_user(workdir, settings)

    script_args = ["--user-input", input_data] if input_data is not None else []
    invocation = build_exec_invocation(
        code_file, workdir, script_args, openai_api_key, bedrock_api_key,
        settings=settings,
    )

    # Code path and input are distinct argv values; create_subprocess_exec has no shell.
    # nosemgrep: dangerous-asyncio-create-exec-audit
    process = await asyncio.create_subprocess_exec(
        *invocation.argv,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=invocation.env,
        **invocation.spawn_kwargs,
    )
    return ExecRun(process, workdir, invocation.container_name)


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
    run: ExecRun | None = None
    try:
        run = await spawn_execution_subprocess(
            code, input_data, openai_api_key, bedrock_api_key
        )
        process = run.process
        logger.info("execution subprocess started — pid %s, timeout %ss", process.pid, timeout)

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except TimeoutError:
            logger.error("execution timed out after %ss — killing run", timeout)
            run.kill()
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
        if run is not None:
            run.cleanup()
