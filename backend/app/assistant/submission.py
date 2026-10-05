"""In-turn proposal submission: the ``submit_proposal`` inline function.

The Harness pauses when the model calls it and hands the call to Launchpad, which
runs the platform's own proposal validation (``proposal.validate`` behind the same
preparation merge ``record_proposal`` applies) and answers with the verdict. The
model corrects a rejected submission with change operations in the SAME reply, so
the member receives a proposal that already validates instead of a rejection and a
second full generation. Nothing is stored here: the last submission of a completed
reply becomes the turn's revision in ``service.run_turn``, exactly as a fenced
block would, and an interrupted reply stores nothing.
"""

import json
from collections.abc import Callable
from typing import Any

from app.assistant import proposal as proposal_contract

TOOL_NAME = "submit_proposal"
# ``allowedTools`` pattern that admits an inline function (live-verified 2026-09-29:
# a plain name matches builtins only, ``@inline_function/<name>`` matches nothing)
ALLOWED_PATTERN = f"@{TOOL_NAME}"
MAX_SUBMISSIONS = 8
MAX_INPUT_BYTES = 4 * proposal_contract.PROPOSAL_MAX_BYTES

TOOL = {
    "type": "inline_function",
    "name": TOOL_NAME,
    "config": {"inlineFunction": {
        "description": (
            "Submit the Launchpad proposal for validation. Pass EITHER `proposal` (the "
            "complete proposal object) OR `change` ({base_revision, operations}: RFC 6902 "
            "add/remove/replace/test operations against the current stored proposal or "
            "against the candidate an earlier submission of this reply returned). Returns "
            "`accepted`, or `rejected` with the validation errors to fix."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "proposal": {"type": "object",
                             "description": "The complete proposal object."},
                "change": {
                    "type": "object",
                    "properties": {
                        "base_revision": {"type": "integer"},
                        "operations": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["base_revision", "operations"],
                },
            },
        },
    }},
}

# (raw proposal) -> (display content, errors); the service binds catalog + preparation
Validator = Callable[[Any], tuple[Any, list[str]]]


class Submissions:
    """One turn's submissions. ``candidate`` is what the turn records at its end."""

    def __init__(
        self, validate: Validator, *, base_revision: int | None,
        base_content: dict[str, Any] | None, candidate_revision: int,
    ) -> None:
        self._validate = validate
        self.base_revision = base_revision
        self._base_content = base_content
        # the number the candidate is shown as; never stored — only a change target
        self.candidate_revision = candidate_revision
        self.candidate: dict[str, Any] | None = None
        self.count = 0

    def reset(self) -> None:
        """Forget a failed attempt's submissions: a replayed turn starts over."""
        self.candidate = None
        self.count = 0

    def handle(self, raw_input: str) -> dict[str, Any]:
        """The tool result for one call (JSON-serializable)."""
        self.count += 1
        if self.count > MAX_SUBMISSIONS:
            return self._rejected([
                f"submission limit reached ({MAX_SUBMISSIONS} per reply); end the reply, "
                "list the open problems for the member and wait for their answer"])
        if len(raw_input.encode("utf-8")) > MAX_INPUT_BYTES:
            return self._rejected([f"submission exceeds {MAX_INPUT_BYTES} bytes"])
        try:
            data = json.loads(raw_input or "{}")
        except ValueError as exc:
            return self._rejected([f"tool input is not valid JSON: {exc}"])
        if not isinstance(data, dict) or len(set(data) & {"proposal", "change"}) != 1 \
                or set(data) - {"proposal", "change"}:
            return self._rejected(["pass exactly one of `proposal` or `change`"])
        if "proposal" in data:
            raw = data["proposal"]
        else:
            raw, errors = self._apply(data["change"])
            if raw is None:
                return self._rejected(errors)
        display, errors = self._validate(raw)
        if proposal_contract.is_patch_base(display):
            self.candidate = display
        elif isinstance(raw, dict) and proposal_contract.is_patch_base(raw):
            self.candidate = raw
        if errors:
            return self._rejected(errors)
        return {
            "status": "accepted",
            "candidate_revision": self.candidate_revision,
            "next": "It becomes the member's reviewable proposal when this reply ends. Do "
                    "not print it in the reply; summarize the design and what changed in "
                    "plain words. Submit again only if the member's request needs another "
                    "change.",
        }

    def _apply(self, change: Any) -> tuple[dict[str, Any] | None, list[str]]:
        target = change.get("base_revision") if isinstance(change, dict) else None
        if self.candidate is not None and target == self.candidate_revision:
            return proposal_contract.apply_patch(self.candidate, change,
                                                 base_revision=self.candidate_revision)
        if self._base_content is not None and self.base_revision is not None:
            return proposal_contract.apply_patch(self._base_content, change,
                                                 base_revision=self.base_revision)
        return None, ["there is no stored proposal to change yet; submit the complete "
                      "proposal"]

    def _rejected(self, errors: list[str]) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "rejected", "errors": errors[:40]}
        if self.candidate is not None:
            result["candidate_revision"] = self.candidate_revision
            result["next"] = (
                "Fix exactly these errors with a `change` whose base_revision is "
                f"{self.candidate_revision} (the candidate you submitted), keep every "
                "confirmed decision, and submit again before ending the reply.")
        else:
            result["next"] = "Fix these errors and submit again before ending the reply."
        return result


def input_summary(raw_input: str) -> str:
    """What the transcript shows for a call: the proposal's name or the number of
    changes, never the JSON (the member reviews the proposal in its panel)."""
    try:
        data = json.loads(raw_input or "{}")
    except ValueError:
        return "proposal (unreadable input)"
    if isinstance(data, dict) and isinstance(data.get("proposal"), dict):
        name = data["proposal"].get("name")
        return f"proposal {name}" if isinstance(name, str) and name else "proposal"
    if isinstance(data, dict) and isinstance(data.get("change"), dict):
        ops = data["change"].get("operations")
        n = len(ops) if isinstance(ops, list) else 0
        return f"proposal revision ({n} edit{'s' if n != 1 else ''})"
    return "proposal"
