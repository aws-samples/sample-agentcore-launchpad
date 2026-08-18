"""Run-scoped, fingerprint-validated cache for agentic judge verdicts.

The cache lives under ``<base_dir>/<state_hash>/<task_id>.json`` (the
orchestrator points ``base_dir`` at ``<out_root>/judge_cache``). A record is
only a hit when both the lookup identity (``state_hash``, ``task_id``) and an
opaque ``fingerprint`` (aggregate evidence hash, rubric/artifact_checks hash,
judge backend/model, prompt version, inspector version, verdict schema
version -- composed by the caller) match exactly; malformed records or
fingerprint mismatches are ordinary cache misses, never judge failures.

Writes are atomic (temp file + fsync + ``os.replace``) and
``locked_record`` provides a per-key ``fcntl.flock`` so the orchestrator can
hold one lock across lookup, model execution, and write -- preventing two
concurrent evaluations from duplicating a judgment for the same key.
"""
from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from typing import Iterator

_SCHEMA_VERSION = 1
_RECORD_KEYS = frozenset({"schema_version", "fingerprint", "verdict"})


def _validate_key(value: str, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    if "/" in value or "\\" in value or ".." in value:
        raise ValueError(f"{label} must be filesystem-safe (no path separators or '..'): {value!r}")
    return value


def _read_record(path: str, fingerprint: object) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != _RECORD_KEYS:
        return None
    if payload.get("schema_version") != _SCHEMA_VERSION:
        return None
    if payload.get("fingerprint") != fingerprint:
        return None
    verdict = payload.get("verdict")
    if not isinstance(verdict, dict):
        return None
    return verdict


def _write_record(path: str, fingerprint: object, verdict: dict) -> None:
    if not isinstance(verdict, dict):
        raise TypeError("verdict must be a dict")
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "verdict": verdict,
    }
    descriptor, tmp_path = tempfile.mkstemp(prefix=".verdict-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class _LockedRecord:
    """A cache record handle scoped to one held ``flock``."""

    def __init__(self, path: str) -> None:
        self._path = path

    def get(self, fingerprint: object) -> dict | None:
        return _read_record(self._path, fingerprint)

    def put(self, fingerprint: object, verdict: dict) -> None:
        _write_record(self._path, fingerprint, verdict)


class VerdictCache:
    """Persist agentic judge verdicts under ``<base_dir>/<state_hash>/<task_id>.json``."""

    def __init__(self, base_dir: str) -> None:
        self._base_dir = os.fspath(base_dir)

    def _record_path(self, state_hash: str, task_id: str) -> str:
        state_hash = _validate_key(state_hash, "state_hash")
        task_id = _validate_key(task_id, "task_id")
        return os.path.join(self._base_dir, state_hash, f"{task_id}.json")

    def get(self, state_hash: str, task_id: str, fingerprint: object) -> dict | None:
        """Return the cached verdict, or ``None`` on any miss.

        Missing files, malformed JSON, a non-dict payload, an unexpected
        top-level key set, a schema-version mismatch, and a fingerprint
        mismatch are all treated as ordinary misses.
        """
        return _read_record(self._record_path(state_hash, task_id), fingerprint)

    def put(self, state_hash: str, task_id: str, fingerprint: object, verdict: dict) -> None:
        """Atomically persist ``verdict`` under ``(state_hash, task_id)``.

        Raises ``TypeError`` if ``verdict`` is not a dict; never partially
        writes a record (temp file + fsync + ``os.replace``).
        """
        _write_record(self._record_path(state_hash, task_id), fingerprint, verdict)

    @contextmanager
    def locked_record(self, state_hash: str, task_id: str) -> Iterator[_LockedRecord]:
        """Hold one per-key ``fcntl.flock`` across lookup, model work, and write.

        Callers should recheck the cache (``record.get(...)``) immediately
        after entering this context, since another process may have written
        the record between an earlier unlocked check and lock acquisition.
        """
        path = self._record_path(state_hash, task_id)
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        lock_path = f"{path}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                yield _LockedRecord(path)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


__all__ = ["VerdictCache"]
