"""The record cache and the Logs client that serves it to the production provider.

One JSON file per session under ``backend/.eval-cache/sessions/`` (gitignored), in the same
shape as the committed real-record fixtures (``tests/fixtures/obs315_*.json``)::

    {"session_id", "log_group", "source", "fetched_at"?, "window"?,
     "records": [{"timestamp": <ms>, "group": "runtime" | "aws/spans", "record": {…}}]}

so a fixture IS a cache entry. The records are kept raw (the provider redacts them itself
after its query, N17-obs307) — the directory is created ``0700`` and never committed.

:class:`CachedLogs` answers the three calls the evaluation code makes on a CloudWatch Logs
client — ``start_query`` / ``get_query_results`` (``LaunchpadSpansProvider``'s content and
hierarchy queries) and ``filter_log_events`` (the console's ``eval_turns_from_content_logs``)
— from those records, the way CloudWatch would: content rows from the requested groups in
``@timestamp`` order, hierarchy rows from ``aws/spans`` only, filter-pattern matches from the
named runtime group.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RUNTIME = "runtime"
SPANS = "aws/spans"
_SESSION_IN_QUERY = re.compile(r"attributes\.session\.id\s+(?:like|=)\s+'([^']+)'")


@dataclass
class SessionRecords:
    """One session's raw records from its runtime group and ``aws/spans``."""

    session_id: str
    log_group: str
    records: list[dict[str, Any]]
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> SessionRecords:
        known = {"session_id", "log_group", "records", "source"}
        return cls(
            session_id=str(data["session_id"]), log_group=str(data.get("log_group") or ""),
            records=list(data.get("records") or []), source=str(data.get("source") or ""),
            meta={k: v for k, v in data.items() if k not in known},
        )

    def to_json(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "log_group": self.log_group,
                "source": self.source, **self.meta, "records": self.records}

    def group(self, name: str) -> list[dict[str, Any]]:
        """The raw records of one group (``RUNTIME`` or ``SPANS``), in timestamp order."""
        return [x["record"] for x in sorted(self.records, key=lambda x: x["timestamp"])
                if x["group"] == name]

    @property
    def counts(self) -> dict[str, int]:
        return {RUNTIME: len(self.group(RUNTIME)), SPANS: len(self.group(SPANS))}


def _file_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)
    if safe != session_id:  # keep two ids that sanitise alike apart
        safe += "-" + hashlib.sha256(session_id.encode()).hexdigest()[:8]
    return f"{safe}.json"


class RecordCache:
    """``<root>/sessions/<session>.json`` read/write, plus run and Global response files."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, name: str) -> Path:
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        return path

    def session_path(self, session_id: str) -> Path:
        return self.root / "sessions" / _file_name(session_id)

    def load(self, session_id: str) -> SessionRecords | None:
        path = self.session_path(session_id)
        if not path.is_file():
            return None
        return load_file(path)

    def save(self, entry: SessionRecords) -> Path:
        path = self._dir("sessions") / _file_name(entry.session_id)
        _write_json(path, entry.to_json())
        return path

    def load_json(self, kind: str, key: str) -> Any:
        path = self.root / kind / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def save_json(self, kind: str, key: str, data: Any) -> Path:
        path = self._dir(kind) / f"{key}.json"
        _write_json(path, data)
        return path


def _write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_file(path: Path) -> SessionRecords:
    """A cache entry or a committed fixture."""
    return SessionRecords.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


class CachedLogs:
    """A CloudWatch Logs client over cached sessions (no network).

    ``sessions`` maps session id → :class:`SessionRecords`; ``trace`` narrows every answer
    to one trace (the per-turn console oracle in :mod:`.reconcile`). Unknown sessions
    answer empty, like CloudWatch.
    """

    def __init__(self, sessions: dict[str, SessionRecords], *, trace: str | None = None) -> None:
        self._sessions = sessions
        self._trace = trace
        self._queries: dict[str, tuple[list[str], str]] = {}

    def _entries(self, session_id: str | None) -> list[dict[str, Any]]:
        entry = self._sessions.get(session_id or "")
        if entry is None:
            return []
        rows = sorted(entry.records, key=lambda x: x["timestamp"])
        if self._trace:
            rows = [x for x in rows if x["record"].get("traceId") == self._trace]
        return rows

    def start_query(self, *, logGroupNames: list[str], startTime: int, endTime: int,  # noqa: N803
                    queryString: str, **_: Any) -> dict[str, str]:
        query_id = str(len(self._queries))
        self._queries[query_id] = (list(logGroupNames), queryString)
        return {"queryId": query_id}

    def get_query_results(self, *, queryId: str) -> dict[str, Any]:  # noqa: N803
        groups, query = self._queries[queryId]
        found = _SESSION_IN_QUERY.search(query)
        entries = self._entries(found.group(1) if found else None)
        if query.lstrip().startswith("fields spanId"):
            rows = [[{"field": key, "value": x["record"][key]}
                     for key in ("spanId", "parentSpanId", "traceId") if x["record"].get(key)]
                    for x in entries if x["group"] == SPANS and SPANS in groups]
        else:
            runtime_requested = any(g != SPANS for g in groups)
            rows = [[{"field": "@message", "value": json.dumps(x["record"], ensure_ascii=False)}]
                    for x in entries
                    if (x["group"] == SPANS and SPANS in groups)
                    or (x["group"] == RUNTIME and runtime_requested)]
        return {"status": "Complete", "results": rows}

    def filter_log_events(self, *, logGroupName: str, filterPattern: str = "",  # noqa: N803
                          **_: Any) -> dict[str, Any]:
        term = filterPattern.strip().strip('"')
        events = []
        for entry in self._sessions.values():
            if entry.log_group and entry.log_group != logGroupName:
                continue
            for x in self._entries(entry.session_id):
                if x["group"] != RUNTIME:
                    continue
                message = json.dumps(x["record"], ensure_ascii=False)
                if term in message:
                    events.append({"timestamp": x["timestamp"], "message": message})
        return {"events": sorted(events, key=lambda e: e["timestamp"])}
