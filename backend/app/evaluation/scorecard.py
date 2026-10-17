"""Agent scorecards: the agent version a run evaluated, what it was measured on, and
how two runs line up case by case.

Ledger-only facts for the console's agent-centric evaluation views; the composite
score and regression math stay in the frontend, which owns evaluator polarity.

- **Version.** A run submitted since the scorecard shipped carries
  ``EvalRun.agent_snapshot_seq`` — the newest *deployed* ``SpecSnapshot.seq`` at
  submit time (``stamped``). An older row's version is inferred from the snapshot
  timeline (``inferred``) and never written back; a passive scope (a time window,
  chosen sessions, log streams) scores sessions that predate the run, so its version
  is only what was live when the run was made (``observed``).
- **Benchmark.** One local dataset, one cloud dataset, or the agent's live traffic.
  A local dataset's revision is the fingerprint of what the run replayed; a cloud
  dataset's is its pinned version (or the draft's fingerprint).
- **Alignment.** Two replays of one benchmark pair their sessions by scenario id
  through ``EvalRun.scenario_map``. A legacy row without the map is paired by
  position, only when it skipped no scenario and produced exactly one session per
  scenario of the items it replayed; anything else compares overall scores only.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer

from app.evaluation.models import EvalDataset, EvalRun
from app.evaluation.scenarios import input_preview, normalize_scenarios
from app.models.ledger import Agent, Deployment, SpecSnapshot
from app.services import agent_access, snapshots, visibility
from app.services.visibility import Viewer

STAMPED = "stamped"
INFERRED = "inferred"
OBSERVED = "observed"

# run scopes, from the scope encoding of `EvalRun.dataset_name` / `dataset_id`
SCOPE_DATASET = "dataset"
SCOPE_CLOUD = "cloud"
SCOPE_WINDOW = "window"
SCOPE_SESSIONS = "sessions"
SCOPE_LOGS = "logs"
SCOPE_ONLINE = "online"  # an online insights report (never on a scorecard)
PASSIVE_SCOPES = frozenset({SCOPE_WINDOW, SCOPE_SESSIONS, SCOPE_LOGS, SCOPE_ONLINE})

LIVE = "live"  # the benchmark kind + key of an agent's passive runs

ALIGN_SCENARIO = "scenario"
ALIGN_ORDER = "order"
ALIGN_NONE = "none"

SCORECARD_RUN_CAP = 1000


def _naive_utc(value: datetime | None) -> datetime | None:
    """SQLite drops the zone on read; compare everything as naive UTC."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value


def iso(value: datetime | None) -> str | None:
    """An ISO time with its UTC offset (every stored time is UTC)."""
    if value is None:
        return None
    return value.replace(tzinfo=value.tzinfo or UTC).isoformat()


# ── the stamp ─────────────────────────────────────────────────────────────────


def newest_deployed_seq(db: Session, agent_id: str | None) -> int | None:
    """The seq of the agent's newest snapshot that reached AWS — what an invoke runs.

    A newer snapshot whose deploy is still in flight (or failed) has no
    ``aws_version`` and is skipped: the live agent is still the previous one.
    """
    if not agent_id:
        return None
    return db.scalar(
        select(func.max(SpecSnapshot.seq)).where(
            SpecSnapshot.agent_id == agent_id, SpecSnapshot.aws_version.is_not(None)
        )
    )


# ── scope + benchmark ─────────────────────────────────────────────────────────


def scope_of(run: EvalRun) -> str:
    """dataset | cloud | window | sessions | logs | online, as the runs list reads it."""
    name = run.dataset_name or ""
    if run.dataset_id:
        return SCOPE_CLOUD if name.startswith("cloud:") else SCOPE_DATASET
    if name.startswith("window:"):
        return SCOPE_WINDOW
    if name.startswith("logs:"):
        return SCOPE_LOGS
    if name.startswith("online:"):
        return SCOPE_ONLINE
    return SCOPE_SESSIONS


