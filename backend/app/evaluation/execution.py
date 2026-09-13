"""Executable multi-actor / multi-session scenarios (``metadata.launchpad_execution``).

An ordinary predefined scenario replays its ``turns`` sequentially in ONE runtime
session under the bare ``default`` actor. That cannot test what memory-isolation
and session-freshness golden tests describe ("A tells the agent a secret, B must
not learn it", "A opens a new session, the agent must still remember"). This
module adds an **opt-in, versioned** extension inside the scenario's standard
``metadata`` dict, so the cloud dataset schema (free-form example documents)
preserves it and every non-opt-in scenario keeps the old wire behaviour exactly.

Schema (version 1) — every key is validated, unknown keys are rejected::

    "metadata": {
      "launchpad_execution": {
        "version": 1,
        "repeat": 1,                       # 1..MAX_REPEAT — replay the WHOLE
                                           # procedure with fresh actors/sessions
        "steps": [                         # exactly one step per turns[] index,
          {"turn": 0, "actor": "A", "session": "a1"},   # executed in list order
          {"turn": 1, "actor": "A", "session": "a2"},
          {"turn": 2, "actor": "B", "session": "b1"}
        ],
        "checks": [                        # optional deterministic rules
          {"id": "seed", "type": "contains", "turn": 0, "text": "amber"},
          {"id": "recall", "type": "contains", "turn": 1, "text": "amber",
           "depends_on": ["seed"]},
          {"id": "no_leak", "type": "not_contains", "turn": 2, "text": "amber",
           "depends_on": ["seed"]}
        ]
      }
    }

* ``actor`` / ``session`` are **aliases** (``^[A-Za-z][A-Za-z0-9_-]{0,31}$``), never
  raw ids. The platform mints one synthetic actor per (workspace, agent, run,
  scenario, repeat, actor alias) and one fresh runtime session per (repeat,
  actor alias, session alias). The same actor alias keeps its identity across
  its session aliases; different actor aliases never share one. A session alias
  belongs to exactly one actor alias.
* ``checks`` inspect the agent's ACTUAL response to the targeted turn only —
  never the prompt, the description or the ground truth. ``type`` is one of
  ``exact`` / ``contains`` / ``not_contains`` (literal text, case-insensitive
  unless ``case_sensitive`` is true; no regex, no expressions, no code, no LLM).
  ``depends_on`` may reference checks declared EARLIER in the list; a dependency
  that did not pass makes the dependent check ``inconclusive``, never ``pass``.
  A missing response (invoke failure / stopped run) makes the check ``error``.
* Outcomes are aggregated fail-closed: a scenario passes only when every check
  passes in every repeat.

Persistence: the run row's ``execution`` JSON records every minted session
(with the actor it ran under), every step and every check result, updated after
each step so a stopped or failed run still shows exactly which sessions exist.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.core.errors import AppError
from app.evaluation.scenarios import scenario_prompts

EXECUTION_KEY = "launchpad_execution"
EXECUTION_VERSION = 1
MAX_REPEAT = 5
MAX_ACTORS_PER_SCENARIO = 10
MAX_SESSIONS_PER_SCENARIO = 20
MAX_CHECKS_PER_SCENARIO = 20
MAX_CHECK_TEXT = 2000
# Whole-run caps, validated before any invoke: expanded invocations across all
# scenarios × repeats, and the unique sessions the batch evaluation will be
# scoped to (StartBatchEvaluation sessionMetadata max 500).
MAX_EXPANDED_CALLS = 200
MAX_UNIQUE_SESSIONS = 500
# Evidence stored on the run row is capped; full responses live in memory /
# content logs, not in the ledger.
EVIDENCE_MAX_CHARS = 400

CHECK_TYPES = ("exact", "contains", "not_contains")
CHECK_OUTCOMES = ("pass", "fail", "error", "inconclusive")

_ALIAS_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,31}$")
_ACTOR_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_STEP_KEYS = {"turn", "actor", "session"}
_CHECK_KEYS = {"id", "type", "turn", "text", "depends_on", "case_sensitive"}
_PLAN_KEYS = {"version", "repeat", "steps", "checks"}


class ExecutionError(ValueError):
    """A scenario's ``launchpad_execution`` block is malformed or out of bounds."""


@dataclass(frozen=True)
class Step:
    turn: int
    actor: str
    session: str


