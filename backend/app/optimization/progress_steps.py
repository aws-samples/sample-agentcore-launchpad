"""Coded progress lines of a Runtime canary action (N17-obs447).

A canary action reports progress through a plain ``Callable[[str], None]`` (``run_action``
persists it on the row, the console shows it while the action runs). The lines were English
sentences shown verbatim on the zh console. :func:`step` builds a :class:`Step`: still the
same ``str`` (every ``progress`` / ``log`` callable and the stored ``progress`` text are
unchanged), plus a stable ``code`` and its ``params`` that ``run_action`` stores additively
(``progress_detail``) and the console words in either language. Same pattern as the verdict
reasons (``verdict.reason_of``, N17-obs443).

A line a shared deployer helper writes through the canary's ``log`` (skill bundling, the
deploy-time narrowing) carries no code; the console falls back to its text.
"""

from __future__ import annotations

from typing import Any

# every code a canary-owned call site emits — the console's `CANARY_PROGRESS_CODES` mirrors it
STEP_CODES = frozenset({
    # canary_service
    "creating_online_config", "stopping_ab_test", "reading_current_version",
    "minting_candidate", "opening_ab_test", "replaying_sessions", "aggregating_evidence",
    "shifting_traffic", "recording_promotion", "rolling_forward", "deleting_artifact",
    "tearing_down", "waiting_endpoints_gone",
    # canary_infra
    "current_version", "candidate_artifact", "candidate_published",
    "candidate_runtime_status", "pinning_endpoint", "endpoint_status",
    "promoting_stable_endpoint", "stable_endpoint_status", "runtime_gone",
    "endpoint_listing_failed", "endpoints_still_listed", "endpoint_already_gone",
    # traffic
    "dispatching_replay", "worker_progress",
})


class Step(str):
    """A progress line: the English text as the ``str``, plus ``code`` and ``params``."""

    code: str
    params: dict[str, Any]

    def __new__(cls, code: str, text: str, **params: Any) -> Step:
        obj = super().__new__(cls, text)
        obj.code = code
        obj.params = params
        return obj


def step(code: str, text: str, **params: Any) -> Step:
    if code not in STEP_CODES:
        raise ValueError(f"unknown canary progress code {code!r}")
    return Step(code, text, **params)


def detail_of(message: str) -> dict[str, Any] | None:
    """``{code, params}`` of a coded line; ``None`` for plain text."""
    if isinstance(message, Step):
        return {"code": message.code, "params": dict(message.params)}
    return None