def window_hours(run: EvalRun) -> int | None:
    """N of a ``window:Nh`` scope, else None."""
    name = run.dataset_name or ""
    if not name.startswith("window:") or not name.endswith("h"):
        return None
    try:
        return int(name[len("window:"):-1])
    except ValueError:
        return None


def dataset_labels(
    db: Session, runs: list[EvalRun], *, workspace_id: str | None, viewer: Viewer
) -> dict[str, str]:
    """``dataset_id → current name`` of the local datasets the runs replayed that the
    viewer may see (one query). A hidden dataset keeps the name stored on the run, as
    the run payload already shows it — its rename is not disclosed."""
    ids = {r.dataset_id for r in runs if r.dataset_id and scope_of(r) == SCOPE_DATASET}
    if not ids:
        return {}
    rows = db.execute(
        select(EvalDataset.id, EvalDataset.name).where(
            EvalDataset.id.in_(ids),
            EvalDataset.workspace_id == workspace_id,
            visibility.clause(viewer, EvalDataset.created_by),
        )
    )
    return dict(rows.tuples().all())


def benchmark_of(run: EvalRun, labels: dict[str, str] | None = None) -> dict[str, Any]:
    """``{kind, key, label, dataset_id, revision, size}`` — what the run was measured on.

    ``label`` is the local dataset's current name when it still exists, else the name
    stored on the run (without the ``cloud:`` prefix); null for live traffic, which the
    console names itself. ``revision`` is null when unknown (a legacy row).
    """
    scope = scope_of(run)
    if scope == SCOPE_DATASET:
        return {
            "kind": SCOPE_DATASET,
            "key": f"ds:{run.dataset_id}",
            "label": (labels or {}).get(run.dataset_id) or run.dataset_name or run.dataset_id,
            "dataset_id": run.dataset_id,
            "revision": run.dataset_fingerprint,
            "size": run.dataset_size,
        }
    if scope == SCOPE_CLOUD:
        if run.dataset_version:
            revision: str | None = f"v{run.dataset_version}"
        elif run.dataset_fingerprint:
            revision = f"draft:{run.dataset_fingerprint}"
        else:
            revision = None
        return {
            "kind": SCOPE_CLOUD,
            "key": f"cloud:{run.dataset_id}",
            "label": (run.dataset_name or "").removeprefix("cloud:") or run.dataset_id,
            "dataset_id": run.dataset_id,
            "revision": revision,
            "size": run.dataset_size,
        }
    return {"kind": LIVE, "key": LIVE, "label": None, "dataset_id": None,
            "revision": None, "size": None}


def agent_key(run: EvalRun) -> str:
    """The agent id, or ``logs:<service_name>`` for a log-source run."""
    if run.agent_id:
        return run.agent_id
    service = (run.log_source or {}).get("service_name") or run.agent_name
    return f"logs:{service}"


# ── version on read ───────────────────────────────────────────────────────────


def _deployed_timeline(
    db: Session, agent_ids: set[str]
) -> dict[str, list[tuple[int, datetime | None]]]:
    """``agent_id → [(seq, deployed_at)]`` of the deployed snapshots, by seq (2 queries).

    ``deployed_at`` is the deployment's end, else the snapshot's own creation (the
    canary snapshots and the startup baseline have no deployment).
    """
    if not agent_ids:
        return {}
    rows = db.execute(
        select(
            SpecSnapshot.agent_id, SpecSnapshot.seq, SpecSnapshot.created_at,
            SpecSnapshot.deployment_id,
        )
        .where(SpecSnapshot.agent_id.in_(agent_ids), SpecSnapshot.aws_version.is_not(None))
        .order_by(SpecSnapshot.agent_id, SpecSnapshot.seq)
    ).all()
    deployment_ids = {row.deployment_id for row in rows if row.deployment_id}
    ended: dict[str, datetime | None] = {}
    if deployment_ids:
        ended = dict(
            db.execute(
                select(Deployment.id, Deployment.ended_at).where(
                    Deployment.id.in_(deployment_ids)
                )
            ).tuples().all()
        )
    out: dict[str, list[tuple[int, datetime | None]]] = {}
    for agent_id, seq, created_at, deployment_id in rows:
        at = ended.get(deployment_id) if deployment_id else None
        out.setdefault(agent_id, []).append((seq, _naive_utc(at or created_at)))
    return out


