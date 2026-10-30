"""Policy feature boundary (D20): the ``PolicyEngine`` Protocol and its registered slot.

AgentCore Policy / Policy Engines do not exist in the China partition, so Launchpad
evaluates Cedar itself. This module is the one seam a test replaces with a
``FakeEngine``: **evaluation only**. The store (ledger + S3 copies), the per-workspace
compiled-set cache, enforcement and audit are plain modules
(``policy_store`` / ``policy_cedar`` / ``policy_bundle`` / ``policy_entities``; P1-b adds
``policy_enforcement``, P1-d ``policy_audit``).

Exactly the Memory / Registry / Knowledge pattern: ``set_backend(engine)`` happens in
``app/main.py`` when ``"policy"`` is in ``Settings.features``; until then every
``/api/policies/**`` route answers 503 ``feature.unavailable {"feature": "policy"}``
through ``get_backend()``.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from app.core.features import feature_unavailable

FEATURE = "policy"

Decision = Literal["allow", "deny", "no_decision"]


@dataclass(frozen=True)
class ValidationIssue:
    """One validator finding. ``policy_id`` is the engine's positional id
    (``policy<n>``) or our ``@id`` annotation when the statement carries one; ``line`` /
    ``col`` are filled when the engine's message names a position (cedarpy 4.12 does
    not for most errors — they stay ``None`` rather than being guessed)."""

    message: str
    policy_id: str | None = None
    line: int | None = None
    col: int | None = None


@dataclass
class ValidationReport:
    """``{ok, errors[{line,col,msg}], warnings[]}`` (plan §2.4) + the stable ids the
    statement declares, so the console can show them before publish."""

    ok: bool
    errors: list[ValidationIssue] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    policy_ids: list[str] = field(default_factory=list)
    scope: str = "launchpad_schema"

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": [
                {"line": e.line, "col": e.col, "msg": e.message, "policy_id": e.policy_id}
                for e in self.errors
            ],
            "warnings": list(self.warnings),
            "policy_ids": list(self.policy_ids),
            "scope": self.scope,
        }


@dataclass(frozen=True)
class EntityRef:
    """A Cedar entity UID in the structured form the engine accepts."""

    type: str  # fully qualified, e.g. ``Launchpad::User``
    id: str

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "id": self.id}

    def __str__(self) -> str:  # ``Launchpad::User::"river"`` — for reasons and logs
        return f'{self.type}::"{self.id}"'


@dataclass(frozen=True)
class AuthzRequest:
    """One ``(principal, action, resource, context)`` question."""

    principal: EntityRef
    action: str  # ``invoke`` | ``call_tool`` | ``list_tool`` (bare; namespaced by the engine)
    resource: EntityRef
    context: dict[str, Any] = field(default_factory=dict)

    def as_cedar(self, namespace: str) -> dict[str, Any]:
        return {
            "principal": self.principal.as_dict(),
            "action": {"type": f"{namespace}::Action", "id": self.action},
            "resource": self.resource.as_dict(),
            "context": dict(self.context),
        }


@dataclass
class AuthzDecision:
    """``{decision: allow|deny|no_decision, reasons[policy ids], errors[]}``.

    ``reasons`` are the determining policies as **our** ids (the ``@id`` annotation
    when present — ``<policy_id>:v<n>`` in a compiled workspace set — else the engine's
    positional ``policy<n>``). ``no_decision`` is what a parse error, an undeclared
    entity attribute or a missing context attribute yields; callers treat it as an
    engine failure (fail-closed), never as a permit.
    """

    decision: Decision
    reasons: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    latency_ms: float = 0.0

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class PolicyEngine(Protocol):
    """Cedar evaluation only. The store (ledger + S3), enforcement and audit are plain
    modules; this is the one seam a test replaces with a FakeEngine."""

    def validate(self, statement: str, schema: str) -> ValidationReport:
        """Parse + schema-validate one statement set → {ok, errors[{line,col,msg}], warnings[]}."""
        ...

    def format(self, statement: str) -> str:
        """Canonical Cedar formatting (editor FORMAT button). Raises ``ValueError`` on a
        statement that does not parse."""
        ...

    def compile(self, statement: str) -> Any:
        """Parse once into an opaque handle ``authorize`` / ``authorize_batch`` accept in
        place of ``policies`` (the per-call re-parse is the dominant cost; the
        workspace cache keeps one handle per compiled set). Raises ``ValueError`` on a
        parse error."""
        ...

    def authorize(
        self, request: AuthzRequest, policies: Any, entities: list[dict[str, Any]],
    ) -> AuthzDecision:
        """One decision: {decision: allow|deny|no_decision, reasons[policy_ids], errors[]}.
        ``policies`` is Cedar text or a ``compile()`` handle."""
        ...

    def authorize_batch(
        self, requests: list[AuthzRequest], policies: Any, entities: list[dict[str, Any]],
    ) -> list[AuthzDecision]:
        """Bundle compilation / simulator matrix — N decisions, same order as ``requests``."""
        ...

    def policy_ids(self, statement: str) -> list[str]:
        """Stable ids for each policy in a statement set (`@id("…")` annotation if present,
        else `policy<n>`). Raises ``ValueError`` on a parse error."""
        ...


_backend: PolicyEngine | None = None


def set_backend(backend: PolicyEngine | None) -> None:
    global _backend
    _backend = backend


def backend_or_none() -> PolicyEngine | None:
    return _backend


def get_backend() -> PolicyEngine:
    if _backend is None:
        raise feature_unavailable(FEATURE)
    return _backend
