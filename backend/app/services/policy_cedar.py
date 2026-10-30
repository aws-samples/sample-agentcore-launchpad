"""``PolicyEngine`` on the ``cedarpy`` wheel (Cedar 4.12) + the per-policy-engine compiled-set
cache (plan §2.1, §2.3(d); keyed by ``policy_engines.id`` since the Global model).

``cedarpy`` re-parses the policy text on every ``is_authorized`` call (≈ 0.9 ms for 50
policies as text vs ≈ 0.1 ms with a parsed ``PolicySet`` handle — probe 2026-10-14), so
the hub keeps **one compiled handle per policy engine** here and hands the Cedar engine
handles, never text (the simulator's cedarpy path; decisions on traffic are the Gateway's).
Everything cedarpy-shaped stays in this module: a wheel upgrade touches nothing else.

Cache semantics — ``CompiledSet`` per policy engine, ``version_stamp = sha256(sorted
(policy_id, active_version)) + mode``, built lazily, invalidated in-process by
``invalidate(engine_id)`` (the store calls it on publish / retire / delete)
and re-stamped from the ledger after a 30 s TTL (``SELECT id, active_version`` — one
cheap query — covers a second hub process). A stamp change rebuilds the set.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.policy import Policy, PolicyVersion
from app.policy import NAMESPACE, schema_json
from app.services.policy_backend import (
    AuthzDecision,
    AuthzRequest,
    ValidationIssue,
    ValidationReport,
    get_backend,
)

log = logging.getLogger("launchpad.policy")

STAMP_TTL_SECONDS = 30.0
_POSITION_RE = re.compile(r"line\s+(\d+)(?:[,:]?\s*(?:column|col)\s+(\d+))?", re.IGNORECASE)
_POLICY_PREFIX_RE = re.compile(r"^for policy `([^`]*)`,\s*")


# ── engine ─────────────────────────────────────────────────────────────────


class CedarEngine:
    """The production ``PolicyEngine``: cedarpy 4.12.1 with the ``Launchpad`` schema."""

    def __init__(self, schema: str | None = None) -> None:
        import cedarpy  # the only import site of the wheel

        self._cedar = cedarpy
        self._schema_text = schema or schema_json()
        self._schemas: dict[str, Any] = {}

    # -- helpers --------------------------------------------------------------
    def _schema(self, schema: str | None = None) -> Any:
        text = schema or self._schema_text
        handle = self._schemas.get(text)
        if handle is None:
            handle = self._cedar.Schema.from_json_str(text)
            self._schemas[text] = handle
        return handle

    def _static_policies(self, statement: str) -> dict[str, Any]:
        """``{policy<n>: policy-json}`` in the engine's positional order. ``ValueError`` on
        a parse error (the engine's own message)."""
        data = json.loads(self._cedar.policies_to_json_str(statement))
        if data.get("templates"):
            raise ValueError(
                "policy templates (`?principal` / `?resource` slots) are not supported"
            )
        return data.get("staticPolicies") or {}

    @staticmethod
    def _annotated_id(key: str, policy: dict[str, Any]) -> str:
        annotations = policy.get("annotations") or {}
        return str(annotations.get("id") or key)

    def _decision(self, result: Any, started: float) -> AuthzDecision:
        diagnostics = result.diagnostics
        annotated = getattr(diagnostics, "id_annotations_by_reason", None) or {}
        reasons = [str(annotated.get(r, r)) for r in (diagnostics.reasons or [])]
        value = getattr(result.decision, "value", str(result.decision))
        decision = {"Allow": "allow", "Deny": "deny"}.get(str(value), "no_decision")
        return AuthzDecision(
            decision=decision,  # type: ignore[arg-type]
            reasons=reasons,
            errors=[str(e) for e in (diagnostics.errors or [])],
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    # -- PolicyEngine ---------------------------------------------------------
    def validate(self, statement: str, schema: str) -> ValidationReport:
        if not statement or not statement.strip():
            return ValidationReport(ok=False, errors=[ValidationIssue("statement is empty")])
        try:
            policies = self._static_policies(statement)
        except ValueError as exc:
            return ValidationReport(ok=False, errors=[_issue(str(exc), None)])
        ids_by_key = {k: self._annotated_id(k, v) for k, v in policies.items()}
        try:
            result = self._cedar.validate_policies(statement, self._schema(schema))
        except ValueError as exc:  # unparsable schema text
            return ValidationReport(ok=False, errors=[_issue(f"schema error: {exc}", None)])
        errors = []
        for err in getattr(result, "errors", None) or []:
            key = str(getattr(err, "policy_id", "") or "")
            message = str(getattr(err, "error", err))
            message = _POLICY_PREFIX_RE.sub("", message)
            errors.append(_issue(message, ids_by_key.get(key) or key or None))
        ok = bool(getattr(result, "validation_passed", False)) and not errors
        return ValidationReport(ok=ok, errors=errors, policy_ids=list(ids_by_key.values()))

    def format(self, statement: str) -> str:
        return self._cedar.format_policies(statement)

    def compile(self, statement: str) -> Any:
        return self._cedar.PolicySet.from_str(statement)

    def authorize(
        self, request: AuthzRequest, policies: Any, entities: list[dict[str, Any]],
    ) -> AuthzDecision:
        started = time.perf_counter()
        result = self._cedar.is_authorized(
            request.as_cedar(NAMESPACE), policies, entities, self._schema()
        )
        return self._decision(result, started)

    def authorize_batch(
        self, requests: list[AuthzRequest], policies: Any, entities: list[dict[str, Any]],
    ) -> list[AuthzDecision]:
        if not requests:
            return []
        started = time.perf_counter()
        results = self._cedar.is_authorized_batch(
            [r.as_cedar(NAMESPACE) for r in requests], policies, entities, self._schema()
        )
        return [self._decision(r, started) for r in results]

    def policy_ids(self, statement: str) -> list[str]:
        return [self._annotated_id(k, v) for k, v in self._static_policies(statement).items()]

    # -- extra (not part of the Protocol; the compiler uses it when present) ---
    def parse_policies(self, statement: str) -> dict[str, Any]:
        """Cedar's parsed static policies, without interpreting string literals as code."""
        return self._static_policies(statement)

    def render_policies(self, policies: dict[str, Any]) -> str:
        """Render a transformed Cedar EST through the same parser's JSON boundary."""
        if not policies:
            return ""
        return self._cedar.policies_from_json_str(
            json.dumps({"staticPolicies": policies, "templates": {}, "templateLinks": []})
        )

    def annotate(self, statement: str, stamp: str) -> str:
        """Return ``statement`` with every policy carrying ``@id("<stamp>")`` (one policy)
        or ``@id("<stamp>#<n>")`` (several), replacing any author-supplied ``@id`` so the
        determining ids a decision reports are always ``<policy_id>:v<n>[#k]``. Other
        annotations are kept. Round-trips through Cedar JSON, so the text is
        re-rendered canonically."""
        policies = self._static_policies(statement)
        many = len(policies) > 1
        for index, (_key, policy) in enumerate(policies.items()):
            annotations = dict(policy.get("annotations") or {})
            annotations["id"] = f"{stamp}#{index}" if many else stamp
            policy["annotations"] = annotations
        return self.render_policies(policies)


def _issue(message: str, policy_id: str | None) -> ValidationIssue:
    line = col = None
    match = _POSITION_RE.search(message)
    if match:
        line = int(match.group(1))
        col = int(match.group(2)) if match.group(2) else None
    return ValidationIssue(message=message, policy_id=policy_id, line=line, col=col)


# ── compiled policy sets ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ActivePolicy:
    """One ACTIVE policy as the cache holds it: the annotated statement (no agent scope since
    the Global model — a policy of an engine applies to every call its gateways see)."""

    policy_id: str
    version: int
    statement: str  # annotated with ``@id("<policy_id>:v<version>")``
    dialect: str = "cedar"  # ``dogwood`` rows are the engine's alone (cedarpy fail-closed)

    @property
    def stamp(self) -> str:
        return f"{self.policy_id}:v{self.version}"


@dataclass
class CompiledSubset:
    """The compiled handle for one applicable subset (one agent, or the whole set)."""

    text: str
    handle: Any
    policy_versions: dict[str, int]
    # stamps of applicable ACTIVE ``dialect='dogwood'`` rows: cedarpy cannot evaluate them
    # (another vocabulary, temporal operators) — a decision over such a subset is a
    # ``policy_evaluation_error`` deny, never a skip (``policy_bundle.dogwood_denial``)
    dogwood: tuple[str, ...] = ()

    @property
    def policy_ids(self) -> list[str]:
        return list(self.policy_versions)


@dataclass
class CompiledSet:
    engine_id: str
    version_stamp: str
    mode: str
    policies: tuple[ActivePolicy, ...]
    built_at: float = field(default_factory=time.monotonic)
    checked_at: float = field(default_factory=time.monotonic)
    _subset: CompiledSubset | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def active_count(self) -> int:
        return len(self.policies)

    def subset(self) -> CompiledSubset:
        """The compiled handle over the engine's whole ACTIVE set (compiled once)."""
        with self._lock:
            if self._subset is None:
                cedar = [p for p in self.policies if p.dialect != "dogwood"]
                text = "\n".join(p.statement for p in cedar)
                self._subset = CompiledSubset(
                    text=text,
                    handle=compile_text(text),
                    policy_versions={p.policy_id: p.version for p in self.policies},
                    dogwood=tuple(p.stamp for p in self.policies if p.dialect == "dogwood"),
                )
            return self._subset

    def for_agent(self, agent_id: str | None = None,
                  extra_policy_ids: tuple[str, ...] = ()) -> CompiledSubset:
        """Kept for callers of the pre-Global signature: the agent no longer selects a subset."""
        return self.subset()


_CACHE: dict[str, CompiledSet] = {}
_CACHE_LOCK = threading.Lock()


def compile_text(text: str) -> Any:
    """Parse once through the registered engine (an empty set is a valid, deny-all set).

    A set that no longer parses is handed to the engine as raw text instead of raising:
    every decision over it is then ``no_decision`` with the parse error in
    ``errors`` — fail-closed and visible, never a 500 on the hot path."""
    engine = get_backend()
    if not text.strip():
        text = "// empty policy set — Cedar default-deny\n"
    try:
        return engine.compile(text)
    except ValueError as exc:
        log.error("compiled policy set does not parse — decisions will be NoDecision: %s", exc)
        return text


def annotate(statement: str, stamp: str) -> str:
    """Stamp ``@id`` annotations through the engine when it can (``CedarEngine``); a
    FakeEngine without ``annotate`` leaves the text as-is."""
    engine = get_backend()
    fn = getattr(engine, "annotate", None)
    return fn(statement, stamp) if callable(fn) else statement


def version_stamp(db: Session, engine_id: str, mode: str) -> str:
    rows = db.execute(
        select(Policy.id, Policy.active_version)
        .where(Policy.engine_id == engine_id, Policy.status == "active")
        .order_by(Policy.id)
    ).all()
    digest = hashlib.sha256()
    for policy_id, version in rows:
        digest.update(f"{policy_id}:{version}\n".encode())
    return f"{digest.hexdigest()[:32]}+{mode}"


def _build(db: Session, engine_id: str, mode: str, stamp: str) -> CompiledSet:
    rows = db.execute(
        select(Policy, PolicyVersion)
        .join(
            PolicyVersion,
            (PolicyVersion.policy_id == Policy.id)
            & (PolicyVersion.version == Policy.active_version),
        )
        .where(Policy.engine_id == engine_id, Policy.status == "active")
        .order_by(Policy.created_at, Policy.id)
    ).all()
    policies = []
    for policy, version in rows:
        statement = version.statement
        dialect = getattr(version, "dialect", None) or "cedar"
        try:
            if dialect != "dogwood":
                statement = annotate(statement, f"{policy.id}:v{version.version}")
        except ValueError as exc:
            # An ACTIVE version that no longer parses (schema or wheel change) must be
            # visible as NoDecision at evaluation, not silently dropped from the set.
            log.error("policy %s v%s no longer parses: %s", policy.id, version.version, exc)
        policies.append(
            ActivePolicy(
                policy_id=policy.id,
                version=int(version.version),
                statement=statement,
                dialect=dialect,
            )
        )
    return CompiledSet(
        engine_id=engine_id, version_stamp=stamp, mode=mode, policies=tuple(policies),
    )


def compiled_set(db: Session, engine_id: str, mode: str = "log_only") -> CompiledSet:
    """The policy engine's compiled ACTIVE set — cached, TTL-re-stamped, rebuilt on change.
    A workspace id resolves to its default engine (the pre-engine call form)."""
    from app.models.policy import PolicyEngine

    if db.get(PolicyEngine, engine_id) is None:
        from app.services import policy_store

        engine_id = policy_store._engine_arg(db, engine_id).id
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(engine_id)
        if cached is not None and now - cached.checked_at < STAMP_TTL_SECONDS:
            if cached.mode == mode:
                return cached
    stamp = version_stamp(db, engine_id, mode)
    with _CACHE_LOCK:
        cached = _CACHE.get(engine_id)
        if cached is not None and cached.version_stamp == stamp:
            cached.checked_at = now
            return cached
    built = _build(db, engine_id, mode, stamp)
    with _CACHE_LOCK:
        _CACHE[engine_id] = built
    return built


def invalidate(engine_id: str | None = None) -> None:
    """Drop the compiled set of one policy engine (or all). Called by the store after
    publish / retire / delete and by the test fixtures."""
    with _CACHE_LOCK:
        if engine_id is None:
            _CACHE.clear()
        else:
            _CACHE.pop(engine_id, None)


def cached_or_none(engine_id: str) -> CompiledSet | None:
    with _CACHE_LOCK:
        return _CACHE.get(engine_id)