def _predates(run: EvalRun, deployed: list[tuple[int, datetime | None]]) -> bool:
    """The run was created before the agent's oldest deployed snapshot was live."""
    created = _naive_utc(run.created_at)
    first_at = deployed[0][1] if deployed else None
    return created is not None and first_at is not None and created < first_at


def _deploy_ends(db: Session, agent_ids: set[str]) -> dict[str, list[datetime]]:
    """``agent_id → [ended_at]`` of the agents' succeeded deployments (1 query)."""
    if not agent_ids:
        return {}
    rows = db.execute(
        select(Deployment.agent_id, Deployment.ended_at).where(
            Deployment.agent_id.in_(agent_ids),
            Deployment.status == "succeeded",
            Deployment.ended_at.is_not(None),
        )
    ).all()
    out: dict[str, list[datetime]] = {}
    for agent_id, ended_at in rows:
        at = _naive_utc(ended_at)
        if at is not None:
            out.setdefault(agent_id, []).append(at)
    return out


def _inferred_seq(
    run: EvalRun, deployed: list[tuple[int, datetime | None]], deploy_ends: list[datetime]
) -> int | None:
    """The newest deployed snapshot live at ``run.created_at``. A run older than every
    snapshot (the startup baseline backfill came later) gets the oldest one only when no
    deployment of the agent succeeded between the run and that snapshot — the agent still
    ran the configuration the snapshot recorded. Otherwise the version it evaluated is
    unknown: None."""
    created = _naive_utc(run.created_at)
    live = [
        seq for seq, at in deployed if created is None or at is None or at <= created
    ]
    if live:
        return max(live)
    first_seq, first_at = deployed[0]
    if created is not None and first_at is not None and any(
        created < end < first_at for end in deploy_ends
    ):
        return None
    return first_seq


def _resolve(
    run: EvalRun, deployed: list[tuple[int, datetime | None]], deploy_ends: list[datetime]
) -> dict[str, Any] | None:
    if not run.agent_id or not deployed:
        return None
    if scope_of(run) in PASSIVE_SCOPES:
        seq = run.agent_snapshot_seq
        if seq is None:
            seq = _inferred_seq(run, deployed, deploy_ends)
        return None if seq is None else {"seq": seq, "source": OBSERVED}
    if run.agent_snapshot_seq is not None:
        return {"seq": run.agent_snapshot_seq, "source": STAMPED}
    seq = _inferred_seq(run, deployed, deploy_ends)
    return None if seq is None else {"seq": seq, "source": INFERRED}


def resolve_snapshots(db: Session, runs: list[EvalRun]) -> dict[str, dict[str, Any] | None]:
    """``run_id → {seq, source} | None`` for a page of runs, in two or three queries.

    1. no agent (a log-source run), or an agent with no deployed snapshot → None;
    2. a passive scope → the stamped seq, else the inferred one, as ``observed``;
    3. a stamped seq → ``stamped``;
    4. else ``inferred``: the newest deployed snapshot whose deploy time is at or
       before the run's creation, else the oldest deployed snapshot — unless a
       deployment succeeded between the run and that snapshot, which leaves the run's
       version unknown (None; the console reads it as older than every snapshot).
    """
    timeline = _deployed_timeline(db, {r.agent_id for r in runs if r.agent_id})
    # deployments matter only for runs older than their agent's first snapshot
    early = {
        r.agent_id for r in runs
        if r.agent_id and r.agent_snapshot_seq is None
        and _predates(r, timeline.get(r.agent_id) or [])
    }
    ends = _deploy_ends(db, early)
    return {
        r.id: _resolve(r, timeline.get(r.agent_id) or [], ends.get(r.agent_id) or [])
        for r in runs
    }


