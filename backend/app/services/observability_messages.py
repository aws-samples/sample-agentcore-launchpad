"""Normalize same-span OTel content logs and MCP attributes for the trace drawer.

Provider ``gen_ai.*.message`` events are request history; only ``gen_ai.choice``
is the current completion. Strands/Harness/native SDK envelopes already name
their input/output sides. Neither format inherits content from another span.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

MESSAGE_TEXT_CAP = 2000
MESSAGES_PER_SIDE = 20
BLOCKS_PER_MESSAGE = 20
MESSAGE_SPANS_CAP = 50
GEN_AI_MESSAGE_ROLES = {
    "gen_ai.system.message": "system",
    "gen_ai.user.message": "user",
    "gen_ai.assistant.message": "assistant",
    "gen_ai.tool.message": "tool",
}
GEN_AI_MESSAGE_EVENTS = (*GEN_AI_MESSAGE_ROLES, "gen_ai.choice")


def _decode(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def _text(value: Any, cap: int = MESSAGE_TEXT_CAP) -> str:
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))[:cap]


def _label(value: Any) -> str | None:
    return value[:MESSAGE_TEXT_CAP] if isinstance(value, str) else None


def _tool_block(kind: str, value: dict[str, Any]) -> dict[str, Any]:
    if kind == "tool_use":
        payload = _decode(value.get("input"))
        block = {
            "type": kind, "name": _label(value.get("name")),
            "input": _text(payload, 500),
        }
    else:
        payload = _decode(value.get("content"))
        status = _label(value.get("status"))
        if status is None and isinstance(value.get("isError"), bool):
            status = "error" if value["isError"] else "success"
        block = {"type": kind, "status": status, "text": _text(payload, 500)}
    tool_id = value.get("toolUseId")
    if isinstance(tool_id, str) and tool_id:
        # Use the full payload, before display truncation. Same text with a
        # different tool id (or different text past the cap) is a distinct call.
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        block["_tool_key"] = (
            kind, tool_id, block.get("name"), hashlib.sha256(canonical.encode()).hexdigest(),
        )
    return block


def _parse_content_blocks(raw: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Accept serialized/decoded blocks and preserve unknown content readably."""
    if raw is None:
        return []
    raw = _decode(raw)
    blocks = raw if isinstance(raw, list) else [raw]
    out: list[dict[str, Any]] = []
    for block in blocks[:BLOCKS_PER_MESSAGE]:
        if not isinstance(block, dict):
            out.append({"type": "text", "text": _text(block)})
        elif "text" in block:
            out.append({"type": "text", "text": _text(block["text"])})
        elif isinstance(block.get("toolUse"), dict):
            out.append(_tool_block("tool_use", block["toolUse"]))
        elif isinstance(block.get("toolResult"), dict):
            out.append(_tool_block("tool_result", block["toolResult"]))
        elif block.get("type") == "tool_use":
            out.append(_tool_block("tool_use", {**block, "toolUseId": block.get("id")}))
        elif block.get("type") == "tool_result":
            out.append(_tool_block("tool_result", {
                **block, "toolUseId": block.get("tool_use_id"),
                "isError": block.get("is_error"),
            }))
        elif depth < 4 and any(k in block for k in ("content", "message", "tool.result")):
            # Strands may put the tool result beside the assistant message,
            # and repeat it as a separate message. Dedup happens across both.
            for key in ("content", "message", "tool.result"):
                out.extend(_parse_content_blocks(block.get(key), depth + 1))
        else:
            out.append({"type": "other", "text": _text(block, 300)})
    return out[:BLOCKS_PER_MESSAGE]


def _append_messages(target: list[dict[str, Any]], messages: list[dict[str, Any]]) -> None:
    seen = {
        block["_tool_key"]: block
        for message in target for block in message["blocks"] if "_tool_key" in block
    }
    for message in messages:
        blocks = []
        for block in message["blocks"]:
            key = block.get("_tool_key")
            if key and key in seen:
                # A native tool.message may precede the richer toolResult.
                if block.get("status") and not seen[key].get("status"):
                    seen[key]["status"] = block["status"]
                continue
            if len(target) >= MESSAGES_PER_SIDE or len(blocks) >= BLOCKS_PER_MESSAGE:
                continue
            blocks.append(block)
            if key:
                seen[key] = block
        if len(target) < MESSAGES_PER_SIDE and (blocks or message.get("finish_reason")):
            target.append({**message, "blocks": blocks})


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    messages = _decode(messages)
    if not isinstance(messages, list):
        return []
    out: list[dict[str, Any]] = []
    for message in messages[:MESSAGES_PER_SIDE]:
        message = _decode(message)
        if not isinstance(message, dict):
            continue
        role = _label(message.get("role"))
        content = _decode(message.get("content"))
        finish = _label(message.get("finish_reason"))
        if isinstance(content, dict):
            finish = finish or _label(content.get("finish_reason"))
        tool_id = message.get("tool_call_id") or message.get("id")
        if role == "tool" and isinstance(tool_id, str) and content is not None:
            blocks = [_tool_block("tool_result", {
                **message, "content": content, "toolUseId": tool_id,
            })]
        else:
            blocks = _parse_content_blocks(content)
        calls = _decode(message.get("tool_calls"))
        if isinstance(calls, list):
            for call in calls[:BLOCKS_PER_MESSAGE]:
                if isinstance(call, dict) and isinstance(call.get("function"), dict):
                    function = call["function"]
                    blocks.append(_tool_block("tool_use", {
                        "toolUseId": call.get("id"), "name": function.get("name"),
                        "input": function.get("arguments"),
                    }))
                else:
                    blocks.append({"type": "other", "text": _text(call, 300)})
        entry = {"role": role, "blocks": blocks}
        if finish:
            entry["finish_reason"] = finish
        _append_messages(out, [entry])
    return out


