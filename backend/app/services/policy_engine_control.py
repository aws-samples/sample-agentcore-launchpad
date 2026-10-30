"""The hub's client of the policy engine's control plane (stream A5; contract I5 in
docs/cn/interfaces/policy-principal-contracts.md, amendment 2: one control socket per shard).

JSON lines over ``/run/launchpad-policy-engine/control-<shard>.sock`` (on the hub host,
0660 ``lp-policy``): one request object per line in, one response object per line out.

* ``install_all`` — the set a publish / retire produced, installed on EVERY shard; any shard
  that refuses or cannot be reached fails the whole install (``EngineControlError``), so the
  caller leaves the previous set live (``policy.engine_install_failed``).
* ``authorize_stateless`` — never records; the invoke gate, simulator and narrowing ask any
  healthy shard (the first that answers).
* ``status`` — per-shard readiness, store epoch and installed set versions.

A socket error / timeout is "the engine cannot decide" — the caller's fail mode applies
(lead note 1). Nothing here runs unless the interceptor switch is on for the workspace.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable
from typing import Any

from app.policy import contracts

logger = logging.getLogger("launchpad.policy")

TIMEOUT_S = 2.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class EngineControlError(RuntimeError):
    """The engine refused or could not be reached. ``code``: ``unavailable`` (socket error /
    timeout / malformed answer) or ``refused`` (``ok: false``); ``errors`` = the engine's."""

    def __init__(self, code: str, message: str, errors: list[Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.errors = list(errors or [])


def socket_path(shard: int) -> str:
    return contracts.ENGINE_CONTROL_SOCKET.format(shard=shard)


def shard_count() -> int:
    return len(contracts.ENGINE_SHARD_PORTS)


def _call_socket(shard: int, message: dict[str, Any], *, timeout: float = TIMEOUT_S) -> dict:
    """One request / response line on ``shard``'s control socket."""
    data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(socket_path(shard))
            sock.sendall(data)
            buf = bytearray()
            while b"\n" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_RESPONSE_BYTES:
                    raise EngineControlError("unavailable", f"shard {shard}: answer too large")
    except EngineControlError:
        raise
    except OSError as exc:
        raise EngineControlError(
            "unavailable", f"shard {shard}: control socket {type(exc).__name__}") from None
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        out = json.loads(line)
    except ValueError:
        raise EngineControlError("unavailable", f"shard {shard}: malformed answer") from None
    if not isinstance(out, dict):
        raise EngineControlError("unavailable", f"shard {shard}: malformed answer")
    return out


# the transport seam: tests (and the stage-0 FakeEngine) replace it
call: Callable[..., dict[str, Any]] = _call_socket


_INSTALL_LOCK = threading.Lock()
# ws -> the last install message every shard accepted (this process): what a partial install
# rolls the early shards back to (N17-obs492, re-review F3). Lost on restart — then the
# reconcile's shard-disagreement check re-syncs with ``force``.
_LAST_INSTALLED: dict[str, dict[str, Any]] = {}


def install_all(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Install on every shard, one install at a time per process; the first refusal /
    unreachable shard raises AFTER the shards that already accepted are rolled back to the
    previous set (``force: true`` — the engine's version CAS otherwise refuses an older
    version), so the shards never stay on different sets (N17-obs492). A rollback the engine
    refuses or cannot receive is logged; the reconcile re-syncs it."""
    if message.get("op") != "install":
        raise ValueError("install_all sends an install message")
    ws = str(message.get("ws") or "")
    with _INSTALL_LOCK:
        answers = []
        for shard in range(shard_count()):
            try:
                out = call(shard, message)
            except EngineControlError:
                _roll_back(ws, range(shard))
                raise
            if out.get("ok") is not True:
                _roll_back(ws, range(shard))
                raise EngineControlError(
                    "refused", f"shard {shard} refused the install", out.get("errors"))
            answers.append(out)
        _LAST_INSTALLED[ws] = dict(message)
        return answers


def _roll_back(ws: str, shards: range) -> None:
    previous = _LAST_INSTALLED.get(ws)
    for shard in shards:
        if previous is None:
            logger.warning("policy engine: shard %d accepted %s for %s but another shard did "
                           "not and no previous set is known here — the reconcile re-syncs it",
                           shard, "the install", ws)
            continue
        try:
            out = call(shard, {**previous, "force": True})
        except EngineControlError as exc:
            logger.warning("policy engine: rollback of shard %d to %s failed: %s", shard,
                           previous.get("policy_set_version"), exc)
            continue
        if out.get("ok") is not True:
            logger.warning("policy engine: shard %d refused the rollback to %s: %s", shard,
                           previous.get("policy_set_version"), out.get("errors"))


def authorize_stateless(ws: str, requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The decisions of ``requests`` (I4 decide items with ``record: false``) from the first
    shard that answers; ``EngineControlError`` when none does."""
    last: EngineControlError | None = None
    for shard in range(shard_count()):
        try:
            out = call(shard, {"op": "authorize_stateless", "ws": ws, "requests": requests})
        except EngineControlError as exc:
            last = exc
            continue
        if out.get("ok") is not True:
            raise EngineControlError("refused", f"shard {shard} refused", out.get("errors"))
        return list(out.get("decisions") or [])
    raise last or EngineControlError("unavailable", "no engine shard answered")


def status() -> list[dict[str, Any]]:
    """Every shard's status (an unreachable one reports ``{shard, ready: false}``)."""
    out = []
    for shard in range(shard_count()):
        try:
            answer = call(shard, {"op": "status"})
            out.extend(answer.get("shards") or [{"shard": shard, "ready": False}])
        except EngineControlError as exc:
            out.append({"shard": shard, "ready": False, "error": exc.code})
    return out