def resolve_snapshot(db: Session, run: EvalRun) -> dict[str, Any] | None:
    return resolve_snapshots(db, [run])[run.id]


# ── the scorecard read ────────────────────────────────────────────────────────


def _run_entry(
    run: EvalRun, labels: dict[str, str], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "id": run.id,
        "name": run.name,
        "agent_key": agent_key(run),
        "status": run.status,
        "error": run.error,
        "created_at": iso(run.created_at),
        "updated_at": iso(run.updated_at),
        "evaluators": list(run.evaluators or []),
        "scores": list(run.scores or []),
        "sessions": len(run.session_ids or []),
        "scope": scope_of(run),
        # the scope as stored (`window:24h`, `cloud:<name>`, …) and a pinned cloud version
        "dataset_name": run.dataset_name,
        "dataset_version": run.dataset_version,
        "benchmark": benchmark_of(run, labels),
        "snapshot": snapshot,
        "created_by_name": run.created_by_name,
    }


def _versions(
    snaps: list[SpecSnapshot], *, with_changes: bool
) -> list[dict[str, Any]]:
    """An agent's snapshot timeline, oldest first. With ``with_changes`` each entry
    carries the diff groups against the newest *deployed* snapshot before it — what
    was live before this publish — so a failed publish in between never hides a
    change from the version that followed it."""
    out: list[dict[str, Any]] = []
    live: SpecSnapshot | None = None
    for snap in snaps:
        entry: dict[str, Any] = {
            "seq": snap.seq,
            "aws_version": snap.aws_version,
            "created_at": iso(snap.created_at),
            "created_by_name": snap.created_by_name,
            "note": snap.note,
        }
        if with_changes:
            entry["changes"] = (
                snapshots.changed_groups(snapshots.diff_specs(live.spec or {}, snap.spec or {}))
                if live is not None else []
            )
        out.append(entry)
        if snap.aws_version:
            live = snap
    return out