def _event_messages(record: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    body = _decode(record.get("body"))
    if not isinstance(body, dict):
        return {}, False
    sides = {}
    for side in ("input", "output"):
        payload = _decode(body.get(side))
        if isinstance(payload, dict):
            messages = _normalize_messages(payload.get("messages"))
            if messages:
                sides[side] = messages
    if sides:
        return sides, True
    attrs = record.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    event = record.get("eventName") or attrs.get("event.name")
    if event == "gen_ai.choice":
        message = _decode(body.get("message"))
        if not isinstance(message, dict):
            message = {"content": message}
        messages = _normalize_messages([{
            **message, "role": message.get("role") or "assistant",
            "finish_reason": body.get("finish_reason") or message.get("finish_reason"),
        }])
        return {"output": messages} if messages else {}, False
    if isinstance(event, str) and event in GEN_AI_MESSAGE_ROLES:
        messages = _normalize_messages([{**body, "role": GEN_AI_MESSAGE_ROLES[event]}])
        return {"input": messages} if messages else {}, False
    return {}, False


def _record_session_ids(record: dict[str, Any]) -> list[str]:
    resource = record.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    candidates = [resource.get("session.id")]
    for attrs in (record.get("attributes"), resource.get("attributes")):
        if isinstance(attrs, dict):
            candidates.append(attrs.get("session.id"))
    return [sid for sid in candidates if isinstance(sid, str) and sid]


def message_event_session_ids(rows: list[dict[str, str]]) -> set[str]:
    """Privacy provenance is independent of display caps and content validity."""
    session_ids = set()
    for row in rows:
        record = _decode(row.get("@message"))
        if not isinstance(record, dict):
            continue
        attrs = record.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else {}
        event = record.get("eventName") or attrs.get("event.name")
        is_content = isinstance(event, str) and event in GEN_AI_MESSAGE_EVENTS
        body = _decode(record.get("body"))
        if isinstance(body, dict):
            for side in ("input", "output"):
                payload = _decode(body.get(side))
                if isinstance(payload, dict) and "messages" in payload:
                    is_content = True
        if is_content:
            session_ids.update(_record_session_ids(record))
    return session_ids


def _event_time(record: dict[str, Any], row: dict[str, str]) -> int:
    for key in ("timeUnixNano", "observedTimeUnixNano"):
        try:
            value = int(record.get(key) or 0)
            if value > 0:
                return value
        except (TypeError, ValueError, OverflowError):
            continue
    try:
        timestamp = row.get("@timestamp")
        value = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1_000_000_000)
    except (TypeError, ValueError, OverflowError):
        return 0  # timestamp-less records retain their original order


def _public_messages(entry: dict[str, Any]) -> dict[str, Any]:
    for side in ("input", "output"):
        for message in entry.get(side, []):
            for block in message["blocks"]:
                block.pop("_tool_key", None)
    return entry


def parse_message_events(rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
    """Decode bounded messages, keeping every contributing event's session id."""
    records = []
    for row in rows:
        record = _decode(row.get("@message"))
        if isinstance(record, dict):
            records.append((_event_time(record, row), record))
    records.sort(key=lambda item: item[0])  # stable for equal or missing timestamps
    by_span: dict[str, dict[str, Any]] = {}
    envelope_sides: dict[str, set[str]] = {}
    for _, record in records:
        span_id = record.get("spanId")
        if not isinstance(span_id, str) or not span_id:
            continue
        try:
            sides, envelope = _event_messages(record)
        except (TypeError, ValueError, RecursionError):
            continue  # malformed content must not hide the other spans
        if not sides:
            continue  # ordinary correlated logs do not consume the span budget
        if span_id not in by_span and len(by_span) >= MESSAGE_SPANS_CAP:
            continue  # still collect later outputs/provenance for admitted spans
        entry = by_span.setdefault(span_id, {})
        sids = entry.setdefault("session_ids", [])
        for sid in _record_session_ids(record):
            if sid not in sids:
                sids.append(sid)
        if sids:
            entry["session_id"] = sids[0]
        full_sides = envelope_sides.setdefault(span_id, set())
        for side, messages in sides.items():
            if side in full_sides:
                continue
            if envelope:
                # Full envelopes are authoritative over individual events on
                # the same side. Do not duplicate their text/history.
                entry[side] = []
                full_sides.add(side)
            _append_messages(entry.setdefault(side, []), messages)
    return {key: _public_messages(value) for key, value in by_span.items()}


def span_attribute_messages(attrs: dict[str, Any]) -> dict[str, Any]:
    """MCP tools record their arguments/results directly on their own span."""
    entry = {}
    for side, attr, kind in (
        ("input", "gen_ai.tool.call.arguments", "tool_use"),
        ("output", "gen_ai.tool.call.result", "tool_result"),
    ):
        if attr not in attrs or attrs[attr] is None:
            continue
        value = {
            "toolUseId": attrs.get("gen_ai.tool.call.id"),
            "name": attrs.get("gen_ai.tool.name") or attrs.get("tool.name"),
            "input" if side == "input" else "content": attrs[attr],
        }
        entry[side] = [{
            "role": "assistant" if side == "input" else "tool",
            "blocks": [_tool_block(kind, value)],
        }]
    return _public_messages(entry)
