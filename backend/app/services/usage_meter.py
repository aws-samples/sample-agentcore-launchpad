"""In-band usage metering: one ``usage_events`` row per settled charge.

A ``UsageMeter`` carries who pays and what the charge is about (payer, source,
origin workspace, agent, session, model, ref). It is passed **explicitly** down
the invoke chain — never ambient — and the low-level wrappers report usage to
its ``on_usage`` sink:

- ``hc.iter_harness_stream(on_usage=)``: every Harness ``metadata`` event. Harness
  sends one per **model call** (live probe 2026-10-15: a one-tool turn sent two,
  the second re-sending the grown context), so a turn's usage is their SUM;
- ``rt._runtime_payload_events(on_usage=)``: the Claude Agent SDK container's
  ``complete.usage`` (Anthropic field names, one whole-turn aggregate) and
  Converse-shaped ``metadata`` events from BYOC agents or converted twins;
- the codegen ``ResultMessage.usage`` (one per agent round).

``settle()`` at the end of a turn prices the accumulated tokens with the merged
price map (``services/rates.py``) — never the CLI's own ``total_cost_usd``, so
every source prices alike and admin overrides apply — writes the row, and
upserts ``usage_sessions``. A turn on an opaque runtime that reported nothing is
charged the admin's flat per-turn estimate instead, flagged ``estimated``.

Writes go through the meter's **own** short ``SessionLocal`` (streaming
generators and worker threads outlive the request's session), and the meter
**never raises** into the caller: a lost usage row is better than a broken chat
turn, and reconciliation restores measured tokens for span-visible sessions.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.budgets import UsageEvent, UsageSession
from app.services import budgets, rates

logger = logging.getLogger("launchpad.usage")

KIND_TOKENS = "tokens"
KIND_TURN_ESTIMATE = "turn_estimate"
# a long-running job's up-front estimate for work that reports no usage in-band
# (services/job_budget.py); a true-up or reconciliation supersedes it
KIND_JOB_ESTIMATE = "job_estimate"
KIND_EVALUATION_BUILTIN = "evaluation_builtin"
KIND_EVALUATION_CUSTOM = "evaluation_custom"
# written by reconciliation (services/budget_reconcile.py): AgentCore fees per session
KIND_RUNTIME_COMPUTE = "runtime_compute"
KIND_MEMORY_EVENT = "memory_event"
KIND_MEMORY_RETRIEVAL = "memory_retrieval"
KIND_GATEWAY_CALL = "gateway_call"
KIND_WEB_SEARCH = "web_search"
KIND_KB_QUERY = "kb_query"
KIND_CODE_INTERPRETER = "code_interpreter"
# what reconciliation may replace with measured span tokens
ESTIMATE_KINDS = (KIND_TURN_ESTIMATE, KIND_JOB_ESTIMATE)

# on_usage kinds: one model call (summed) or one whole-turn aggregate (which,
# when present, is the turn's usage — adding the per-call events too would count
# the same tokens twice)
USAGE_CALL = "call"
USAGE_TURN = "turn"

_KEYS = {
    "tokens_in": ("inputTokens", "input_tokens"),
    "tokens_out": ("outputTokens", "output_tokens"),
    "cache_read": ("cacheReadInputTokens", "cache_read_input_tokens"),
    "cache_write": ("cacheWriteInputTokens", "cache_creation_input_tokens"),
}


@dataclass
class TokenUsage:
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0

    def add(self, other: TokenUsage) -> None:
        self.tokens_in += other.tokens_in
        self.tokens_out += other.tokens_out
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    @property
    def total(self) -> int:
        return self.tokens_in + self.tokens_out + self.cache_read + self.cache_write


def _count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value >= 0:
        return int(value)
    return None


def normalize_usage(usage: Any) -> TokenUsage | None:
    """A token-usage mapping in Bedrock naming (Harness ``metadata.usage``, Converse
    ``TokenUsage``, Evaluate ``tokenUsage``) or Anthropic naming (Claude SDK /
    CLI ``usage``) → ``TokenUsage``; None when it carries no token count at all.
    A missing key is 0 (the live probe saw no cache keys on an uncached turn)."""
    if not isinstance(usage, Mapping):
        return None
    out = TokenUsage()
    seen = False
    for target, names in _KEYS.items():
        for name in names:
            number = _count(usage.get(name))
            if number is not None:
                setattr(out, target, number)
                seen = True
                break
    return out if seen else None


@dataclass
class _Turn:
    calls: TokenUsage = field(default_factory=TokenUsage)
    turns: TokenUsage = field(default_factory=TokenUsage)
    call_events: int = 0
    turn_events: int = 0
    cli_cost_usd: float = 0.0


class UsageMeter:
    """Meters one billable activity (a chat turn, an invoke, an assistant turn…)."""

    def __init__(
        self,
        *,
        user_id: str | None,
        user_name: str | None,
        source: str,
        origin_workspace_id: str | None = None,
        agent_id: str | None = None,
        resource_id: str | None = None,
        session_id: str | None = None,
        model_id: str | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
        opaque: bool = False,
        estimate_kind: str = KIND_TURN_ESTIMATE,
        estimate_usd: float | None = None,
    ) -> None:
        self.user_id = user_id
        self.user_name = user_name
        self.source = source
        self.origin_workspace_id = origin_workspace_id
        self.agent_id = agent_id
        self.resource_id = resource_id
        self.session_id = session_id
        self.model_id = model_id
        self.ref_type = ref_type
        self.ref_id = ref_id
        # an opaque runtime reports no usage in-band: a silent turn gets the estimate
        self.opaque = opaque
        # what that estimate is: the admin's flat per-turn default (core), or a job's
        # per-agent recent average written as ``job_estimate`` (services/job_budget.py)
        self.estimate_kind = estimate_kind
        self.estimate_usd = estimate_usd
        self._lock = threading.Lock()
        self._turn = _Turn()

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def for_payer(cls, payer: budgets.Payer, source: str, **kwargs: Any) -> UsageMeter:
        return cls(user_id=payer.user_id, user_name=payer.name, source=source, **kwargs)

    @classmethod
    def for_user(
        cls, user_id: str | None, source: str, *, user_name: str | None = None,
        **kwargs: Any,
    ) -> UsageMeter:
        """A meter for a job worker (child 2): the job row stores its payer's
        ``users.id`` at start, and the worker thread rebuilds the meter from it."""
        name = user_name
        if user_id and name is None:
            try:
                from app.models.ledger import User

                db = SessionLocal()
                try:
                    user = db.get(User, user_id)
                    name = user.username if user is not None else None
                finally:
                    db.close()
            except Exception:  # noqa: BLE001 — a display name is never worth a failure
                name = None
        return cls(user_id=user_id, user_name=name, source=source, **kwargs)

    @classmethod
    def for_agent(
        cls,
        payer: budgets.Payer,
        source: str,
        agent: Any,
        *,
        workspace_id: str | None = None,
        session_id: str | None = None,
        **kwargs: Any,
    ) -> UsageMeter:
        """A meter for turns on ``agent``: its model prices the tokens, and a
        non-Harness agent that reports nothing is charged the per-turn estimate."""
        model_id, _ = budgets.agent_model(agent)
        from app.services.runtime_discovery import is_discovered_harness

        harness = agent.method == "harness" or is_discovered_harness(agent)
        return cls(
            user_id=payer.user_id, user_name=payer.name, source=source,
            origin_workspace_id=workspace_id or agent.workspace_id,
            agent_id=agent.id, resource_id=agent.arn, session_id=session_id,
            model_id=model_id, opaque=not harness, **kwargs,
        )

    def derive(self, **overrides: Any) -> UsageMeter:
        """A fresh meter with this one's context (payer, source, refs…) and the
        given fields replaced — one per turn of a job, so concurrent turns never
        share an accumulator."""
        fields = {
            "user_id": self.user_id, "user_name": self.user_name, "source": self.source,
            "origin_workspace_id": self.origin_workspace_id, "agent_id": self.agent_id,
            "resource_id": self.resource_id, "session_id": self.session_id,
            "model_id": self.model_id, "ref_type": self.ref_type, "ref_id": self.ref_id,
            "opaque": self.opaque, "estimate_kind": self.estimate_kind,
            "estimate_usd": self.estimate_usd,
        }
        fields.update(overrides)
        return UsageMeter(**fields)

    # ── the in-band sinks ───────────────────────────────────────────────────

    def on_usage(self, usage: Any, kind: str = USAGE_CALL) -> None:
        """Accumulate one reported usage (thread-safe; never raises)."""
        try:
            tokens = normalize_usage(usage)
            if tokens is None:
                return
            with self._lock:
                if kind == USAGE_TURN:
                    self._turn.turns.add(tokens)
                    self._turn.turn_events += 1
                    cost = usage.get("total_cost_usd") if isinstance(usage, Mapping) else None
                    if isinstance(cost, int | float) and not isinstance(cost, bool):
                        self._turn.cli_cost_usd += float(cost)
                else:
                    self._turn.calls.add(tokens)
                    self._turn.call_events += 1
        except Exception:  # noqa: BLE001 — metering never breaks a turn
            logger.warning("usage meter: could not read a usage report", exc_info=True)

    def harness_usage(self, metadata: Any) -> None:
        """One Harness ``metadata`` event payload (``{usage, metrics}``)."""
        self.on_usage(metadata.get("usage") if isinstance(metadata, Mapping) else None)

    def claude_sdk_usage(self, usage: Any) -> None:
        """The Claude Agent SDK ``complete.usage`` / ``ResultMessage.usage``."""
        self.on_usage(usage, USAGE_TURN)

    def converse_usage(self, usage: Any) -> None:
        """One Converse(Stream) ``usage`` (``metadata.usage``)."""
        self.on_usage(usage)

    def record_session(self) -> None:
        """Map the meter's session to its payer now, before any usage settles: a
        turn that runs for hours (the coding agent) must be known to reconciliation
        from its start, since a session's first window starts at its first turn.
        Never raises."""
        self._upsert_session()

    @property
    def seen_usage(self) -> bool:
        with self._lock:
            return bool(self._turn.call_events or self._turn.turn_events)

    # ── settlement ──────────────────────────────────────────────────────────

    def settle(self, *, failed: bool = False, session_id: str | None = None) -> str | None:
        """Close the turn: write its tokens (or the opaque estimate) and upsert the
        session. Returns the row id written, if any. Never raises."""
        try:
            if session_id:
                self.session_id = session_id
            with self._lock:
                turn, self._turn = self._turn, _Turn()
            if turn.turn_events:
                usage = turn.turns
                detail: dict[str, Any] = {"turn_reports": turn.turn_events}
                if turn.call_events:
                    detail["model_calls_ignored"] = turn.call_events
                if turn.cli_cost_usd:
                    detail["cli_total_cost_usd"] = round(turn.cli_cost_usd, 8)
            elif turn.call_events:
                usage = turn.calls
                detail = {"model_calls": turn.call_events}
            else:
                # Nothing reported. A failed turn may never have reached the agent
                # (a refused session id is not even the payer's), so it records
                # nothing at all; reconciliation measures what really ran.
                if failed:
                    return None
                row_id = self.turn_estimate(upsert_session=False) if self.opaque else None
                self._upsert_session()
                return row_id
            if failed:
                detail["failed"] = True
            row_id = self.record_tokens(usage, detail=detail, upsert_session=False)
            if self.opaque and rates.price_of(self.model_id) is None:
                # Reported tokens the map cannot price (an imported runtime with no
                # known model) would cost the payer nothing: the gate let the turn in
                # as estimate-charged, so it is charged the estimate after all.
                self.turn_estimate(upsert_session=False)
            self._upsert_session()
            return row_id
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: settlement failed", exc_info=True)
            return None

    def record_tokens(
        self,
        usage: TokenUsage,
        *,
        model_id: str | None = None,
        kind: str = KIND_TOKENS,
        estimated: bool = False,
        detail: dict[str, Any] | None = None,
        upsert_session: bool = True,
    ) -> str | None:
        """Write one priced token row now (never raises)."""
        try:
            model = model_id or self.model_id
            usd, priced = rates.price_tokens(
                model, usage.tokens_in, usage.tokens_out, usage.cache_read, usage.cache_write,
            )
            extra = dict(detail or {})
            if not priced:
                extra["unpriced"] = True
            row_id = self._write(
                kind=kind, model_id=model, usage=usage, usd=usd, estimated=estimated,
                detail=extra or None,
            )
            if upsert_session:
                self._upsert_session()
            return row_id
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: could not record tokens", exc_info=True)
            return None

    def turn_estimate(self, *, upsert_session: bool = True) -> str | None:
        """Charge one turn of an opaque runtime its estimate: the meter's own
        ``estimate_usd`` when set (a job's per-agent average), else the admin's flat
        per-turn default."""
        try:
            usd = self.estimate_usd
            if usd is None:
                db = SessionLocal()
                try:
                    usd = budgets.budget_settings(db)["opaque_turn_estimate_usd"]
                finally:
                    db.close()
            row_id = self._write(
                kind=self.estimate_kind, model_id=self.model_id, usage=TokenUsage(),
                usd=float(usd), estimated=True, quantity=1.0, unit="turn",
            )
            if upsert_session:
                self._upsert_session()
            return row_id
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: could not record a turn estimate", exc_info=True)
            return None

    def job_estimate(
        self,
        usd: float | None,
        *,
        quantity: float,
        unit: str,
        detail: dict[str, Any] | None = None,
        model_id: str | None = None,
        upsert_session: bool = False,
    ) -> str | None:
        """Debit one up-front ``job_estimate`` row (flagged estimated) for work that
        reports no usage in-band. ``usd`` None means its model has no price: the
        row is kept (and flagged) so the admin price editor lists the model, but
        adds nothing. Never raises."""
        try:
            extra = dict(detail or {})
            if usd is None:
                extra["unpriced"] = True
            row_id = self._write(
                kind=KIND_JOB_ESTIMATE, model_id=model_id or self.model_id, usage=TokenUsage(),
                usd=None if usd is None else round(float(usd), 8), estimated=True,
                quantity=float(quantity), unit=unit, detail=extra or None,
            )
            if upsert_session:
                self._upsert_session()
            return row_id
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: could not record a job estimate", exc_info=True)
            return None

    def evaluate_usage(
        self, evaluator_id: str, token_usage: Any, *, evaluated_session: str | None = None,
    ) -> str | None:
        """One on-demand ``Evaluate`` result (exact ``tokenUsage``).

        Built-in (and third-party) evaluators are billed by AgentCore per token at
        the evaluator rates — model usage included. A custom evaluator is billed per
        evaluation, and its judge's own model tokens are priced at the platform
        judge model (an assumption, so that part is flagged estimated). The judged
        session is someone's agent session, not the payer's: it is recorded as the
        row's ``ref``, never in ``usage_sessions``.
        """
        try:
            tokens = normalize_usage(token_usage) or TokenUsage()
            db = SessionLocal()
            try:
                fee = rates.agentcore_rates(db)
            finally:
                db.close()
            ref = {"ref_type": "session", "ref_id": evaluated_session} if evaluated_session \
                else {}
            builtin = evaluator_id.startswith(("Builtin.", "ThirdParty."))
            if builtin:
                if tokens.total == 0:
                    return None  # an error result: nothing scored, nothing billed
                usd = (tokens.tokens_in / 1000 * fee["evaluation_builtin_input_per_1k"]
                       + tokens.tokens_out / 1000 * fee["evaluation_builtin_output_per_1k"])
                return self._write(
                    kind=KIND_EVALUATION_BUILTIN, model_id=None, usage=tokens,
                    usd=round(usd, 8), estimated=evaluator_id.startswith("ThirdParty."),
                    quantity=1.0, unit="evaluation",
                    detail={"evaluator_id": evaluator_id}, **ref,
                )
            from app.core.config import get_settings

            judge = (get_settings().judge_model_id or "").strip() or None
            judge_usd, priced = rates.price_tokens(
                judge, tokens.tokens_in, tokens.tokens_out, tokens.cache_read, tokens.cache_write,
            )
            detail: dict[str, Any] = {"evaluator_id": evaluator_id,
                                      "evaluation_fee_usd": fee["evaluation_custom"]}
            if tokens.total and not priced:
                detail["unpriced"] = True
            return self._write(
                kind=KIND_EVALUATION_CUSTOM, model_id=judge, usage=tokens,
                usd=round(fee["evaluation_custom"] + (judge_usd or 0.0), 8),
                estimated=bool(tokens.total), quantity=1.0, unit="evaluation",
                detail=detail, **ref,
            )
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: could not record an evaluation", exc_info=True)
            return None

    # ── storage ─────────────────────────────────────────────────────────────

    def _write(
        self,
        *,
        kind: str,
        model_id: str | None,
        usage: TokenUsage,
        usd: float | None,
        estimated: bool,
        quantity: float | None = None,
        unit: str | None = None,
        detail: dict[str, Any] | None = None,
        ref_type: str | None = None,
        ref_id: str | None = None,
    ) -> str:
        now = datetime.now(UTC)
        db = SessionLocal()
        try:
            row = UsageEvent(
                user_id=self.user_id,
                user_name=(self.user_name or None) and str(self.user_name)[:64],
                period=budgets.period_of(now),
                source=self.source,
                kind=kind,
                origin_workspace_id=self.origin_workspace_id,
                agent_id=self.agent_id,
                session_id=None if ref_type == "session" else self.session_id,
                model_id=(model_id or None) and str(model_id)[:160],
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cache_read=usage.cache_read,
                cache_write=usage.cache_write,
                quantity=quantity,
                unit=unit,
                usd=usd,
                estimated=estimated,
                ref_type=ref_type or self.ref_type,
                ref_id=ref_id or self.ref_id,
                detail=detail,
                created_at=now,
            )
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    def _upsert_session(self) -> None:
        """Record which person pays for this session (the first payer keeps it)."""
        if not self.session_id:
            return
        workspace_id = self.origin_workspace_id or DEFAULT_WORKSPACE_ID
        key = (str(self.session_id)[:128], workspace_id)
        try:
            for _attempt in range(2):
                db = SessionLocal()
                try:
                    now = datetime.now(UTC)
                    row = db.get(UsageSession, key)
                    if row is None:
                        db.add(UsageSession(
                            session_id=key[0], origin_workspace_id=key[1],
                            user_id=self.user_id,
                            user_name=(self.user_name or None) and str(self.user_name)[:64],
                            source=self.source, agent_id=self.agent_id,
                            resource_id=(self.resource_id or None)
                            and str(self.resource_id)[:256],
                            first_turn_at=now, last_turn_at=now, turns=1,
                        ))
                    else:
                        row.last_turn_at = now
                        row.turns = (row.turns or 0) + 1
                    db.commit()
                    return
                except IntegrityError:
                    db.rollback()  # a concurrent first turn inserted it: update instead
                finally:
                    db.close()
        except Exception:  # noqa: BLE001
            logger.warning("usage meter: could not record the session", exc_info=True)


def supersede(row_ids: Any, by: str, *, db: Any = None) -> int:
    """Mark estimate rows as replaced by row ``by`` (a measured row, or a better
    estimate): spend stops counting them. Rows another row already superseded
    keep their first replacement. Returns how many rows changed.

    With its own session (the default) it commits and never raises. Given the
    caller's ``db`` it only flushes the update into that transaction — and lets
    a failure raise — so a caller that must write a replacement row, supersede
    and advance a watermark atomically (reconciliation) commits all three or
    none."""
    ids = [str(row_id) for row_id in (row_ids or []) if row_id and str(row_id) != by]
    if not ids or not by:
        return 0

    def apply(session: Any) -> int:
        return int(
            session.query(UsageEvent)
            .filter(UsageEvent.id.in_(ids), UsageEvent.superseded_by.is_(None))
            .update({UsageEvent.superseded_by: by}, synchronize_session=False)
            or 0
        )

    if db is not None:
        return apply(db)
    try:
        own = SessionLocal()
        try:
            changed = apply(own)
            own.commit()
            return changed
        finally:
            own.close()
    except Exception:  # noqa: BLE001
        logger.warning("usage meter: could not supersede %s", ids, exc_info=True)
        return 0


def meter_sink(meter: UsageMeter | None) -> dict[str, Any]:
    """``{"on_usage": meter.on_usage}`` for a wrapper call, or ``{}`` without a
    meter — so callers that pass nothing keep the exact call they made before."""
    return {"on_usage": meter.on_usage} if meter is not None else {}
