"""Bounded subprocess execution for trusted inspector adapters."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

from ._scratch import current_scratch_transaction
from ._secure_files import current_evidence_fds
from .base import (
    MAX_COMMAND_OUTPUT_CHARS,
    MAX_COMMAND_TIMEOUT_SECONDS,
    EvaluationError,
    InspectionError,
    _absolute_path,
    _open_real_directory,
    bounded_diagnostic,
)


def _bounded_pipe_reader(
    pipe,
    chunks: list[bytes],
    totals: list[int],
    errors: list[BaseException],
) -> None:
    try:
        while True:
            chunk = pipe.read(8_192)
            if not chunk:
                break
            totals[0] += len(chunk)
            remaining = MAX_COMMAND_OUTPUT_CHARS - sum(map(len, chunks))
            if remaining > 0:
                chunks.append(chunk[:remaining])
    except BaseException as exc:
        errors.append(exc)
    finally:
        pipe.close()


def _decode_output(chunks: list[bytes], total: int) -> str:
    captured = b"".join(chunks).decode("utf-8", errors="replace")
    if total > MAX_COMMAND_OUTPUT_CHARS:
        captured += (
            f"\n...[truncated {total - MAX_COMMAND_OUTPUT_CHARS} bytes]..."
        )
    return captured


def _validate_directory(path: str, label: str) -> str:
    absolute = _absolute_path(path, label)
    transaction = current_scratch_transaction()
    if transaction is not None and absolute == transaction.proc_path:
        if not os.path.isdir(absolute):
            raise InspectionError(f"{label} transaction is unavailable")
        return absolute
    descriptor = _open_real_directory(absolute, label)
    os.close(descriptor)
    return absolute


def _supervisor_config(
    command: list[str],
    *,
    timeout: int | float,
    cwd: str,
    home: str,
    pass_fds: tuple[int, ...],
) -> tuple[dict, tuple[int, ...]]:
    transaction = current_scratch_transaction()
    payload_fds = set(pass_fds)
    payload_fds.update(current_evidence_fds())
    supervisor_fds = set(payload_fds)
    config = {
        "command": command,
        "timeout": timeout,
        "cwd": cwd,
        "home": home,
        "path_env": os.environ.get("PATH", "/usr/bin:/bin"),
        "transaction_fd": None,
        "pass_fds": [],
    }
    if transaction is not None:
        payload_fds.add(transaction.descriptor)
        supervisor_fds.add(transaction.descriptor)
        supervisor_fds.add(transaction.outer_descriptor)
        config.update(
            {
                "transaction_fd": transaction.outer_descriptor,
                "max_file_bytes": transaction.remaining_bytes(),
                "max_scratch_bytes": transaction.limits.max_bytes,
                "max_scratch_entries": transaction.limits.max_entries,
                "max_scratch_depth": transaction.limits.max_depth,
            }
        )
    for descriptor in supervisor_fds:
        try:
            os.fstat(descriptor)
        except OSError as exc:
            raise InspectionError(
                "pass_fds contains an unavailable descriptor"
            ) from exc
    config["pass_fds"] = sorted(payload_fds)
    return config, tuple(sorted(supervisor_fds))


def _start_supervisor(
    config: dict,
    inherited_fds: tuple[int, ...],
) -> tuple[subprocess.Popen[bytes], int, int]:
    config_read, config_write = os.pipe()
    status_read, status_write = os.pipe()
    liveness_read, liveness_write = os.pipe()
    supervisor_script = Path(__file__).with_name("_supervisor.py")
    repo_root = str(Path(__file__).resolve().parents[4])
    parent_pid = os.getpid()
    supervisor_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": config["home"],
        "LANG": "C.UTF-8",
        "PYTHONPATH": repo_root,
    }
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                str(supervisor_script),
                str(config_read),
                str(status_write),
                str(liveness_read),
                str(parent_pid),
            ],
            cwd=os.path.abspath(os.sep),
            env=supervisor_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
            pass_fds=(
                config_read,
                status_write,
                liveness_read,
                *inherited_fds,
            ),
            start_new_session=True,
        )
    except Exception:
        os.close(config_read)
        os.close(config_write)
        os.close(status_read)
        os.close(status_write)
        os.close(liveness_read)
        os.close(liveness_write)
        raise
    os.close(config_read)
    os.close(status_write)
    os.close(liveness_read)
    try:
        payload = json.dumps(config, separators=(",", ":")).encode("utf-8")
        with os.fdopen(config_write, "wb", closefd=True) as destination:
            destination.write(payload)
    except Exception:
        process.terminate()
        process.wait()
        os.close(status_read)
        os.close(liveness_write)
        raise
    return process, status_read, liveness_write


def _terminate_and_wait(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=4)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def safe_run(
    command: list[str],
    *,
    timeout: int | float = 120,
    cwd: str,
    home: str,
    pass_fds: tuple[int, ...] = (),
) -> subprocess.CompletedProcess[str]:
    """Run argv under a subreaper supervisor and bounded environment."""
    if (
        not isinstance(command, list)
        or not command
        or any(
            not isinstance(part, str) or not part or "\x00" in part
            for part in command
        )
    ):
        raise InspectionError("invalid inspector command")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
        or timeout > MAX_COMMAND_TIMEOUT_SECONDS
    ):
        raise InspectionError("command timeout must be positive and bounded")
    if (
        not isinstance(pass_fds, tuple)
        or any(
            isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or descriptor < 0
            for descriptor in pass_fds
        )
    ):
        raise InspectionError("pass_fds must contain valid descriptors")
    cwd = _validate_directory(cwd, "command cwd")
    home = _validate_directory(home, "command home")
    config, inherited_fds = _supervisor_config(
        command,
        timeout=timeout,
        cwd=cwd,
        home=home,
        pass_fds=pass_fds,
    )
    try:
        process, status_fd, liveness_fd = _start_supervisor(
            config,
            inherited_fds,
        )
    except OSError as exc:
        raise EvaluationError(
            "inspector command could not start: "
            f"{type(exc).__name__}: {bounded_diagnostic(exc)}"
        ) from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_total = [0]
    stderr_total = [0]
    reader_errors: list[BaseException] = []
    readers = [
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(
                process.stdout,
                stdout_chunks,
                stdout_total,
                reader_errors,
            ),
            daemon=True,
        ),
        threading.Thread(
            target=_bounded_pipe_reader,
            args=(
                process.stderr,
                stderr_chunks,
                stderr_total,
                reader_errors,
            ),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    try:
        try:
            process.wait(timeout=timeout + 8)
        except subprocess.TimeoutExpired:
            _terminate_and_wait(process)
        for reader in readers:
            reader.join(timeout=2)
        if any(reader.is_alive() for reader in readers):
            raise EvaluationError(
                "inspector command output reader did not finish"
            )
        if reader_errors:
            raise EvaluationError(
                "inspector command output could not be read: "
                f"{bounded_diagnostic(reader_errors[0])}"
            )
        stdout = _decode_output(stdout_chunks, stdout_total[0])
        stderr = _decode_output(stderr_chunks, stderr_total[0])
        with os.fdopen(status_fd, "rb", closefd=True) as source:
            status_data = source.read()
        status_fd = -1
        try:
            status = json.loads(status_data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise EvaluationError(
                "inspector supervisor returned an invalid status"
            ) from exc

        reason = status.get("reason")
        if reason == "timeout":
            raise EvaluationError(
                f"inspector command timed out after {timeout} seconds"
            )
        if reason == "start":
            raise EvaluationError(
                "inspector command could not start: "
                f"{bounded_diagnostic(status.get('detail', ''))}"
            )
        if reason is not None:
            # Every other supervisor-reported reason ("cancelled", "scratch",
            # "cleanup", "supervisor") describes the process-isolation
            # sandbox itself failing, not the command's own assessment of
            # its input -- an evaluation error, not an artifact failure.
            raise EvaluationError(
                f"inspector command {reason} failure: "
                f"{bounded_diagnostic(status.get('detail', ''))}"
            )
        returncode = status.get("returncode")
        completed = subprocess.CompletedProcess(
            command,
            int(returncode),
            stdout=stdout,
            stderr=stderr,
        )
        if returncode != 0:
            diagnostic = stderr or stdout or "(no output)"
            raise InspectionError(
                f"inspector command exited {returncode}: "
                f"{bounded_diagnostic(diagnostic, MAX_COMMAND_OUTPUT_CHARS)}"
            )
        return completed
    finally:
        try:
            os.close(liveness_fd)
        except OSError:
            pass
        _terminate_and_wait(process)
        if status_fd >= 0:
            os.close(status_fd)
        for reader in readers:
            reader.join(timeout=2)
