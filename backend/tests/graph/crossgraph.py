"""Cross-graph walks — two lifecycle graphs interleaved on one shared entity (round 17, loop
engine, r17-b; the REFERENCE graph's dependencies walked as histories, not as single edges).

The reference graph (``graphs/reference.yaml``) names every cross-entity guard once: "delete
the agent while a canary runs → 409", "delete the dataset once the run finished → 200". The
orphan class behind N16-obs83 / obs111 / obs114 lives in the *histories* around those edges —
what the second entity does before, during and after the shared row goes. A cross-graph walk
therefore drives two worlds at once (``test_graph_crossgraph.py``):

* **agent × runtime canary** — the canary's graph on the agent the agent graph created;
* **agent × chat session** — a chat session (gated live turns included) on that agent;
* **dataset × evaluation run** — the run graph with an administrator's ``?force=true``
  dataset delete injected while the run is ACTIVE.

``Plan`` draws, from one seed, which side moves at every step and the step at which the
**delete is injected** (always in the middle third, so both the "before" and the "after"
histories are long). The oracle after EVERY step is the executor's (own-id ledger scan, the
world's extra scans, the per-table digest on every refusal) plus ``dangling_references``:
a status-aware scan that treats a ``deleted`` Agent named by a LIVE referrer as dangling —
exactly the blind spot of ``ledger_invariants.scan_control_plane`` that let N16-obs111 pass
(it only knew *missing* rows). Every rule cites the sentence that makes the state illegal.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.evaluation.models import EvalDataset, EvalRun, OnlineEvalConfig
from app.models.ledger import Agent
from app.optimization.models import ExperimentAssignment, RuntimeCanary

PAIRINGS = ("agent_x_canary", "agent_x_chat", "dataset_x_run", "agent_x_run",
            "agent_x_kbmem", "canary_x_dataset", "agent_x_offboarding")
ROUND3_PAIRINGS = ("agent_x_kbmem", "canary_x_dataset", "agent_x_offboarding")
MIN_LEN, MAX_LEN = 20, 40
VERIFY_SEEDS = 6
SOAK_SEEDS = 60

class RegisteredSymptom(AssertionError):
    """The exact observed deviation a register row names (e.g. the 202 of N17-obs13). Only this
    failure type may be xfailed by a pin — any other failure at the same step is new."""


class RegisteredDangling(AssertionError):
    """A dangling-reference violation a register row already names (matched by substring);
    only this type may be xfailed by a pin."""


#: Every register row the cross-graph walks opened — ``(register id, pairing, minimal
#: sequence)`` — counted by the loop-engine table (``loop_report.py``).
FINDINGS: tuple[tuple[str, str, str], ...] = (
    ("N17-obs12", "agent_x_chat",
     "agent: redeploy | chat: send_while_live / send_ended / other_member_post → 409 "
     "agent.not_active (the status gate pre-empts the session gates)"),
    ("N17-obs13", "agent_x_canary",
     "agent: create>deploy_succeeds | canary: seed_running>setup>traffic>verdict | agent: "
     "redeploy | canary: complete_final → 202 (expected 409 agent.deploy_in_progress)"),
)

#: evaluation-engine.md §22: "an **ACTIVE** run replaying it (`service.ACTIVE_STATUSES` =
#: queued / invoking / waiting / evaluating)" — the contract's own list, not the code's.
RUN_ACTIVE = frozenset({"queued", "invoking", "waiting", "evaluating"})
#: agents.md §1.1 / users-and-ownership.md §8: a canary "whose status is not terminal
#: (`running`)" owns its agents; completed / rolled_back / cleaned never block.
CANARY_LIVE = frozenset({"running"})


@dataclass(frozen=True)
class Plan:
    pairing: str
    seed: int
    length: int
    sides: tuple[str, ...]  # "a" (the shared entity's graph) / "b" (the dependent graph)
    inject_at: int  # step index at which the delete is injected
    redeploy_at: int = -1  # round 2: step at which a redeploy of the shared agent is injected
    flip_at: int = -1  # round 3: the administrator flips the workspace to `isolated` here …
    flip_back_at: int = -1  # … and back to `shared` here (last third)

    @property
    def id(self) -> str:
        return f"{self.pairing}/X{self.seed:04d}"


def plan(pairing: str, seed: int, *, min_len: int = MIN_LEN, max_len: int = MAX_LEN) -> Plan:
    """Deterministic in ``(pairing, seed)``. Round 2 (r17-e) adds a second injection — a
    redeploy of the shared agent — at another step of the middle third (seeds ≥ 60 only, so
    round 1's plans replay byte-identically)."""
    rng = random.Random(f"{pairing}:{seed}")
    length = rng.randint(min_len, max_len)
    sides = tuple(rng.choice(("a", "b")) for _ in range(length))
    inject_at = rng.randint(length // 3, (2 * length) // 3)
    redeploy_at = flip_at = flip_back_at = -1
    if seed >= SOAK_SEEDS or pairing == "agent_x_run" or pairing in ROUND3_PAIRINGS:
        redeploy_at = rng.randint(length // 3, (2 * length) // 3)
    if seed >= 2 * SOAK_SEEDS or pairing in ROUND3_PAIRINGS:
        # round 3 (r17-g): the visibility flip shared → isolated → shared by the administrator
        flip_at = rng.randint(length // 3, (2 * length) // 3)
        flip_back_at = rng.randint((2 * length) // 3, max((2 * length) // 3, length - 1))
    return Plan(pairing, seed, length, sides, inject_at, redeploy_at, flip_at, flip_back_at)


def loop_round() -> int:
    try:
        return max(1, int(__import__("os").environ.get("LAUNCHPAD_LOOP_ROUND", "1")))
    except ValueError:
        return 1


def cross_seeds(*, soak: bool) -> list[int]:
    """6 seeds per pairing in verify; under soak the round's 60-seed window
    ``[(r-1)·60, r·60)`` (`LAUNCHPAD_LOOP_ROUND`), so round 2 is not a replay of round 1."""
    if not soak:
        return list(range(VERIFY_SEEDS))
    r = loop_round()
    return list(range((r - 1) * SOAK_SEEDS, r * SOAK_SEEDS))


def pick(rng: random.Random, edges: list[Any]) -> Any:
    """Uniform over the enabled edges, sorted by id first so the draw never depends on
    dict / list ordering."""
    ordered = sorted(edges, key=lambda e: e.id)
    return ordered[rng.randrange(len(ordered))]


# ---------------------------------------------------------------------------
# the dangling-reference oracle
# ---------------------------------------------------------------------------


def dangling_references(session_factory: Callable[[], Any], *, agent_ids: set[str],
                        canary_ids: set[str], run_ids: set[str],
                        forced_datasets: set[str] = frozenset(),
                        notes: list[str] | None = None) -> list[str]:
    """Dangling references among the rows a cross-graph world created (one line each).

    * a `running` canary naming an Agent that is missing OR `deleted` — agents.md §1.1 /
      users-and-ownership.md §8 ("a canary … owns its agents until it is settled"; the delete
      is refused while it runs, so a running canary can never point at a deleted row);
    * assignment rows / `can_<tag>` online configs of a canary row that no longer exists —
      testing.md §6.1 `scan_control_plane` ("assignments / owner-prefixed online configs
      naming a missing … canary");
    * an ACTIVE run (§22's list) naming a missing dataset — §22 refuses the dataset's delete
      while an ACTIVE run replays it, so the state is reachable only through an
      administrator's `force` (`forced_datasets` names those: contract-permitted, appended to
      ``notes`` when given — the cross walk records it in its trace — never a violation);
    * an ACTIVE run naming a missing or `deleted` Agent — no contract sentence names this
      case (the agent delete lists its refusals exhaustively: forbidden, deploy_in_progress,
      in_use); it is reported so a walk that reaches it registers the gap.
    """
    db = session_factory()
    try:
        out: list[str] = []
        agents = {a.id: a for a in db.scalars(select(Agent)).all()}
        canaries = {
            c.id: c for c in db.scalars(select(RuntimeCanary)).all() if c.id in canary_ids
        }
        for c in canaries.values():
            if c.status not in CANARY_LIVE:
                continue
            for aid in (c.champion_agent_id, c.challenger_agent_id):
                row = agents.get(aid)
                if row is None or row.status == "deleted":
                    kind = "missing" if row is None else "deleted"
                    out.append(f"running canary {c.id} names {kind} agent {aid} (agents.md §1.1)")
        live_canaries = {c.id for c in db.scalars(select(RuntimeCanary)).all()}
        for owner in set(db.scalars(select(ExperimentAssignment.owner_id)).all()):
            if owner in canary_ids and owner not in live_canaries:
                out.append(f"assignment rows of a missing canary {owner} (testing.md §6.1)")
        tags = {cid[:8] for cid in canary_ids}
        for cfg in db.scalars(select(OnlineEvalConfig)).all():
            name = str(cfg.name)
            if name.startswith("can_") and name[4:12] in tags \
                    and not any(cid.startswith(name[4:12]) for cid in live_canaries):
                out.append(f"online config {cfg.config_id} ({name}) names a missing canary "
                           "(testing.md §6.1)")
        datasets = set(db.scalars(select(EvalDataset.id)).all())
        for run in db.scalars(select(EvalRun)).all():
            if run.id not in run_ids or run.status not in RUN_ACTIVE:
                continue
            if run.dataset_id and run.dataset_id not in datasets:
                if run.dataset_id in forced_datasets:
                    if notes is not None:
                        notes.append(f"note: ACTIVE run {run.id} names force-deleted dataset "
                                     f"{run.dataset_id} (§22 admin force — permitted)")
                else:
                    out.append(f"ACTIVE run {run.id} ({run.status}) names missing dataset "
                               f"{run.dataset_id} (evaluation-engine.md §22)")
            if run.agent_id:
                row = agents.get(run.agent_id)
                if row is None or row.status == "deleted":
                    out.append(f"ACTIVE run {run.id} ({run.status}) names "
                               f"{'missing' if row is None else 'deleted'} agent {run.agent_id} "
                               "(no contract sentence — gap)")
        return list(dict.fromkeys(out))  # champion == challenger reports the agent once
    finally:
        db.close()
