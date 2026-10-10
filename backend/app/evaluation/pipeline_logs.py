"""CloudWatch log records → conversation turns (V2 数据处理 · 运行日志 source).

A pipeline whose source is ``type="logs"`` reads its log groups with one Logs
Insights query and converts each record with an embedded format rule into
``{role, text}`` turns grouped by session — the transcript shape
`pipelines.items_from_transcript` already consumes, so extraction, dedupe and the
dataset write are the trace path unchanged. Nothing here calls a model.

Presets:

* ``genai`` — OTel GenAI content records (``otel-rt-logs`` and the like), read by
  `observability.content_records_turns`;
* ``message`` — one record is one message (session / role / text fields);
* ``exchange`` — one record is one question and its answer (input / output).

Fields are JSON dot paths only (``payload.messages.0.text``); a record with a text
prefix before its JSON (``INFO 2026-… {...}``) is decoded from its first ``{``.
User regexes are deliberately not supported (backtracking on the server).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app.services import observability

MAX_EVENTS = 10_000  # the Logs Insights result cap
MAX_SAMPLES = 5
# field suggestions come from more records than the samples shown: a group that
# mixes formats (several services, or app + framework logs) would otherwise only
# suggest the newest format's fields (measured in the dev e2e)
MAX_PATH_RECORDS = 50
MAX_LEAVES_PER_RECORD = 200
MAX_PREVIEW_SESSIONS = 8
MAX_PATHS = 60
SAMPLE_CHARS = 2000
MAX_SESSION_KEY = 256
STREAM_FIELD = "@logStream"
GENAI_SESSION_FIELD = "attributes.session.id"

FIELD_PATH_RE = r"^(@logStream|[A-Za-z0-9_$\-]+(\.[A-Za-z0-9_$\-]+)*)$"
FieldPath = Field(default=None, max_length=128, pattern=FIELD_PATH_RE)
RoleName = Field(min_length=1, max_length=32)


class LogFormat(BaseModel):
    """How a record becomes turns. Unused fields of the other presets are kept
    as sent (the editor switches presets without losing what was typed)."""

    preset: Literal["genai", "message", "exchange"] = "genai"
    session_field: str | None = FieldPath
    role_field: str | None = FieldPath
    text_field: str | None = FieldPath
    user_roles: list[str] = Field(default_factory=lambda: ["user", "human"], max_length=10)
    assistant_roles: list[str] = Field(
        default_factory=lambda: ["assistant", "ai", "bot"], max_length=10
    )
    input_field: str | None = FieldPath
    output_field: str | None = FieldPath

    @model_validator(mode="after")
    def _preset_fields(self) -> LogFormat:
        required = {
            "genai": [],
            "message": ["session_field", "role_field", "text_field"],
            "exchange": ["input_field"],
        }[self.preset]
        missing = [name for name in required if not getattr(self, name)]
        if missing:
            raise ValueError(f"preset '{self.preset}' needs {', '.join(missing)}")
        if self.preset == "message" and not (self.user_roles and self.assistant_roles):
            raise ValueError("preset 'message' needs user_roles and assistant_roles")
        for roles in (self.user_roles, self.assistant_roles):
            if any(not r.strip() or len(r) > 32 for r in roles):
                raise ValueError("role names must be 1-32 characters")
        return self


# Logs Insights flattens a pure-JSON record's fields, so GenAI content records can
# be narrowed server-side (a runtime group is mostly stdout lines — measured 6 of
# 962 records on a live A2A runtime). The custom presets cannot: a record with a
# text prefix before its JSON is not flattened, and would be dropped.
GENAI_FILTER = (
    "filter ispresent(body.input.messages.0.role) or ispresent(body.output.messages.0.role)"
)


def events_query(keyword: str | None, preset: str = "message") -> str:
    """The newest records of the input log groups, optionally only those that
    contain ``keyword`` (case-insensitive, matched literally)."""
    lines = ["fields @timestamp, @message, @logStream, @log"]
    if preset == "genai":
        lines.append(GENAI_FILTER)
    needle = (keyword or "").strip()
    if needle:
        pattern = re.escape(needle).replace("/", "\\/")
        lines.append(f"filter @message like /(?i){pattern}/")
    lines += ["sort @timestamp desc", f"limit {MAX_EVENTS}"]
    return "\n| ".join(lines)


def fetch_events(
    logs: Any, log_groups: list[str], hours: int, keyword: str | None, preset: str
) -> tuple[list[dict[str, str]], bool]:
    """(rows newest first, whether the result cap may have hidden older rows)."""
    rows = observability.run_insights_queries(
        {"events": events_query(keyword, preset)}, hours, logs=logs, log_groups=log_groups,
    )["events"]
    return rows, len(rows) >= MAX_EVENTS


def parse_record(message: str | None) -> dict[str, Any] | None:
    """The record's JSON object, tolerating a text prefix before it."""
    text = (message or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        start = text.find("{")
        if start <= 0:
            return None
        try:
            value, _end = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def get_path(record: Any, path: str) -> Any:
    """``a.b.0.c`` into nested dicts/lists; None when any step is missing.

    A dotted key stored flat (OTel's ``"session.id"``) is matched before the
    path is split further, so ``attributes.session.id`` reads both shapes."""
    parts = path.split(".")
    node = record
    i = 0
    while i < len(parts):
        if isinstance(node, dict):
            for j in range(len(parts), i, -1):  # longest flat key first
                key = ".".join(parts[i:j])
                if key in node:
                    node, i = node[key], j
                    break
            else:
                return None
        elif isinstance(node, list) and parts[i].isdigit() and int(parts[i]) < len(node):
            node, i = node[int(parts[i])], i + 1
        else:
            return None
    return node


def text_of(value: Any) -> str | None:
    """Readable text of a field: a string, a number, a list of strings or
    ``{text}`` parts, or a ``{text}`` / ``{content}`` wrapper."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list):
        texts = [text_of(v) for v in value]
        joined = "\n".join(t for t in texts if t)
        return joined or None
    if isinstance(value, dict):
        for key in ("text", "content", "message"):
            if key in value:
                return text_of(value[key])
    return None


def _scalar_key(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str | int | float):
        key = str(value).strip()
        return key[:MAX_SESSION_KEY] if key else None
    return None


def record_paths(records: list[dict[str, Any]]) -> list[str]:
    """Dotted paths of the scalar / text leaves of sample records (list items by
    their first index) — the field suggestions of the format editor. Ranked by
    how many records carry them (ties: first seen), so every format present in
    the sample is represented, the most common first."""
    counts: Counter[str] = Counter()

    for record in records:
        seen: dict[str, None] = {}

        def walk(node: Any, prefix: str, depth: int, seen: dict[str, None] = seen) -> None:
            if len(seen) >= MAX_LEAVES_PER_RECORD or depth > 5:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    if re.fullmatch(r"[A-Za-z0-9_$\-.]+", str(key)):
                        walk(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
            elif isinstance(node, list):
                if node:
                    walk(node[0], f"{prefix}.0" if prefix else "0", depth + 1)
            elif prefix:
                seen.setdefault(prefix, None)

        walk(record, "", 0)
        counts.update(seen.keys())
    return [path for path, _n in counts.most_common(MAX_PATHS)]


def _role(value: Any, fmt: LogFormat) -> str | None:
    name = (text_of(value) or "").lower()
    if name in {r.strip().lower() for r in fmt.user_roles}:
        return "user"
    if name in {r.strip().lower() for r in fmt.assistant_roles}:
        return "assistant"
    return None


def _orphan_key(row: dict[str, str], prompt: str) -> str:
    """A stable session key for an exchange record that names no session — the
    same record yields the same key on every run, so dedupe holds."""
    raw = f"{row.get('@logStream')}|{row.get('@timestamp')}|{prompt}"
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:16]


def _session_key(row: dict[str, str], record: dict[str, Any], path: str) -> str | None:
    if path == STREAM_FIELD:
        return _scalar_key(row.get("@logStream"))
    return _scalar_key(get_path(record, path))


def convert(
    rows: list[dict[str, str]],
    fmt: LogFormat,
    *,
    max_sessions: int,
    visible: Any = None,
) -> dict[str, Any]:
    """Rows (newest first) → the newest ``max_sessions`` sessions' turns.

    Returns ``{events, parsed, failed{reason: n}, sessions_found, sessions[
    {session_id, stream, log_group, last, records, turns}], paths}``; each
    session's turns are oldest first."""
    failed: Counter[str] = Counter()
    parsed = 0
    sessions: dict[str, dict[str, Any]] = {}
    found: set[str] = set()
    sampled: list[dict[str, Any]] = []
    for row in rows:
        record = parse_record(row.get("@message"))
        if record is None:
            failed["not_json"] += 1
            continue
        if len(sampled) < MAX_PATH_RECORDS:
            sampled.append(record)
        entries: list[dict[str, Any]]
        if fmt.preset == "genai":
            body = record.get("body")
            if not isinstance(body, dict) or not ({"input", "output"} & body.keys()):
                failed["not_genai"] += 1
                continue
            key = _session_key(row, record, fmt.session_field or GENAI_SESSION_FIELD)
            entries = [record]
        elif fmt.preset == "message":
            key = _session_key(row, record, fmt.session_field or "")
            role = _role(get_path(record, fmt.role_field or ""), fmt)
            text = text_of(get_path(record, fmt.text_field or ""))
            if key and role is None:
                failed["other_role"] += 1
                continue
            if key and text is None:
                failed["no_text"] += 1
                continue
            entries = [{"role": role, "text": text}]
        else:
            prompt = text_of(get_path(record, fmt.input_field or ""))
            if prompt is None:
                failed["no_text"] += 1
                continue
            reply = text_of(get_path(record, fmt.output_field)) if fmt.output_field else None
            key = (
                _session_key(row, record, fmt.session_field)
                if fmt.session_field else _orphan_key(row, prompt)
            )
            entries = [{"role": "user", "text": prompt}]
            if reply:
                entries.append({"role": "assistant", "text": reply})
        if not key:
            failed["no_session"] += 1
            continue
        if visible is not None and not visible(key):
            continue  # another principal's private session: never read
        parsed += 1
        found.add(key)
        session = sessions.get(key)
        if session is None:
            if len(sessions) >= max_sessions:
                continue  # older than the newest max_sessions sessions
            session = sessions[key] = {
                "session_id": key,
                "stream": row.get("@logStream") or "",
                "log_group": (row.get("@log") or "").split(":", 1)[-1],
                "last": row.get("@timestamp"),
                "chunks": [],
            }
        session["chunks"].append(entries)

    out = []
    for session in sessions.values():
        chunks = session.pop("chunks")[::-1]  # newest first → oldest first
        if fmt.preset == "genai":
            turns = observability.content_records_turns([e for c in chunks for e in c])
        else:
            turns = [entry for chunk in chunks for entry in chunk]
        out.append({**session, "records": len(chunks), "turns": turns})
    return {
        "events": len(rows),
        "parsed": parsed,
        "failed": dict(failed),
        "sessions_found": len(found),
        "sessions": out,
        "paths": record_paths(sampled),
    }


def samples(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The newest raw records, clipped — the preview's left-hand side."""
    return [
        {
            "stream": row.get("@logStream") or "",
            "timestamp": row.get("@timestamp"),
            "message": (row.get("@message") or "")[:SAMPLE_CHARS],
        }
        for row in rows[:MAX_SAMPLES]
    ]