@dataclass(frozen=True)
class Check:
    id: str
    type: str
    turn: int
    text: str
    depends_on: tuple[str, ...] = ()
    case_sensitive: bool = False


@dataclass(frozen=True)
class ExecutionPlan:
    scenario_id: str
    repeat: int
    steps: tuple[Step, ...]
    checks: tuple[Check, ...]
    prompts: tuple[str, ...]

    @property
    def actor_aliases(self) -> list[str]:
        seen: list[str] = []
        for step in self.steps:
            if step.actor not in seen:
                seen.append(step.actor)
        return seen

    @property
    def session_keys(self) -> list[tuple[str, str]]:
        """(actor alias, session alias) pairs in first-use order."""
        seen: list[tuple[str, str]] = []
        for step in self.steps:
            key = (step.actor, step.session)
            if key not in seen:
                seen.append(key)
        return seen

    @property
    def expanded_calls(self) -> int:
        return self.repeat * len(self.steps)

    @property
    def expanded_sessions(self) -> int:
        return self.repeat * len(self.session_keys)


# ─── schema ──────────────────────────────────────────────────────────────────
def is_executable(scenario: dict[str, Any]) -> bool:
    """True when the item opts into the execution extension (key present)."""
    metadata = scenario.get("metadata")
    return isinstance(metadata, dict) and EXECUTION_KEY in metadata


def any_executable(items: list[dict[str, Any]]) -> bool:
    return any(is_executable(item) for item in items if isinstance(item, dict))


def _fail(scenario_id: str, message: str) -> ExecutionError:
    return ExecutionError(f"scenario '{scenario_id}': {EXECUTION_KEY} {message}")