def scorecard_out(
    db: Session,
    workspace_id: str,
    runs: list[EvalRun],
    *,
    agent_id: str | None,
    viewer: Viewer,
    truncated: bool,
) -> dict[str, Any]:
    """The scorecard payload for ``runs`` (oldest first, already filtered).

    ``agents`` are the agents those runs belong to (plus ``agent_id`` itself when it
    is visible, so its page can show a version timeline before its first run), newest
    evaluation first. A deleted or vanished agent keeps its runs under the stored
    name, with an empty timeline. ``changes`` are computed only with ``agent_id``
    (one agent's specs are loaded, never the whole workspace's).
    """
    labels = dataset_labels(db, runs, workspace_id=workspace_id, viewer=viewer)
    resolved = resolve_snapshots(db, runs)

    latest: dict[str, EvalRun] = {}
    for run in runs:  # oldest first: the last write is the newest run
        latest[agent_key(run)] = run
    platform_ids = {key for key, run in latest.items() if run.agent_id}
    if agent_id and agent_id not in platform_ids:
        target = agent_access.agent_in(db, workspace_id, agent_id, viewer=viewer,
                                       include_deleted=True)
        if target is not None:
            platform_ids.add(agent_id)
    rows = {
        a.id: a for a in db.scalars(
            select(Agent).where(Agent.id.in_(platform_ids), Agent.workspace_id == workspace_id)
        )
    } if platform_ids else {}
    live_ids = {aid for aid, a in rows.items() if a.status != "deleted"}
    snaps_by_agent: dict[str, list[SpecSnapshot]] = {}
    if live_ids:
        query = select(SpecSnapshot).where(SpecSnapshot.agent_id.in_(live_ids))
        if not agent_id:
            query = query.options(defer(SpecSnapshot.spec))
        for snap in db.scalars(query.order_by(SpecSnapshot.agent_id, SpecSnapshot.seq)):
            snaps_by_agent.setdefault(snap.agent_id, []).append(snap)

    def agent_entry(key: str, run: EvalRun | None) -> dict[str, Any]:
        if run is not None and not run.agent_id:
            source = run.log_source or {}
            return {"key": key, "agent_id": None, "name": source.get("service_name")
                    or run.agent_name, "method": None, "log_source": source,
                    "deleted": False, "current_seq": None, "versions": []}
        row = rows.get(key)
        alive = row is not None and row.status != "deleted"
        snaps = snaps_by_agent.get(key, []) if alive else []
        deployed = [s.seq for s in snaps if s.aws_version]
        return {
            "key": key,
            "agent_id": key,
            "name": row.name if alive else (run.agent_name if run is not None else row.name),
            "method": row.method if row is not None else None,
            "log_source": None,
            "deleted": not alive,
            "current_seq": max(deployed) if deployed else None,
            "versions": _versions(snaps, with_changes=bool(agent_id)),
        }

    ordered = sorted(latest.items(), key=lambda kv: (_naive_utc(kv[1].created_at)
                                                      or datetime.min), reverse=True)
    agents = [agent_entry(key, run) for key, run in ordered]
    if agent_id and agent_id not in latest and agent_id in rows:
        agents.append(agent_entry(agent_id, None))
    return {
        "runs": [_run_entry(r, labels, resolved.get(r.id)) for r in runs],
        "agents": agents,
        "truncated": truncated,
    }


# ── compare ───────────────────────────────────────────────────────────────────


def _none(reason: str) -> dict[str, Any]:
    return {"alignment": ALIGN_NONE, "reason": reason, "maps": None}


def _order_map(
    run: EvalRun, items_of: Callable[[EvalRun], list[dict[str, Any]] | None]
) -> tuple[dict[str, dict[str, str]] | None, str | None]:
    """A legacy run's session → scenario map by position, or a reason."""
    if not run.dataset_id:
        return None, "no_dataset"
    if run.scenario_failures:
        return None, "scenarios_skipped"
    items = items_of(run)
    if items is None:
        return None, "dataset_unavailable"
    scenarios = normalize_scenarios(items)
    session_ids = list(run.session_ids or [])
    if not session_ids or len(session_ids) != len(scenarios):
        return None, "session_count_mismatch"
    return {
        sid: {"scenario_id": str(s.get("scenario_id") or f"item_{i + 1}"),
              "input": input_preview(s)}
        for i, (sid, s) in enumerate(zip(session_ids, scenarios, strict=True))
    }, None


def align(
    a: EvalRun,
    b: EvalRun,
    *,
    items_of: Callable[[EvalRun], list[dict[str, Any]] | None],
) -> dict[str, Any]:
    """``{alignment, reason, maps: {a, b} | None}`` for two runs.

    - ``none`` when either is live traffic or the benchmarks differ (``items_of`` is
      never called);
    - ``scenario`` when both carry a scenario map;
    - ``order`` when a side lacks it but qualifies for position pairing — ``items_of(run)`` gives
      the items it replayed (the local dataset's current items, or the pinned cloud
      version), None when they cannot be read;
    - otherwise ``none`` with the reason.
    """
    bench_a, bench_b = benchmark_of(a), benchmark_of(b)
    if LIVE in (bench_a["kind"], bench_b["kind"]):
        return _none("live_traffic")
    if bench_a["key"] != bench_b["key"]:
        return _none("different_benchmark")
    maps: dict[str, dict[str, Any]] = {}
    alignment = ALIGN_SCENARIO
    for side, run in (("a", a), ("b", b)):
        if run.scenario_map:
            maps[side] = dict(run.scenario_map)
            continue
        mapped, reason = _order_map(run, items_of)
        if mapped is None:
            return _none(reason or "unaligned")
        maps[side] = mapped
        alignment = ALIGN_ORDER
    return {"alignment": alignment, "reason": None, "maps": maps}


