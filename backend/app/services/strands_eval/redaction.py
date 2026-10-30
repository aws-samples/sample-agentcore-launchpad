"""Credential masking for span / log-record attributes (N17-obs307).

ADOT's botocore instrumentation records ``aws.auth.account.access_key`` (the caller's STS
access-key id) on every AWS SDK span a runtime emits. Every read path that returns span
attributes — the trace detail API, the evaluation spans provider (judge prompts), log
snippets — masks them through this one helper: a credential-like key keeps its name and
reads ``[redacted]``; an access-key id anywhere in a string value is masked in place.

Stdlib only: shipped into the evaluation worker zip with the rest of this package.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
# Small and explicit on purpose: masking a key an operator needs is a visible loss, a
# missed credential is a leak — each entry names a credential, not a "might be" field.
SENSITIVE_KEY_RE = re.compile(
    r"access[_.-]?key|secret|session[_.-]?token|authorization|x-amz-security-token|password",
    re.IGNORECASE,
)
ACCESS_KEY_ID_RE = re.compile(r"A(?:S|K)IA[A-Z0-9]{16}")


def is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and SENSITIVE_KEY_RE.search(key) is not None


def redact_text(text: str) -> str:
    """``text`` with every AWS access-key id replaced by ``[redacted]``."""
    return ACCESS_KEY_ID_RE.sub(REDACTED, text)


def redact_value(value: Any) -> Any:
    """A copy of ``value`` with credential-like keys masked at every nesting level and
    access-key ids masked inside strings; other scalars are returned as they are."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: REDACTED if is_sensitive_key(k) else redact_value(v) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_value(v) for v in value]
    return value


def redact_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Span (or log-record) ``attributes`` safe to show: same keys, credentials masked."""
    return redact_value(attributes)
