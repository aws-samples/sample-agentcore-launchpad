"""The evaluation worker's side of the eval / canary principal token (per-user policy D5;
contract I9 v2 — amendment 10 in docs/cn/interfaces/policy-principal-contracts.md).

A job whose spec carries ``principal_tokens: {key}`` was submitted while the Gateway
interceptor governs its target: the hub keeps ONE hub-signed system token for the whole job
(``p = System::"eval"`` / ``"canary"``) in ``evaluations/<job_id>/tokens.json`` =
``{schema, job_id, written_at, token}`` and re-signs it while the job runs. Before each turn
the worker reads the file and puts that token into ``launchpad_meta.principal`` of every
session; it re-reads the file when the token it holds has less than ``REREAD_MARGIN_S`` left.
A job without a readable token runs token-less — the Gateway then decides exactly as for any
token-less caller (a system preset passes; an ordinary agent is refused ``principal_missing``,
visible in the hub's Decisions) — so nothing here ever stops a sample.

Read-compat: a ``…/1`` file (one token per session, written by a hub before amendment 10) is
still honoured per session during the rollover.

The token is bearer material: never logged, never copied into an invocation line, a result,
an artefact or a span; only the job id is ever written. A job without the spec flag is
untouched (``with_principal`` returns the session as it came).
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import weakref
from collections.abc import Callable
from typing import Any

SPEC_FLAG = "principal_tokens"
SCHEMA = "launchpad.policy.eval-tokens/2"
SCHEMA_V1 = "launchpad.policy.eval-tokens/1"
REREAD_MARGIN_S = 120

log = logging.getLogger("launchpad.eval.worker")


def _exp(token: str) -> float | None:
    """The token's ``exp`` claim, read without trust (the Gateway verifies)."""
    try:
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        exp = claims.get("exp") if isinstance(claims, dict) else None
        return float(exp) if isinstance(exp, int | float) else None
    except Exception:  # noqa: BLE001
        return None


class JobTokens:
    """One job's token file, read lazily and re-read when the held token runs low."""

    def __init__(self, read: Callable[[str], bytes | None], key: str, job_id: str, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._read = read
        self._key = key
        self._job_id = job_id
        self._clock = clock
        self._lock = threading.Lock()
        self._loaded = False
        self._token: str | None = None  # I9 v2: the job's one token
        self._per_session: dict[str, str] = {}  # I9 v1 read-compat
        self._warned = False

    def _load(self) -> None:
        self._loaded = True
        try:
            raw = self._read(self._key)
            data = json.loads(raw) if raw else None
        except Exception:  # noqa: BLE001 — unreadable = no token; the run goes token-less
            data = None
        if not isinstance(data, dict) or data.get("job_id") != self._job_id:
            return
        if data.get("schema") == SCHEMA and isinstance(data.get("token"), str):
            self._token = data["token"]
        elif data.get("schema") == SCHEMA_V1 and isinstance(data.get("tokens"), dict):
            self._per_session = {str(k): v for k, v in data["tokens"].items()
                                 if isinstance(v, str)}

    def _held(self, session_id: str) -> str | None:
        return self._token or self._per_session.get(session_id)

    def _fresh(self, token: str | None) -> bool:
        exp = _exp(token) if token else None
        return exp is not None and exp - self._clock() >= REREAD_MARGIN_S

    def token_for(self, session_id: str) -> str | None:
        """The job's token (v1: the session's) with at least ``REREAD_MARGIN_S`` left — re-read
        once when it runs low — or ``None``: the turn goes token-less and the Gateway decides."""
        with self._lock:
            if not self._loaded or not self._fresh(self._held(session_id)):
                self._load()
            token = self._held(session_id)
            exp = _exp(token) if token else None
            if token is None or exp is None or exp <= self._clock():
                if not self._warned:
                    self._warned = True
                    log.warning("job %s: no principal token (%s) — the gateway decides",
                                self._job_id, "expired" if token else "none readable")
                return None
            return token


_JOBS: weakref.WeakKeyDictionary[Any, JobTokens] = weakref.WeakKeyDictionary()
_JOBS_LOCK = threading.Lock()


def for_sink(spec: dict[str, Any], sink: Any) -> JobTokens | None:
    """The job's :class:`JobTokens` (one per sink, dropped with it), or ``None`` for a job
    without the flag."""
    flag = spec.get(SPEC_FLAG)
    if not isinstance(flag, dict) or not flag.get("key"):
        return None
    with _JOBS_LOCK:
        held = _JOBS.get(sink)
        if held is None:
            held = JobTokens(sink.read_object, str(flag["key"]), str(sink.job.job_id))
            _JOBS[sink] = held
        return held


def with_principal(session: dict[str, Any], tokens: JobTokens | None) -> dict[str, Any]:
    """The session as one turn sends it: ``launchpad_meta.principal`` = the job's token (a
    copy — the token never lands on the job spec / the invocation line), or the session
    unchanged for a job without tokens or without a readable one (token-less turn)."""
    if tokens is None:
        return session
    token = tokens.token_for(str(session["session_id"]))
    if token is None:
        return session
    meta = dict(session.get("launchpad_meta") or {})
    meta["principal"] = token
    return {**session, "launchpad_meta": meta}