def _ordered(run: EvalRun, mapping: dict[str, Any]) -> list[tuple[str, str, str]]:
    """``[(scenario_id, session_id, input)]`` in the run's own session order (dataset
    order), then any mapped session the list lacks."""
    session_ids = [sid for sid in (run.session_ids or []) if sid in mapping]
    session_ids += [sid for sid in mapping if sid not in set(session_ids)]
    out = []
    for sid in session_ids:
        entry = mapping.get(sid) or {}
        out.append((str(entry.get("scenario_id") or ""), sid, str(entry.get("input") or "")))
    return out


def build_cases(
    a: EvalRun,
    b: EvalRun,
    maps: dict[str, dict[str, Any]],
    results: dict[str, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Per-case pairs ``{scenario_id, input, a, b}`` — B's scenario order, then the
    scenarios only A has. A side is ``{session_id, results}`` or null when that run has
    no session for the scenario; ``results`` are that side's judge records (empty when
    its results stream could not be read)."""
    a_rows = _ordered(a, maps["a"])
    b_rows = _ordered(b, maps["b"])
    a_by_scenario: dict[str, tuple[str, str]] = {}
    for scenario_id, sid, text in a_rows:
        a_by_scenario.setdefault(scenario_id, (sid, text))

    def side(name: str, sid: str) -> dict[str, Any]:
        return {"session_id": sid, "results": results.get(name, {}).get(sid, [])}

    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scenario_id, sid, text in b_rows:
        if scenario_id in seen:
            continue
        seen.add(scenario_id)
        hit = a_by_scenario.get(scenario_id)
        cases.append({
            "scenario_id": scenario_id,
            "input": text or (hit[1] if hit else ""),
            "a": side("a", hit[0]) if hit else None,
            "b": side("b", sid),
        })
    for scenario_id, (sid, text) in a_by_scenario.items():
        if scenario_id in seen:
            continue
        seen.add(scenario_id)
        cases.append({"scenario_id": scenario_id, "input": text, "a": side("a", sid),
                      "b": None})
    return cases


def config_change(
    db: Session,
    a: EvalRun,
    b: EvalRun,
    snap_a: dict[str, Any] | None,
    snap_b: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """What the agent's configuration did between the two runs.

    ``{agent_id, from_seq, to_seq, between}`` when both versions are known and differ
    (``between`` = every snapshot after the lower seq up to the higher one, oldest
    first: the publishes that lead from one to the other); ``{same: true, seq}`` on
    one version; null when either version is unknown or the runs are of different
    agents. The diff itself is the snapshots diff route's.
    """
    if snap_a is None or snap_b is None or not a.agent_id or a.agent_id != b.agent_id:
        return None
    from_seq, to_seq = snap_a["seq"], snap_b["seq"]
    if from_seq == to_seq:
        return {"same": True, "agent_id": a.agent_id, "seq": from_seq}
    low, high = sorted((from_seq, to_seq))
    rows = db.scalars(
        select(SpecSnapshot)
        .options(defer(SpecSnapshot.spec))
        .where(SpecSnapshot.agent_id == a.agent_id, SpecSnapshot.seq > low,
               SpecSnapshot.seq <= high)
        .order_by(SpecSnapshot.seq)
    )
    return {
        "agent_id": a.agent_id,
        "from_seq": from_seq,
        "to_seq": to_seq,
        "between": [
            {"seq": s.seq, "aws_version": s.aws_version, "note": s.note,
             "created_at": iso(s.created_at), "created_by_name": s.created_by_name}
            for s in rows
        ],
    }
