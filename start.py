#!/usr/bin/env python3
"""Start the local AgentCore Launchpad stack in the background."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUN_DIR = ROOT / ".run"
LOG_DIR = RUN_DIR / "logs"
STATE_FILE = RUN_DIR / "launchpad.json"
CHECK_HOST = "127.0.0.1"


class LaunchError(RuntimeError):
    """A user-actionable local launch failure."""


@dataclass(frozen=True)
class Service:
    name: str
    cwd: Path
    command: tuple[str, ...]
    port: int
    health_path: str

    @property
    def log_file(self) -> Path:
        return LOG_DIR / f"{self.name}.log"

    @property
    def health_url(self) -> str:
        return f"http://{CHECK_HOST}:{self.port}{self.health_path}"


def _yaml_setting(key: str) -> Any:
    """Read one key from config/launchpad.yaml, or None when unavailable.

    Best-effort on purpose: this only feeds the pre-flight message below. The
    binding control is the per-request guard in `app.routers.auth`, so a wrong
    guess here changes when the operator learns about it, not whether the console
    is protected.
    """
    config_file = ROOT / "config" / "launchpad.yaml"
    if not config_file.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — pre-flight must never be the thing that breaks a launch
        return None
    return data.get(key) if isinstance(data, dict) else None


def _truthy(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # A hostname we cannot classify, or an empty host (which servers read as
        # "all interfaces") — treat it as reachable so the check fails closed.
        return False


def _check_console_is_protected(hosts: list[str]) -> None:
    """Refuse to launch an unauthenticated console on a reachable interface.

    Mirrors the request-time guard so the operator gets one clear message up
    front instead of a console that refuses every call.
    """
    if _truthy(os.environ.get("LAUNCHPAD_ALLOW_OPEN_CONSOLE")) or _yaml_setting(
        "allow_open_console"
    ):
        return
    if os.environ.get("LAUNCHPAD_AUTH_PASSWORD") or _yaml_setting("auth_password"):
        return
    reachable = sorted({host for host in hosts if not _is_loopback_host(host)})
    if not reachable:
        return
    raise LaunchError(
        "refusing to serve an unauthenticated console on "
        + ", ".join(reachable)
        + ".\n"
        "  Set auth_password in config/launchpad.yaml (or LAUNCHPAD_AUTH_PASSWORD)\n"
        "  to enable the login gate, or LAUNCHPAD_ALLOW_OPEN_CONSOLE=true to accept\n"
        "  an open console on a reachable interface."
    )


def _env_port(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        port = int(raw)
    except ValueError as exc:
        raise LaunchError(f"{name} must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise LaunchError(f"{name} must be between 1 and 65535, got {port}")
    return port


def _process_start_time(pid: int) -> str | None:
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = stat.rsplit(") ", 1)[1].split()
        return fields[19]
    except (IndexError, OSError):
        return None


def _record_is_alive(record: dict[str, Any]) -> bool:
    try:
        pid = int(record["pid"])
        expected_start = str(record["start_time"])
    except (KeyError, TypeError, ValueError):
        return False
    return _process_start_time(pid) == expected_start


def _load_state() -> dict[str, Any] | None:
    if not STATE_FILE.is_file():
        return None
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise LaunchError(f"cannot read lifecycle state {STATE_FILE}: {exc}") from exc
    if state.get("root") != str(ROOT) or not isinstance(state.get("services"), list):
        raise LaunchError(f"invalid lifecycle state in {STATE_FILE}")
    return state


def _write_state(mode: str, records: list[dict[str, Any]]) -> None:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    state = {
        "version": 1,
        "root": str(ROOT),
        "mode": mode,
        "started_at": datetime.now(UTC).isoformat(),
        "services": records,
    }
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def _signal_record(record: dict[str, Any], sig: signal.Signals) -> None:
    if not _record_is_alive(record):
        return
    try:
        os.killpg(int(record["pgid"]), sig)
    except ProcessLookupError:
        pass


def _wait_for_exit(records: list[dict[str, Any]], timeout: float) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    alive = [record for record in records if _record_is_alive(record)]
    while alive and time.monotonic() < deadline:
        # Deliberate shutdown polling interval; timeout_s bounds the wait.
        time.sleep(0.2)  # nosemgrep: arbitrary-sleep
        alive = [record for record in alive if _record_is_alive(record)]
    return alive


def _terminate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    owned = [record for record in records if _record_is_alive(record)]
    for record in reversed(owned):
        _signal_record(record, signal.SIGTERM)
    remaining = _wait_for_exit(owned, 10)
    for record in reversed(remaining):
        _signal_record(record, signal.SIGKILL)
    return _wait_for_exit(remaining, 2)


def stop() -> int:
    state = _load_state()
    if state is None:
        print("AgentCore Launchpad is not running under start.py.")
        return 0

    records = state["services"]
    owned = [record for record in records if _record_is_alive(record)]
    if not owned:
        STATE_FILE.unlink(missing_ok=True)
        print("AgentCore Launchpad is not running (removed stale lifecycle state).")
        return 0

    print("Stopping AgentCore Launchpad...")
    remaining = _terminate_records(owned)
    STATE_FILE.unlink(missing_ok=True)
    if remaining:
        names = ", ".join(str(record.get("name", record.get("pid"))) for record in remaining)
        raise LaunchError(f"could not stop: {names}")
    print("AgentCore Launchpad stopped.")
    return 0


def _port_is_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((CHECK_HOST, port)) == 0


def _ensure_command(name: str) -> None:
    if shutil.which(name) is None:
        raise LaunchError(f"required command not found: {name}")


def _ensure_frontend_dependencies(directory: Path) -> None:
    if (directory / "node_modules" / ".bin" / "vite").is_file():
        return
    print(f"Installing frontend dependencies in {directory.relative_to(ROOT)}...")
    subprocess.run(
        ["npm", "install", "--no-audit", "--no-fund"],
        cwd=directory,
        check=True,
    )


def _build_frontend(directory: Path) -> None:
    print(f"Building {directory.relative_to(ROOT)}...")
    subprocess.run(["npm", "run", "build"], cwd=directory, check=True)


def _service_definitions(prod: bool) -> tuple[list[Service], dict[str, str]]:
    mode = "prod" if prod else "dev"
    public_host = os.environ.get(
        "LAUNCHPAD_HOST",
        "0.0.0.0" if prod else CHECK_HOST,
    )
    api_host = os.environ.get(
        "LAUNCHPAD_API_HOST",
        "0.0.0.0" if prod else CHECK_HOST,
    )
    _check_console_is_protected([public_host, api_host])
    ports = {
        "platform_api": _env_port("PLATFORM_API_PORT", 8000),
        "platform_ui": _env_port("PLATFORM_UI_PORT", 5173),
    }
    if len(set(ports.values())) != len(ports):
        raise LaunchError("PLATFORM_API_PORT and PLATFORM_UI_PORT must be unique")

    platform_backend = [
        "uv",
        "run",
        "uvicorn",
        "app.main:app",
        "--host",
        api_host,
        "--port",
        str(ports["platform_api"]),
    ]
    if not prod:
        platform_backend.append("--reload")

    frontend_command = "preview" if prod else "dev"
    services = [
        Service(
            name="platform-backend",
            cwd=ROOT / "backend",
            command=tuple(platform_backend),
            port=ports["platform_api"],
            health_path="/api/health",
        ),
        Service(
            name="platform-frontend",
            cwd=ROOT / "frontend",
            command=(
                "npm",
                "run",
                frontend_command,
                "--",
                "--host",
                public_host,
                "--port",
                str(ports["platform_ui"]),
                "--strictPort",
            ),
            port=ports["platform_ui"],
            health_path="/",
        ),
    ]

    child_env = os.environ.copy()
    child_env.update(
        {
            "LAUNCHPAD_RUN_MODE": mode,
            "LAUNCHPAD_API": f"http://{CHECK_HOST}:{ports['platform_api']}",
            "PLATFORM_API_PORT": str(ports["platform_api"]),
            "PLATFORM_UI_PORT": str(ports["platform_ui"]),
        }
    )
    return services, child_env


def _http_ready(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status < 400
    except (OSError, urllib.error.URLError):
        return False


def _tail_log(path: Path, line_count: int = 20) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "(log unavailable)"
    return "\n".join(lines[-line_count:])


def _wait_until_ready(service: Service, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise LaunchError(
                f"{service.name} exited with code {return_code}\n"
                f"{_tail_log(service.log_file)}"
            )
        if _http_ready(service.health_url):
            return
        # Deliberate readiness polling interval; service.timeout_s bounds the wait.
        time.sleep(0.5)  # nosemgrep: arbitrary-sleep
    raise LaunchError(
        f"{service.name} did not become healthy at {service.health_url}\n"
        f"{_tail_log(service.log_file)}"
    )


def _print_running(mode: str, records: list[dict[str, Any]]) -> None:
    ports = {record["name"]: record["port"] for record in records}
    print(f"AgentCore Launchpad running ({mode}).")
    print(f"  Console:     http://localhost:{ports['platform-frontend']}")
    print(f"  API docs:    http://localhost:{ports['platform-frontend']}/api/docs")
    print(f"  Logs:        {LOG_DIR.relative_to(ROOT)}/")
    print("  Stop:        ./stop.sh")


def start(prod: bool) -> int:
    _ensure_command("uv")
    _ensure_command("npm")
    services, child_env = _service_definitions(prod)
    mode = "prod" if prod else "dev"

    existing = _load_state()
    if existing is not None:
        records = existing["services"]
        if records and all(_record_is_alive(record) for record in records):
            current_names = [record.get("name") for record in records]
            requested_names = [service.name for service in services]
            if existing.get("mode") == mode and current_names == requested_names:
                _print_running(mode, records)
                return 0
            raise LaunchError(
                "a different launch mode or service set is already running; "
                "run ./stop.sh before starting the requested stack"
            )
        print("Cleaning up a partial or stale previous launch...")
        _terminate_records(records)
        STATE_FILE.unlink(missing_ok=True)

    for service in services:
        if _port_is_listening(service.port):
            raise LaunchError(
                f"port {service.port} for {service.name} is already in use; "
                "override it with the corresponding *_PORT environment variable"
            )

    frontend_directories = [ROOT / "frontend"]
    for directory in frontend_directories:
        _ensure_frontend_dependencies(directory)
    if prod:
        for directory in frontend_directories:
            _build_frontend(directory)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    processes: dict[str, subprocess.Popen[bytes]] = {}
    try:
        for service in services:
            with service.log_file.open("ab") as log:
                log.write(
                    f"\n[{datetime.now(UTC).isoformat()}] starting {mode}\n".encode()
                )
                # Service commands are internal argv tuples; no shell is involved.
                process = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit
                    service.command,
                    cwd=service.cwd,
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            start_time = _process_start_time(process.pid)
            if start_time is None:
                raise LaunchError(f"could not inspect started process {process.pid}")
            record = {
                "name": service.name,
                "pid": process.pid,
                "pgid": process.pid,
                "start_time": start_time,
                "port": service.port,
                "log_file": str(service.log_file.relative_to(ROOT)),
                "command": list(service.command),
            }
            records.append(record)
            processes[service.name] = process
            _write_state(mode, records)

        for service in services:
            _wait_until_ready(service, processes[service.name])
    except (Exception, KeyboardInterrupt):
        _terminate_records(records)
        STATE_FILE.unlink(missing_ok=True)
        raise

    _print_running(mode, records)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Start the local AgentCore Launchpad stack in the background."
    )
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--prod",
        action="store_true",
        help="build the platform frontend and run without backend auto-reload",
    )
    action.add_argument("--stop", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        return stop() if args.stop else start(args.prod)
    except subprocess.CalledProcessError as exc:
        print(f"error: command failed with exit code {exc.returncode}", file=sys.stderr)
        return exc.returncode or 1
    except LaunchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