def _require_keys(scenario_id: str, obj: Any, allowed: set[str], what: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise _fail(scenario_id, f"{what} must be an object")
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise _fail(scenario_id, f"{what} has unknown key(s): {', '.join(unknown)}")
    return obj


def _alias(scenario_id: str, value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ALIAS_RE.match(value):
        raise _fail(
            scenario_id,
            f"{what} must be an alias matching {_ALIAS_RE.pattern} (got {value!r})",
        )
    return value


def _turn_index(scenario_id: str, value: Any, n_turns: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(scenario_id, f"{what} turn must be an integer index")
    if not 0 <= value < n_turns:
        raise _fail(scenario_id, f"{what} turn {value} is out of range (0..{n_turns - 1})")
    return value


def parse_plan(scenario: dict[str, Any]) -> ExecutionPlan:
    """Validate one opt-in predefined scenario and return its typed plan.

    Raises ``ExecutionError`` on any shape / bound / reference violation, and
    for persona (``actor_profile``) or legacy prompt items carrying the key —
    only ``turns`` scenarios can be executed this way.
    """
    scenario_id = str(scenario.get("scenario_id") or "?")
    if "actor_profile" in scenario:
        raise _fail(scenario_id, "cannot be combined with a simulated persona (actor_profile)")
    if "turns" not in scenario:
        raise _fail(scenario_id, "requires a predefined scenario with turns[]")
    turns = scenario.get("turns")
    if not isinstance(turns, list) or not turns:
        raise _fail(scenario_id, "requires a non-empty turns[] list")
    raw = _require_keys(scenario_id, scenario["metadata"][EXECUTION_KEY], _PLAN_KEYS, "block")

    if raw.get("version") != EXECUTION_VERSION:
        raise _fail(scenario_id, f"version must be {EXECUTION_VERSION}")
    repeat = raw.get("repeat", 1)
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= MAX_REPEAT:
        raise _fail(scenario_id, f"repeat must be an integer 1..{MAX_REPEAT}")

    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise _fail(scenario_id, "steps must be a non-empty list")
    n_turns = len(turns)
    if len(steps_raw) != n_turns:
        raise _fail(
            scenario_id,
            f"steps must cover every turn exactly once ({n_turns} turns, "
            f"{len(steps_raw)} steps)",
        )
    steps: list[Step] = []
    seen_turns: set[int] = set()
    session_owner: dict[str, str] = {}
    for i, step_raw in enumerate(steps_raw):
        obj = _require_keys(scenario_id, step_raw, _STEP_KEYS, f"steps[{i}]")
        missing = _STEP_KEYS - set(obj)
        if missing:
            raise _fail(scenario_id, f"steps[{i}] is missing {', '.join(sorted(missing))}")
        turn = _turn_index(scenario_id, obj["turn"], n_turns, f"steps[{i}]")
        if turn in seen_turns:
            raise _fail(scenario_id, f"steps[{i}] repeats turn {turn}")
        seen_turns.add(turn)
        actor = _alias(scenario_id, obj["actor"], f"steps[{i}].actor")
        session = _alias(scenario_id, obj["session"], f"steps[{i}].session")
        owner = session_owner.setdefault(session, actor)
        if owner != actor:
            raise _fail(
                scenario_id,
                f"session alias '{session}' is used by actors '{owner}' and '{actor}' — "
                "a session belongs to exactly one actor",
            )
        steps.append(Step(turn=turn, actor=actor, session=session))
    actors = {s.actor for s in steps}
    if len(actors) > MAX_ACTORS_PER_SCENARIO:
        raise _fail(scenario_id, f"more than {MAX_ACTORS_PER_SCENARIO} actor aliases")
    if len(session_owner) > MAX_SESSIONS_PER_SCENARIO:
        raise _fail(scenario_id, f"more than {MAX_SESSIONS_PER_SCENARIO} session aliases")

    checks_raw = raw.get("checks", [])
    if not isinstance(checks_raw, list):
        raise _fail(scenario_id, "checks must be a list")
    if len(checks_raw) > MAX_CHECKS_PER_SCENARIO:
        raise _fail(scenario_id, f"more than {MAX_CHECKS_PER_SCENARIO} checks")
    checks: list[Check] = []
    declared: set[str] = set()
    for i, check_raw in enumerate(checks_raw):
        obj = _require_keys(scenario_id, check_raw, _CHECK_KEYS, f"checks[{i}]")
        missing = {"id", "type", "turn", "text"} - set(obj)
        if missing:
            raise _fail(scenario_id, f"checks[{i}] is missing {', '.join(sorted(missing))}")
        check_id = _alias(scenario_id, obj["id"], f"checks[{i}].id")
        if check_id in declared:
            raise _fail(scenario_id, f"checks[{i}] duplicates id '{check_id}'")
        if obj["type"] not in CHECK_TYPES:
            raise _fail(
                scenario_id, f"checks[{i}].type must be one of {', '.join(CHECK_TYPES)}"
            )
        turn = _turn_index(scenario_id, obj["turn"], n_turns, f"checks[{i}]")
        text = obj["text"]
        if not isinstance(text, str) or not text.strip():
            raise _fail(scenario_id, f"checks[{i}].text must be a non-empty string")
        if len(text) > MAX_CHECK_TEXT:
            raise _fail(scenario_id, f"checks[{i}].text exceeds {MAX_CHECK_TEXT} characters")
        depends = obj.get("depends_on", [])
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise _fail(scenario_id, f"checks[{i}].depends_on must be a list of check ids")
        for dep in depends:
            if dep == check_id:
                raise _fail(scenario_id, f"checks[{i}] depends on itself")
            if dep not in declared:
                raise _fail(
                    scenario_id,
                    f"checks[{i}] depends on '{dep}', which is not declared earlier",
                )
        case_sensitive = obj.get("case_sensitive", False)
        if not isinstance(case_sensitive, bool):
            raise _fail(scenario_id, f"checks[{i}].case_sensitive must be a boolean")
        declared.add(check_id)
        checks.append(
            Check(
                id=check_id,
                type=obj["type"],
                turn=turn,
                text=text,
                depends_on=tuple(depends),
                case_sensitive=case_sensitive,
            )
        )
    return ExecutionPlan(
        scenario_id=scenario_id,
        repeat=repeat,
        steps=tuple(steps),
        checks=tuple(checks),
        prompts=tuple(scenario_prompts(scenario)),
    )


def validate_items(items: list[dict[str, Any]]) -> None:
    """Dataset-level gate (create / upload / update / cloud run): every opt-in
    item must parse, non-``turns`` items must not carry the key, and the
    expanded totals must fit the run caps. Raises ``AppError`` 422."""
    calls = 0
    sessions = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        if is_executable(item):
            try:
                plan = parse_plan(item)
            except ExecutionError as exc:
                raise AppError("dataset.invalid_execution", str(exc), status_code=422) from exc
            calls += plan.expanded_calls
            sessions += plan.expanded_sessions
        elif "turns" in item or "actor_profile" in item:
            sessions += 1
            calls += len(item.get("turns") or []) or 1
        else:
            sessions += 1
            calls += 1
    if calls > MAX_EXPANDED_CALLS:
        raise AppError(
            "dataset.execution_limits",
            f"dataset expands to {calls} agent invocations (max {MAX_EXPANDED_CALLS}) — "
            "lower repeat counts or split the dataset",
            status_code=422,
        )
    if sessions > MAX_UNIQUE_SESSIONS:
        raise AppError(
            "dataset.execution_limits",
            f"dataset expands to {sessions} sessions (max {MAX_UNIQUE_SESSIONS})",
            status_code=422,
        )


def require_actor_envelope(items: list[dict[str, Any]], *, method: str, protocol: str) -> None:
    """Refuse an opt-in run before any AWS call when the agent's invoke path has
    no actor envelope: A2A runtimes own their conversation state and take no
    actor id, so per-actor isolation cannot be exercised against them."""
    if not any_executable(items):
        return
    if protocol == "a2a" and method != "harness":
        raise AppError(
            "run.execution_unsupported_protocol",
            "this dataset contains multi-actor/multi-session scenarios, but A2A "
            "agents carry no actor identity — they cannot run isolation procedures",
            status_code=422,
        )


# ─── identities ──────────────────────────────────────────────────────────────
def _slug(value: str, limit: int = 24) -> str:
    safe = _ACTOR_SAFE_RE.sub("-", value).strip("-")[:limit] or "s"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:6]
    return f"{safe}-{digest}"


def synthetic_actor(
    *, workspace_id: str, agent_id: str, run_id: str, scenario_id: str, repeat: int, alias: str
) -> str:
    """The memory actor id one actor alias runs under.

    Folds the agent id in first (``memory.scoped_actor`` convention, so the
    platform's per-agent partitioning and the Observability actor probes apply),
    then the workspace, run, scenario and repeat, so no two runs — or two repeats
    of one run — ever share an identity. Same alias within one repeat → same id
    across all its sessions; different aliases → different ids. Stays within the
    AgentCore ``actorId`` charset and its 255-char limit.
    """
    from app.services.memory import scoped_actor

    base = "__".join(
        [
            "eval",
            _slug(workspace_id, 16),
            run_id,
            _slug(scenario_id),
            f"r{repeat}",
            alias,
        ]
    )
    return scoped_actor(agent_id, base)


# ─── runner ──────────────────────────────────────────────────────────────────
# invoke(prompt, session_id, actor_id) -> {"text": str, "session_id": str}
Invoker = Callable[[str, str, str], dict[str, Any]]


@dataclass
class ScenarioState:
    """Mutable execution ledger for one scenario across its repeats."""

    sessions: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)


def _excerpt(text: str) -> str:
    text = text or ""
    return text if len(text) <= EVIDENCE_MAX_CHARS else text[: EVIDENCE_MAX_CHARS - 1] + "…"


def evaluate_checks(
    plan: ExecutionPlan, repeat: int, responses: dict[int, str | None]
) -> list[dict[str, Any]]:
    """Deterministic rule evaluation over the ACTUAL responses of one repeat.

    ``responses`` maps turn index → assistant text (``None`` / missing = no
    answer). Never passes without evidence: a missing answer is ``error``, an
    unmet dependency is ``inconclusive``.
    """
    outcomes: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for check in plan.checks:
        record: dict[str, Any] = {
            "scenario_id": plan.scenario_id,
            "repeat": repeat,
            "id": check.id,
            "type": check.type,
            "turn": check.turn,
            "text": _excerpt(check.text),
        }
        unmet = [d for d in check.depends_on if outcomes.get(d) != "pass"]
        response = responses.get(check.turn)
        if unmet:
            record["outcome"] = "inconclusive"
            record["evidence"] = "precondition not met: " + ", ".join(
                f"{d}={outcomes.get(d, 'missing')}" for d in unmet
            )
        elif response is None:
            record["outcome"] = "error"
            record["evidence"] = f"no assistant response recorded for turn {check.turn}"
        else:
            haystack = response if check.case_sensitive else response.casefold()
            needle = check.text if check.case_sensitive else check.text.casefold()
            if check.type == "exact":
                ok = haystack.strip() == needle.strip()
            elif check.type == "contains":
                ok = needle in haystack
            else:  # not_contains
                ok = needle not in haystack
            record["outcome"] = "pass" if ok else "fail"
            record["evidence"] = _excerpt(response)
        outcomes[check.id] = record["outcome"]
        results.append(record)
    return results


def aggregate_outcome(checks: list[dict[str, Any]]) -> str:
    """Fail-closed roll-up: fail > error > inconclusive > pass; ``none`` when
    there are no checks at all."""
    outcomes = {c.get("outcome") for c in checks}
    if not outcomes:
        return "none"
    for level in ("fail", "error", "inconclusive"):
        if level in outcomes:
            return level
    return "pass"


def run_scenario(
    plan: ExecutionPlan,
    *,
    invoke: Invoker,
    new_session_id: Callable[[], str],
    actor_for: Callable[[int, str], str],
    check_stop: Callable[[], None],
    on_step: Callable[[ScenarioState, str], None],
    state: ScenarioState | None = None,
) -> ScenarioState:
    """Execute one opt-in scenario for every repeat, sequentially.

    ``actor_for(repeat, alias)`` mints the synthetic actor; ``on_step(state,
    session_id)`` is called after EVERY invocation (success or failure) with the
    up-to-date ledger so the caller can persist it. ``check_stop`` runs before
    every invoke and before evaluating checks. Any exception (including the
    caller's stop signal) still records the partial steps, evaluates the checks
    against what was answered (missing → ``error``) and re-raises.
    """
    state = state or ScenarioState()
    for repeat in range(1, plan.repeat + 1):
        actors = {alias: actor_for(repeat, alias) for alias in plan.actor_aliases}
        session_ids: dict[tuple[str, str], str] = {}
        session_rows: dict[tuple[str, str], dict[str, Any]] = {}
        responses: dict[int, str | None] = {}
        try:
            for index, step in enumerate(plan.steps):
                check_stop()
                key = (step.actor, step.session)
                fresh = key not in session_ids
                if fresh:
                    session_ids[key] = new_session_id()
                    row = {
                        "scenario_id": plan.scenario_id,
                        "repeat": repeat,
                        "actor": step.actor,
                        "actor_id": actors[step.actor],
                        "session": step.session,
                        "requested_session_id": session_ids[key],
                        "session_id": session_ids[key],
                        "drift": False,
                        "turns": [],
                        "status": "ok",
                    }
                    session_rows[key] = row
                    state.sessions.append(row)
                row = session_rows[key]
                step_row: dict[str, Any] = {
                    "scenario_id": plan.scenario_id,
                    "repeat": repeat,
                    "index": index,
                    "turn": step.turn,
                    "actor": step.actor,
                    "session": step.session,
                    "session_id": session_ids[key],
                    "status": "ok",
                }
                state.steps.append(step_row)
                try:
                    result = invoke(plan.prompts[step.turn], session_ids[key], actors[step.actor])
                except Exception as exc:
                    step_row["status"] = "failed"
                    step_row["error"] = f"{type(exc).__name__}: {exc}"[:300]
                    row["status"] = "failed"
                    responses[step.turn] = None
                    on_step(state, session_ids[key])
                    raise
                returned = str(result.get("session_id") or session_ids[key])
                if returned != session_ids[key]:
                    # The wrapper always echoes the id it was given; record any
                    # deviation honestly and follow the runtime's id from here.
                    row["drift"] = True
                    row["session_id"] = returned
                    step_row["session_id"] = returned
                    session_ids[key] = returned
                text = str(result.get("text") or "")
                responses[step.turn] = text
                step_row["response_excerpt"] = _excerpt(text)
                row["turns"].append(step.turn)
                on_step(state, returned)
        except BaseException:
            for step in plan.steps:
                responses.setdefault(step.turn, None)
            for row in session_rows.values():
                if row["status"] == "ok" and len(row["turns"]) < sum(
                    1 for s in plan.steps if (s.actor, s.session) == (row["actor"], row["session"])
                ):
                    row["status"] = "partial"
            state.checks.extend(evaluate_checks(plan, repeat, responses))
            raise
        check_stop()
        state.checks.extend(evaluate_checks(plan, repeat, responses))
    return state


# ─── ground truth per actual session ─────────────────────────────────────────
def ground_truth_for_sessions(
    plan: ExecutionPlan, scenario: dict[str, Any], state: ScenarioState
) -> list[dict[str, Any]]:
    """``sessionMetadata`` entries for the sessions one opt-in scenario produced.

    Unlike the positional zip of ``scenarios.ground_truth_metadata``, ground
    truth is split by the ACTUAL session: each session gets the
    ``expectedResponse`` turns it replayed, in replay order; scenario-level
    ``assertions`` / ``expectedTrajectory`` attach to the session that carried
    the scenario's last step (the outcome session) so a negative-isolation
    assertion is never judged against the seeding actor's session. Every entry
    gets a unique ``testScenarioId`` (``<scenario>#r<repeat>/<session alias>``)
    and the correlation as ``metadata`` (a string map the API accepts), so the
    judge results stay attributable to alias + repeat.
    """
    last = plan.steps[-1]
    turns = scenario.get("turns", [])
    out: list[dict[str, Any]] = []
    for row in state.sessions:
        if row["scenario_id"] != plan.scenario_id or not row["turns"]:
            continue
        inline: dict[str, Any] = {}
        gt_turns = [
            {
                "input": {"prompt": plan.prompts[turn]},
                "expectedResponse": {"text": turns[turn]["expected_response"]},
            }
            for turn in row["turns"]
            if isinstance(turns[turn], dict) and turns[turn].get("expected_response")
        ]
        if gt_turns:
            inline["turns"] = gt_turns
        if (row["actor"], row["session"]) == (last.actor, last.session):
            if scenario.get("assertions"):
                inline["assertions"] = [{"text": a} for a in scenario["assertions"]]
            if scenario.get("expected_trajectory"):
                inline["expectedTrajectory"] = {"toolNames": scenario["expected_trajectory"]}
        entry: dict[str, Any] = {
            "sessionId": row["session_id"],
            "testScenarioId": f"{plan.scenario_id}#r{row['repeat']}/{row['session']}",
            "metadata": {
                "launchpad_scenario": plan.scenario_id,
                "launchpad_repeat": str(row["repeat"]),
                "launchpad_actor": row["actor"],
                "launchpad_session": row["session"],
            },
        }
        if inline:
            entry["groundTruth"] = {"inline": inline}
        out.append(entry)
    return out


# ─── run-row projection helpers ──────────────────────────────────────────────
def empty_execution(plans: list[ExecutionPlan]) -> dict[str, Any]:
    return {
        "version": EXECUTION_VERSION,
        "scenarios": [
            {
                "scenario_id": p.scenario_id,
                "repeat": p.repeat,
                "steps": len(p.steps),
                "checks": len(p.checks),
            }
            for p in plans
        ],
        "calls_planned": sum(p.expanded_calls for p in plans),
        "calls_done": 0,
        "sessions": [],
        "steps": [],
        "checks": [],
        "check_status": "none" if not any(p.checks for p in plans) else "pending",
    }


def merge_state(
    blob: dict[str, Any], states: list[ScenarioState], *, final: bool
) -> dict[str, Any]:
    """Project the per-scenario ledgers onto the run row's ``execution`` blob."""
    sessions = [s for st in states for s in st.sessions]
    steps = [s for st in states for s in st.steps]
    checks = [c for st in states for c in st.checks]
    blob = {
        **blob,
        "sessions": sessions,
        "steps": steps,
        "checks": checks,
        "calls_done": sum(1 for s in steps if s.get("status") == "ok"),
    }
    planned_checks = sum(s.get("checks", 0) for s in blob.get("scenarios", []))
    if planned_checks == 0:
        blob["check_status"] = "none"
    elif final or blob["calls_done"] >= blob.get("calls_planned", 0):
        # Fail closed: a run that never produced all its checks (stopped /
        # failed early) cannot be a pass even if every recorded check passed.
        expected = sum(
            s.get("checks", 0) * s.get("repeat", 1) for s in blob.get("scenarios", [])
        )
        status = aggregate_outcome(checks)
        if status == "pass" and len(checks) < expected:
            status = "inconclusive"
        blob["check_status"] = status
    else:
        blob["check_status"] = "pending"
    return blob


def session_actor(execution: dict[str, Any] | None, session_id: str) -> str | None:
    """The synthetic actor a persisted run session ran under (Observability
    reads Memory with it instead of the bare ``default`` actor)."""
    if not execution:
        return None
    for row in execution.get("sessions") or []:
        if row.get("session_id") == session_id or row.get("requested_session_id") == session_id:
            actor_id = row.get("actor_id")
            return str(actor_id) if actor_id else None
    return None
