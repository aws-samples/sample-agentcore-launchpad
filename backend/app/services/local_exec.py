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
import shutil
import signal
import tempfile
from typing import Any

from app.core.config import get_settings

logger = logging.getLogger("launchpad.local_exec")


class ExecInterpreterUnavailable(RuntimeError):
    """The configured studio_exec_python does not exist on disk."""


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


def build_execution_env(
    openai_api_key: str | None = None, bedrock_api_key: str | None = None
) -> dict[str, str]:
    """Environment for the execution subprocess: inherit the backend env, skip
    strands tool-consent prompts (they would hang a headless run), make sure a
    Bedrock region is present, and inject request-scoped API keys."""
    env = os.environ.copy()
    # Skip strands tool consent prompts (would hang headless subprocess runs)
    env["BYPASS_TOOL_CONSENT"] = "true"
    env["STRANDS_NON_INTERACTIVE"] = "true"
    # Generated BedrockModel calls need a region; fall back to the platform
    # default when the ambient env has none.
    region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or get_settings().region
    env["AWS_REGION"] = region
    env["AWS_DEFAULT_REGION"] = region
    # Skill library location for generated code (explicit, in case the copy()
    # above is ever replaced by an allowlist during a refactor)
    if os.environ.get("STUDIO_SKILLS_DIR"):
        env["STUDIO_SKILLS_DIR"] = os.environ["STUDIO_SKILLS_DIR"]
    if openai_api_key:
        env["OPENAI_API_KEY"] = openai_api_key
    if bedrock_api_key:
        env["BEDROCK_API_KEY"] = bedrock_api_key
    return env


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

    workdir = tempfile.mkdtemp(prefix="strands_exec_")
    code_file = os.path.join(workdir, "generated_agent.py")
    with open(code_file, "w", encoding="utf-8") as f:
        f.write(code)

    bundle_skills_for_workdir(code, workdir)

    cmd = [interpreter_path(), "-u", code_file]
    if input_data is not None:
        cmd.extend(["--user-input", input_data])

    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_execution_env(openai_api_key, bedrock_api_key),
        start_new_session=True,
    )
    return process, workdir


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
