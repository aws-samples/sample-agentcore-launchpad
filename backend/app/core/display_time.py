"""Every time the API returns is in the display zone (N17-obs463, user ruling 2026-10-26).

Storage stays UTC: the SQLite ledger writes naive UTC datetimes, AWS hands back aware ones,
and worker / JSON artefacts carry ISO strings with ``+00:00``. The API serialises all of them
as ISO 8601 in ``Settings.display_timezone`` (default ``Asia/Shanghai``), e.g.
``"2026-10-24T18:29:54.079198+08:00"``: the same instant with the display offset.

ONE place does it: :func:`to_display_payload` walks a response body and converts only OUR
times (lead review of N17-obs463 — a value-based walk rewrote user content: a scenario's
"2026-10-24 10:00", a tool argument, proposal text):

- a ``datetime`` object, always (naive = UTC, the ledger's);
- a string under a TIME KEY (:func:`is_time_key`: ``*_at``, camelCase ``*At``, and the
  enumerated :data:`TIME_KEYS`) that is exactly an ISO 8601 date-time;
- never anything inside a CONTENT subtree (:data:`CONTENT_KEYS`: messages' text, span
  attributes and bodies, tool arguments / results, proposal content, the agent spec, memory /
  KB metadata, evaluator configs, raw artefacts) or a content-shaped dict
  (:data:`CONTENT_SHAPE_KEYS`: a dataset item / scenario), whatever its keys;
- in a stream, the event envelope only: ``delta`` / ``text`` events pass untouched.

:class:`DisplayTimeJSONResponse` is the app's ``default_response_class`` and every explicit
JSON response, and ``chat.sse_encode`` runs each stream event through the same walk.

Inbound times (query parameters, request bodies) may now carry ``+08:00``: they are compared
or stored through :func:`to_utc_naive` / ``users.as_utc``, which convert, never relabel.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.responses import JSONResponse

DEFAULT_DISPLAY_TIMEZONE = "Asia/Shanghai"

# exactly a date-time: date, `T` or one space, hh:mm[:ss[.fraction]], optional zone
_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?",
    re.IGNORECASE)
_FRACTION = re.compile(r"\.(\d+)")


@lru_cache(maxsize=8)
def _zone(name: str) -> tzinfo:
    return ZoneInfo(name)


def display_zone() -> tzinfo:
    from app.core.config import get_settings  # noqa: PLC0415 — config imports nothing from here

    return _zone(get_settings().display_timezone)


def to_display(value: datetime, zone: tzinfo | None = None) -> datetime:
    """The same instant in the display zone; a naive value is UTC (the ledger's)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(zone or display_zone())


def to_utc_naive(value: datetime) -> datetime:
    """An inbound time as the ledger stores it: naive UTC (an aware value is converted)."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo is not None else value


def _parse(text: str) -> datetime | None:
    if not _ISO_DATETIME.fullmatch(text):
        return None
    value = text.replace(" ", "T", 1)
    if value[-1] in "zZ":
        value = value[:-1] + "+00:00"
    # fromisoformat takes at most 6 fraction digits — AWS sometimes sends nanoseconds
    value = _FRACTION.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), value, count=1)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def display_iso(value: datetime | str, zone: tzinfo | None = None) -> str:
    """ISO 8601 in the display zone (a string that is not a date-time comes back as is)."""
    if isinstance(value, str):
        parsed = _parse(value)
        if parsed is None:
            return value
        value = parsed
    return to_display(value, zone).isoformat()


# Keys whose string value is one of our timestamps, besides `*_at` / `*At` (enumerated from
# every API response the hermetic suite produces; `test_display_time` pins the list).
TIME_KEYS = frozenset({
    "at", "ts", "time", "timestamp", "start", "end", "since", "until", "first", "last",
    "first_event", "last_event", "last_seen", "locked_until", "bucket", "bin",
})
# Subtrees that carry CONTENT — user, agent, model or AWS-record data — never rewritten, even
# under a time key. A value maps to the child keys that are still OURS inside it.
CONTENT_KEYS: dict[str, frozenset[str]] = {
    key: frozenset() for key in (
        "content", "text", "delta", "body", "attributes", "arguments", "args", "input",
        "inputs", "output", "outputs", "tool_input", "tool_output", "tool_result", "tool_args",
        "result", "parameters", "params", "spec", "env", "environment", "metadata",
        "definition", "prompt", "system_prompt", "expected_response", "expected_trajectory",
        "assertions", "scenarios", "examples", "cases", "raw", "document", "fields",
        "values", "value", "request", "response", "published_content", "draft",
    )
} | {
    # evaluator configs are free-form; the scorecard's config diff (`between`) is ours
    "config": frozenset({"between"}),
}
# A dict with one of these keys IS content wherever it sits: a dataset item / scenario (free-form
# keys a member typed — `scenario_id`, a persona's `actor_profile`, a prompt item's `prompt`).
CONTENT_SHAPE_KEYS = frozenset({"scenario_id", "actor_profile", "prompt"})
_STREAM_CONTENT_EVENTS = frozenset({"delta", "text", "content", "reasoning"})


def is_time_key(key: str) -> bool:
    """A key whose ISO date-time string is one of our timestamps."""
    return key.endswith("_at") or (len(key) > 2 and key.endswith("At") and key[-3].islower()) \
        or key in TIME_KEYS


def to_display_payload(value: Any, zone: tzinfo | None = None, *,
                       _key: str | None = None) -> Any:
    """A response body with OUR times in the display zone (see the module docstring)."""
    zone = zone or display_zone()
    if isinstance(value, datetime):
        return to_display(value, zone).isoformat()
    if isinstance(value, str):
        if _key is None or not is_time_key(_key):
            return value
        return display_iso(value, zone)
    if isinstance(value, dict):
        if CONTENT_SHAPE_KEYS & value.keys():
            return value
        return {k: _walk_child(k, v, zone) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_display_payload(v, zone, _key=_key) for v in value]
    return value


def _walk_child(key: Any, value: Any, zone: tzinfo) -> Any:
    if not isinstance(key, str):
        return to_display_payload(value, zone)
    ours = CONTENT_KEYS.get(key)
    if ours is None:
        return to_display_payload(value, zone, _key=key)
    if ours and isinstance(value, dict):  # a content subtree with named children of ours
        return {k: (to_display_payload(v, zone, _key=k) if k in ours else v)
                for k, v in value.items()}
    return value


def stream_event_payload(event: str, data: Any) -> Any:
    """An SSE event's data: the envelope's own times converted, model text never touched."""
    if event in _STREAM_CONTENT_EVENTS:
        return data
    return to_display_payload(data)


class DisplayTimeJSONResponse(JSONResponse):
    """The app's JSON response: the body's times in the display zone (N17-obs463)."""

    def render(self, content: Any) -> bytes:
        return super().render(to_display_payload(content))
