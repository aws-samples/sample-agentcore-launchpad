"""Linux subprocess supervisor used by safe_run."""
from __future__ import annotations

import ctypes
import json
import os
import resource
import select
import signal
import subprocess
import sys
import time

from skillopt.envs.skilleval.inspectors._scratch_root import (
    ScratchLimits,
    _scan_descriptor,
)

_PR_SET_CHILD_SUBREAPER = 36
_PR_SET_PDEATHSIG = 1
_POLL_SECONDS = 0.01
_CLEANUP_SECONDS = 3.0
_termination_requested = False


def _request_termination(_signum, _frame) -> None:
    global _termination_requested
    _termination_requested = True


def _set_subreaper() -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _set_parent_death_signal(
    signum: int,
    expected_parent: int,
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, signum, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != expected_parent:
        os.kill(os.getpid(), signum)


def _children(pid: int) -> list[int]:
    try:
        with open(
            f"/proc/{pid}/task/{pid}/children",
            encoding="ascii",
        ) as source:
            raw = source.read()
    except (FileNotFoundError, ProcessLookupError):
        return []
    return [int(value) for value in raw.split()]


def _descendants(root_pid: int) -> set[int]:
    found: set[int] = set()
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in _children(parent):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def _reap_available() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _kill_and_reap(process: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + _CLEANUP_SECONDS
    while time.monotonic() < deadline:
        targets = _descendants(os.getpid())
        if process.poll() is None:
            targets.add(process.pid)
        for pid in targets:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        _reap_available()
        if not _children(os.getpid()) and process.poll() is not None:
            return True
        time.sleep(_POLL_SECONDS)
    return False


def _preexec(
    max_file_bytes: int | None,
    expected_parent: int,
) -> None:
    _set_parent_death_signal(signal.SIGKILL, expected_parent)
    if max_file_bytes is not None:
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (max_file_bytes, max_file_bytes),
        )


def _write_status(descriptor: int, payload: dict) -> None:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            break
        view = view[written:]


def _parent_is_alive(descriptor: int) -> bool:
    readable, _writable, _exceptional = select.select(
        [descriptor],
        [],
        [],
        0,
    )
    if not readable:
        return True
    return bool(os.read(descriptor, 1))


def supervise(config: dict, parent_liveness_fd: int) -> dict:
    _set_subreaper()
    signal.signal(signal.SIGTERM, _request_termination)
    signal.signal(signal.SIGINT, _request_termination)

    transaction_fd = config.get("transaction_fd")
    limits = None
    if transaction_fd is not None:
        limits = ScratchLimits(
            max_bytes=config["max_scratch_bytes"],
            max_entries=config["max_scratch_entries"],
            max_depth=config["max_scratch_depth"],
        )
    actual_env = {
        "PATH": config["path_env"],
        "HOME": config["home"],
        "LANG": "C.UTF-8",
    }
    inherited = tuple(config.get("pass_fds", ()))
    supervisor_pid = os.getpid()
    try:
        process = subprocess.Popen(
            config["command"],
            cwd=config["cwd"],
            env=actual_env,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            shell=False,
            close_fds=True,
            pass_fds=inherited,
            preexec_fn=(
                (
                    lambda: _preexec(
                        config["max_file_bytes"],
                        supervisor_pid,
                    )
                )
                if limits is not None
                else (
                    lambda: _set_parent_death_signal(
                        signal.SIGKILL,
                        supervisor_pid,
                    )
                )
            ),
        )
    except OSError as exc:
        return {
            "reason": "start",
            "detail": f"{type(exc).__name__}: {exc}",
            "returncode": None,
        }

    deadline = time.monotonic() + config["timeout"]
    reason = None
    detail = ""
    returncode = None
    while True:
        if not _parent_is_alive(parent_liveness_fd):
            reason = "cancelled"
            detail = "safe_run caller exited"
            break
        if _termination_requested:
            reason = "cancelled"
            detail = "supervisor termination requested"
            break
        if limits is not None:
            try:
                _scan_descriptor(transaction_fd, limits)
            except Exception as exc:
                reason = "scratch"
                detail = str(exc)
                break
        returncode = process.poll()
        if returncode is not None:
            break
        if time.monotonic() >= deadline:
            reason = "timeout"
            detail = f"command timed out after {config['timeout']} seconds"
            break
        time.sleep(_POLL_SECONDS)

    cleaned = _kill_and_reap(process)
    if returncode is None:
        returncode = process.poll()
    if not cleaned:
        reason = "cleanup"
        detail = "command descendants could not be reaped"
    return {
        "reason": reason,
        "detail": detail,
        "returncode": returncode,
    }


def main() -> int:
    if len(sys.argv) != 5:
        return 2
    config_fd = int(sys.argv[1])
    status_fd = int(sys.argv[2])
    parent_liveness_fd = int(sys.argv[3])
    parent_pid = int(sys.argv[4])
    try:
        _set_parent_death_signal(signal.SIGTERM, parent_pid)
        with os.fdopen(config_fd, "rb", closefd=True) as source:
            config = json.loads(source.read().decode("utf-8"))
        status = supervise(config, parent_liveness_fd)
    except BaseException as exc:
        status = {
            "reason": "supervisor",
            "detail": f"{type(exc).__name__}: {exc}",
            "returncode": None,
        }
    os.close(parent_liveness_fd)
    try:
        _write_status(status_fd, status)
    except BrokenPipeError:
        pass
    os.close(status_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
