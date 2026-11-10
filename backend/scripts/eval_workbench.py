"""Local evaluation workbench — verify an evaluation change on REAL records in minutes.

    cd backend && uv run python scripts/eval_workbench.py --run fb8a7e230b11 --host default
    make eval-local RUN=fb8a7e230b11                      # M1 (extraction) only
    make eval-local RUN=fb8a7e230b11 EVAL_ARGS=--judge    # + local judges
    make eval-diff RUN=fb8a7e230b11                       # + Global Evaluate, M2–M4
    make eval-local EVAL_ARGS=--calibration               # judges vs constructed labels (κ)
    make judge-calibration                                 # full set, recorded for the gate
    make judge-drift                                       # daily mini-run vs the record

Runs the production evaluation code of the checkout it is started from (``app.*`` of this
``backend/``, or of ``--backend <other checkout>/backend``) over cached CloudWatch records.
Read-only on AWS: Logs Insights reads, one SSM ``RunShellScript`` that opens the run ledger
``mode=ro`` (``--run``), one SSM ``GetParameter`` (``--judge``), Global ``Evaluate``
(``--global``). Guide: docs/cn/interfaces/evaluation-engine.md §26.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import subprocess
import sys
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_BACKEND = SCRIPTS.parent
CALIBRATION_PAIRS = DEFAULT_BACKEND / "tests" / "fixtures" / "judge_calibration" / "pairs.json"
# EVAL-TRUST 2.1b: the hard tier (``--calibration-tier hard``), recorded as <fp>-hard.*
HARD_CALIBRATION_PAIRS = CALIBRATION_PAIRS.with_name("hard_pairs.json")
# R3-e: the calibration reports the judge gate accepts, one per judge fingerprint (under the
# checkout whose judges ran, ``--backend``'s parent)
CALIBRATION_RECORDS = Path("docs") / "cn" / "reports" / "judge-calibration"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_argument_group("input")
    src.add_argument("--run", help="eval run id (sessions + log group from the host ledger)")
    src.add_argument("--sessions", nargs="+", default=[],
                     help="session ids (space or comma separated); needs --log-group")
    src.add_argument("--log-group", help="the agent runtime group (override for --run)")
    src.add_argument("--fixtures", nargs="+", default=[], type=Path,
                     help="offline: session record files (cache-entry / fixture shape)")
    src.add_argument("--corpus", type=Path,
                     help="probe corpus results.jsonl (session ids, agents, tags, turns)")
    src.add_argument("--host", choices=["default", "t1"], default="default")
    src.add_argument("--instance-id", help="host instance (default: from its EC2 stack)")
    src.add_argument("--lookback-hours", type=int, default=168,
                     help="fetch window without --run (default 168)")
    src.add_argument("--refresh", action="store_true", help="re-fetch records and the run")
    src.add_argument("--offline", action="store_true",
                     help="no Logs / ledger fetch: records and runs from the cache only "
                          "(--judge still reads the Bedrock key from CN SSM)")
    src.add_argument("--cache-dir", type=Path, default=DEFAULT_BACKEND / ".eval-cache")
    run = p.add_argument_group("evaluation")
    run.add_argument("--modes", default="both",
                     help="M1 provider modes: both, runtime_only (comma separated)")
    run.add_argument("--judge", action="store_true", help="score locally with the judges")
    run.add_argument("--evaluators", help="comma separated (default: the run's evaluators)")
    run.add_argument("--judge-model", help="default: the host's eval_judge_model_id")
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--global", dest="use_global", action="store_true",
                     help="also send the spans to Global AgentCore Evaluate (M2–M4)")
    run.add_argument("--global-profile", default="default",
                     help="commercial-partition AWS profile for Evaluate (default: default)")
    run.add_argument("--global-reps", type=int, default=1, help="Global reruns (variance)")
    run.add_argument("--global-results", help="Evaluate responses produced elsewhere (.jsonl)")
    run.add_argument("--global-own-spans", action="store_true",
                     help="A2A workaround: send a session that carries a called peer's spans "
                          "with its own-service spans only (rows marked in the report)")
    cal = p.add_argument_group("calibration (EVAL-TRUST 2.1–2.4)")
    cal.add_argument("--calibration", nargs="?", type=Path, const=CALIBRATION_PAIRS,
                     help="judge the constructed calibration pairs instead of sessions "
                          "(default tests/fixtures/judge_calibration/pairs.json)")
    cal.add_argument("--calibration-models", default="global.zai.glm-5.3",
                     help="comma separated judge models (cross-judge agreement)")
    cal.add_argument("--reps", type=int, default=5, help="reruns per item (variance)")
    cal.add_argument("--calibration-evaluators",
                     help="comma separated subset of the evaluators the pairs name")
    cal.add_argument("--calibration-record", action="store_true",
                     help="also write the report to docs/cn/reports/judge-calibration/"
                          "<fingerprint>.{json,md} of the --backend checkout — what the "
                          "judge gate in make verify reads")
    cal.add_argument("--calibration-tier", choices=("easy", "hard"), default="easy",
                     help="hard = tests/fixtures/judge_calibration/hard_pairs.json (EVAL-TRUST "
                          "2.1b, no verbosity / position probes; recorded as <fp>-hard)")
    cal.add_argument("--calibration-rerender", type=Path,
                     help="re-render a stored calibration.json (its judge rows) at today's "
                          "product pass bars, without calling the judges; with "
                          "--calibration-record it rewrites that tier's record")
    cal.add_argument("--compare-to", type=Path,
                     help="an easy-tier calibration.json (or record) to put next to a hard run")
    cal.add_argument("--calibration-mini", action="store_true",
                     help="R3-e drift watch: the mini-set (base variant only), compared with "
                          "the recorded report of the current fingerprint; exit 1 on drift")
    cal.add_argument("--calibration-injection", action="store_true",
                     help="R3-b: judge the wrong sides as is and with an instruction to the "
                          "judge planted in the answer / a tool result / a SKILL.md")
    cal.add_argument("--self-preference", action="store_true",
                     help="R3-a: every calibration question answered by each "
                          "--calibration-models agent, each answer judged by every judge")
    cal.add_argument("--drift-against", type=Path,
                     help="the recorded report to compare with (default: the current "
                          "fingerprint's record)")
    out = p.add_argument_group("output")
    out.add_argument("--round", help="ledger round label for the metric row")
    out.add_argument("--report-dir", type=Path, help="default: <cache>/reports/<timestamp>")
    out.add_argument("--strict", action="store_true", help="exit 1 unless M1 is 100 %%")
    out.add_argument("--backend", type=Path, default=DEFAULT_BACKEND,
                     help="the checkout's backend/ whose production code runs")
    args = p.parse_args(argv)
    args.sessions = [s for raw in args.sessions for s in raw.split(",") if s]
    args.modes = [m for m in args.modes.split(",") if m]
    if args.calibration_rerender and not args.calibration:
        args.calibration = CALIBRATION_PAIRS
    if not (args.run or args.sessions or args.fixtures or args.corpus or args.calibration):
        p.error("pass --run, --sessions (with --log-group), --corpus or --fixtures")
    if args.sessions and not (args.log_group or args.run):
        p.error("--sessions needs --log-group")
    if args.use_global and not args.judge:
        args.judge = True  # M2–M4 compare the local judges with Global
    return args


def code_identity(backend: Path) -> dict[str, Any]:
    def git(*cmd: str) -> str:
        done = subprocess.run(["git", "-C", str(backend), *cmd], capture_output=True,
                              text=True, check=False)
        return done.stdout.strip()

    return {"sha": git("rev-parse", "HEAD") or "unknown",
            "dirty": bool(git("status", "--porcelain", "--", ".")), "backend": str(backend)}


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    backend = args.backend.resolve()
    sys.path[:0] = [str(backend), str(SCRIPTS)]

    from evalwb import corpus, fetch, judge, metrics, reconcile, report
    from evalwb import global_eval as gev
    from evalwb.cache import RecordCache, load_file

    cache = RecordCache(args.cache_dir)
    # --offline = no Logs / ledger fetch. The CN session is still opened lazily for the one
    # read the judges need (the Global Bedrock key in CN SSM).
    sessions_cache: dict[str, Any] = {}

    def cn_session() -> Any:
        if "cn" not in sessions_cache:
            sessions_cache["cn"] = fetch.cn_session()
        return sessions_cache["cn"]

    cn = None if args.offline else cn_session()
    if args.calibration_rerender:  # stored rows only — no judge call, no AWS
        return rerender_calibration(args, backend, cache)
    if args.calibration:  # judges only — the key read opens the session even with --offline
        return run_calibration(args, backend, cache, cn_session(), judge)
    payload: dict[str, Any] | None = None
    if args.run:
        key = f"{args.host}-{args.run}"
        payload = None if args.refresh else cache.load_json("runs", key)
        if payload is None:
            if cn is None:
                print(f"run {args.run} is not cached and --offline is set", file=sys.stderr)
                return 2
            print(f"resolving run {args.run} on {args.host} (read-only ledger over SSM)")
            payload = fetch.resolve_run(cn, args.host, args.run, instance_id=args.instance_id)
            cache.save_json("runs", key, payload)

    entries = [load_file(f) for f in args.fixtures]
    session_ids = list(args.sessions or (payload["run"]["session_ids"] if payload else []))
    log_group = args.log_group or (fetch.run_log_group(payload) if payload else None)
    # (log group, sessions, (start, end)) fetch groups
    plan: list[tuple[str, list[str], tuple[datetime, datetime]]] = []
    probe: list[Any] = []
    if args.corpus:
        probe = corpus.load(args.corpus)
        ids = sorted({c.agent_id for c in probe})
        key = f"agents-{args.host}-" + hashlib.sha256(",".join(ids).encode()).hexdigest()[:12]
        agents = None if args.refresh else cache.load_json("runs", key)
        if agents is None:
            if cn is None:
                print("corpus agents not resolved and --offline is set", file=sys.stderr)
                return 2
            agents = fetch.resolve_agents(cn, args.host, ids, instance_id=args.instance_id)
            cache.save_json("runs", key, agents)
        groups: dict[str, list[Any]] = {}
        for c in probe:
            resource = (agents.get(c.agent_id) or {}).get("resource_id")
            if not resource:
                print(f"  {c.session_id}: agent {c.agent} has no runtime — skipped")
                continue
            groups.setdefault(fetch.runtime_log_group(resource), []).append(c)
        for group, members in groups.items():
            plan.append((group, [c.session_id for c in members], corpus.window(members)))
        session_ids = [c.session_id for c in probe if any(
            c.session_id in ids_ for _, ids_, _ in plan)]
    elif session_ids:
        plan.append((log_group, session_ids, fetch.fetch_window(
            (payload or {}).get("run", {}).get("created_at"), args.lookback_hours)))
    cached = {} if args.refresh else {s: e for s in session_ids if (e := cache.load(s))}
    for group, ids_, (start, end) in plan:
        missing = [s for s in ids_ if s not in cached]
        if not missing:
            continue
        if cn is None:
            print(f"{len(missing)} sessions not cached and --offline is set", file=sys.stderr)
            return 2
        print(f"fetching {len(missing)} sessions from {group} + aws/spans "
              f"({start:%Y-%m-%d %H:%M} → {end:%Y-%m-%d %H:%M} UTC)")
        for entry in fetch.fetch_sessions(cn.client("logs"), missing, group, start, end):
            if not entry.records:  # not cached: a later run (other window) asks again
                print(f"  {entry.session_id[:12]}: no records in the window")
                continue
            cache.save(entry)
            cached[entry.session_id] = entry
    entries += [cached[s] for s in session_ids if s in cached]
    sessions = {e.session_id: e for e in entries}

    code = code_identity(backend)
    print(f"\ncode under test: {code['sha'][:12]}{' +dirty' if code['dirty'] else ''} "
          f"({backend})")
    m1: dict[str, Any] = {}
    for mode in args.modes:
        results = [reconcile.reconcile_session(e, mode, sessions) for e in entries]
        summary = reconcile.m1_summary(results)
        m1[mode] = {"summary": summary, "sessions": [r.to_json() for r in results]}
        print(f"\nM1 [{mode}] {report.m1_cell(summary)} completed turns match; failed turns "
              f"{summary['failed_turns']} (wrong {summary['failed_wrong']}, leaks "
              f"{summary['leaks']}); sessions clean "
              f"{summary['sessions_ok']}/{summary['sessions']}")
        shown = m1[mode]["sessions"]
        if len(shown) > 30:  # a corpus: print the turns that are not clean only
            shown = [{**x, "rows": [r for r in x["rows"]
                                    if r["status"] not in ("match", "failed_turn")]}
                     for x in shown]
            shown = [x for x in shown if x["rows"] or x.get("error") or not x["order_ok"]]
        print(report.m1_table(shown))
        if probe:
            errors = corpus.error_turns(results, probe)
            m1[mode]["corpus"] = {"tags": corpus.tag_breakdown(results, probe),
                                  "error_turns": errors,
                                  "error_summary": corpus.error_summary(errors)}
            print(f"error turns [{mode}]: {m1[mode]['corpus']['error_summary']}")

    data: dict[str, Any] = {
        "run": args.run, "host": args.host, "sessions": [e.session_id for e in entries],
        "log_group": log_group or (entries[0].log_group if entries else ""),
        "source": ("fixtures" if args.fixtures and not session_ids
                   else f"corpus {args.corpus}" if args.corpus else "cache"),
        "missing_sessions": [s for s in session_ids if s not in sessions]
        + ([c.session_id for c in probe if c.session_id not in session_ids]),
        "cache": str(cache.root), "modes": args.modes,
        "host_head": (payload or {}).get("host_head"),
    }
    out: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                           "round": args.round, "code": code, "input": data, "m1": m1}

    truth = judge.ground_truth(payload, list(sessions))
    evaluator_ids = ([e for e in args.evaluators.split(",") if e] if args.evaluators
                     else list((payload or {}).get("run", {}).get("evaluators") or []))
    tool_spans = {e.session_id: gev.tool_units(e) for e in entries}
    agent_traces = {e.session_id: gev.agent_traces(e) for e in entries}
    shape = {"agent_traces": agent_traces, "tool_spans": tool_spans, "sessions": list(sessions)}
    comparisons: dict[str, Any] = {}
    local: list[dict[str, Any]] = []
    if args.judge:
        from app.services.strands_eval.results import aggregate_scores

        defs, skipped = judge.definitions(evaluator_ids, (payload or {}).get("evaluators") or [])
        model = judge.judge_model(args.judge_model, payload)
        judge.load_bedrock_key(cn or cn_session())
        print(f"\njudging {len(sessions)} sessions × {len(defs)} evaluators with {model}")
        provider = reconcile.provider_for(entries[0], sessions, args.modes[0])
        local = judge.evaluate(provider, list(sessions), defs, truth, model,
                               concurrency=args.concurrency, progress=print)
        out["judge"] = {"model": model, "evaluators": [d["id"] for d in defs],
                        "skipped": skipped, "records": local, "scores": aggregate_scores(local)}
        if payload and payload.get("results"):
            comparisons["local_vs_recorded"] = metrics.compare(
                metrics.units(local, tool_spans, agent_traces=agent_traces),
                metrics.units(payload["results"], tool_spans, agent_traces=agent_traces),
                [d["id"] for d in defs], **shape)
    if args.use_global or args.global_results:
        if args.global_results:
            records, skipped, source = gev.load_results(args.global_results), [], \
                args.global_results
        else:
            print(f"\nGlobal Evaluate ({gev.GLOBAL_REGION}, profile {args.global_profile})")
            client = gev.global_client(args.global_profile)
            records, skipped = gev.run(client, cache, entries, evaluator_ids, truth,
                                       reps=max(1, args.global_reps),
                                       own_spans=args.global_own_spans)
            source = f"Evaluate via profile {args.global_profile}"
            if args.global_own_spans:
                source += " (A2A workaround: own-service spans for peer-calling sessions)"
        out["global"] = {"source": source, "reps": args.global_reps, "skipped": skipped,
                         "records": records}
        comparisons["local_vs_global"] = metrics.compare(
            metrics.units(local, tool_spans, agent_traces=agent_traces),
            metrics.units(records, tool_spans, rep=0), **shape)
        comparisons["global_rerun"] = metrics.rerun_variance(records, args.global_reps,
                                                             tool_spans)
        out["global"]["errors"] = metrics.error_counts(records)
        own = sorted({r["session_id"] for r in records if r.get("own_spans_only")})
        out["global"]["own_spans_sessions"] = own
        if own:  # the workaround's rows on their own, so they can be read apart
            subset = {**shape, "sessions": own}
            comparisons["local_vs_global_own_spans"] = metrics.compare(
                metrics.units([r for r in local if r["session_id"] in own], tool_spans,
                              agent_traces=agent_traces),
                metrics.units([r for r in records if r["session_id"] in own], tool_spans, rep=0),
                **subset)
            mine = [r for r in records if r["session_id"] in own]
            comparisons["global_rerun_own_spans"] = metrics.rerun_variance(
                mine, args.global_reps, tool_spans)
            out["global"]["errors_own_spans"] = metrics.error_counts(mine)
    out["comparisons"] = comparisons
    out["ledger_row"] = report.ledger_row(out)

    directory = args.report_dir or cache.root / "reports" / datetime.now(UTC).strftime(
        "%Y%m%dT%H%M%SZ")
    md, js = report.write(out, directory)
    print(f"\nreport: {md}\n        {js}\nledger row:\n{out['ledger_row']}")
    if args.strict and any(v["summary"]["matched"] != v["summary"]["denominator"]
                           for v in m1.values()):
        return 1
    return 0


def run_calibration(args: argparse.Namespace, backend: Path, cache: Any, cn: Any,
                    judge: Any) -> int:
    """``--calibration``: judge the constructed pairs (``evalwb.calibration``), write
    ``calibration.md`` / ``calibration.json`` under the report directory."""
    import json

    from evalwb import calibration

    try:
        from app.evaluation.judge_fingerprint import fingerprint as judge_fingerprint
    except ImportError:  # a --backend checkout from before R3-e
        def judge_fingerprint() -> str:
            return "unknown"

    hard = args.calibration_tier == "hard"
    if hard and args.calibration == CALIBRATION_PAIRS:
        args.calibration = HARD_CALIBRATION_PAIRS
    pairs = calibration.load(args.calibration)
    if args.self_preference:
        return run_self_preference(args, backend, cache, cn, judge, pairs,
                                   judge_fingerprint())
    full = len(pairs)
    if args.calibration_mini:
        pairs = calibration.mini(pairs)
    models = [m for m in args.calibration_models.split(",") if m]
    subset = ([e for e in args.calibration_evaluators.split(",") if e]
              if args.calibration_evaluators else None)
    work = calibration.items(pairs, subset, probes=not (args.calibration_mini or hard),
                             injection=args.calibration_injection)
    judge.load_bedrock_key(cn)
    print(f"calibration: {len(pairs)} pairs → {len(work)} items × {len(models)} models × "
          f"{args.reps} reps")
    rows = calibration.judge(work, models, max(1, args.reps), concurrency=args.concurrency,
                             progress=print)
    rows = [{k: judge.scrub(v) for k, v in r.items()} for r in rows]
    summary = calibration.summarize(rows)
    code = code_identity(backend)
    fingerprint = judge_fingerprint()
    meta = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "sha": code["sha"][:12] + (" +dirty" if code["dirty"] else ""),
            "fingerprint": fingerprint, "mini": bool(args.calibration_mini),
            "tier": args.calibration_tier,
            "pairs": len(pairs), "pair_set": full, "reps": args.reps, "models": models,
            "evaluators": subset,
            "langs": {lang: sum(1 for p in pairs if p["lang"] == lang) for lang in ("zh", "en")}}
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    kind = ("injection-" if args.calibration_injection else
            "drift-" if args.calibration_mini else "calibration-")
    directory = args.report_dir or cache.root / "reports" / (kind + stamp)
    directory.mkdir(parents=True, exist_ok=True)
    if args.calibration_injection:
        result = calibration.injection_summary(rows)
        (directory / "injection.json").write_text(json.dumps(
            {"meta": meta, "result": result, "rows": rows}, ensure_ascii=False, indent=1,
            default=str), encoding="utf-8")
        md = calibration.injection_markdown(result, meta)
        (directory / "injection.md").write_text(md, encoding="utf-8")
        print(md)
        print(f"report: {directory / 'injection.md'}")
        return 0
    (directory / "calibration.json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1,
        default=str), encoding="utf-8")
    md = calibration.markdown(summary, meta)
    if args.compare_to:
        easy = json.loads(args.compare_to.read_text(encoding="utf-8"))["summary"]
        md += "\n".join(calibration.compare_tiers(easy, summary)) + "\n"
    (directory / "calibration.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"report: {directory / 'calibration.md'}")
    records = backend.parent / CALIBRATION_RECORDS
    if args.calibration_record:
        # the record keeps the summary only: the rows carry the judges' explanations
        records.mkdir(parents=True, exist_ok=True)
        name = fingerprint + ("-hard" if hard else "")
        (records / f"{name}.json").write_text(json.dumps(
            {"fingerprint": fingerprint, "meta": meta, "summary": summary},
            ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
        (records / f"{name}.md").write_text(md, encoding="utf-8")
        print(f"recorded: {records / (name + '.json')}")
    if args.calibration_mini:
        return report_drift(args, directory, summary, meta, calibration, records)
    return 0


def rerender_calibration(args: argparse.Namespace, backend: Path, cache: Any) -> int:
    """``--calibration-rerender``: the stored rows of a calibration run re-summarized at the
    product's pass bars (``calibration.rescore``) — the judge outputs are reused as they are,
    so the record keeps its fingerprint, pairs, reps and models."""
    import json

    from evalwb import calibration

    stored = json.loads(args.calibration_rerender.read_text(encoding="utf-8"))
    if not stored.get("rows"):
        print(f"{args.calibration_rerender}: no judge rows to re-render (a record keeps the "
              "summary only — re-run the calibration)")
        return 1
    rows = calibration.rescore(stored["rows"])
    summary = calibration.summarize(rows)
    code = code_identity(backend)
    meta = {**stored["meta"],
            "rendered_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "rendered_sha": code["sha"][:12] + (" +dirty" if code["dirty"] else ""),
            "bars": {e: calibration.product_bar(e) for e in sorted({r["evaluator"]
                                                                   for r in rows})}}
    meta.setdefault("tier", "easy")
    directory = args.report_dir or cache.root / "reports" / (
        "rerender-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "calibration.json").write_text(json.dumps(
        {"meta": meta, "summary": summary, "rows": rows}, ensure_ascii=False, indent=1,
        default=str), encoding="utf-8")
    md = calibration.markdown(summary, meta)
    if args.compare_to:
        easy = json.loads(args.compare_to.read_text(encoding="utf-8"))["summary"]
        md += "\n".join(calibration.compare_tiers(easy, summary)) + "\n"
    (directory / "calibration.md").write_text(md, encoding="utf-8")
    print(md)
    if args.calibration_record:
        records = backend.parent / CALIBRATION_RECORDS
        name = meta["fingerprint"] + ("-hard" if meta["tier"] == "hard" else "")
        (records / f"{name}.json").write_text(json.dumps(
            {"fingerprint": meta["fingerprint"], "meta": meta, "summary": summary},
            ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
        (records / f"{name}.md").write_text(md, encoding="utf-8")
        print(f"recorded: {records / (name + '.json')}")
    return 0


def run_self_preference(args: argparse.Namespace, backend: Path, cache: Any, cn: Any,
                        judge: Any, pairs: list[dict[str, Any]], fingerprint: str) -> int:
    """``--self-preference`` (R3-a): ``self_preference.{json,md}`` under the report dir."""
    import json

    from evalwb import calibration, self_preference

    models = [m for m in args.calibration_models.split(",") if m]
    judge.load_bedrock_key(cn)
    print(f"self-preference: {len(pairs)} questions × {len(models)} agents")
    answers = self_preference.answer_all(pairs, models, concurrency=args.concurrency)
    evaluators = tuple(e for e in (args.calibration_evaluators or "").split(",") if e) or \
        self_preference.EVALUATORS
    work = self_preference.work(pairs, answers, evaluators)
    print(f"self-preference: {len(work)} items × {len(models)} judges × {args.reps} reps")
    rows = calibration.judge(work, models, max(1, args.reps), concurrency=args.concurrency,
                             progress=print)
    rows = [{k: judge.scrub(v) for k, v in r.items()} for r in rows]
    result = self_preference.summarize(rows)
    code = code_identity(backend)
    meta = {"generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "sha": code["sha"][:12] + (" +dirty" if code["dirty"] else ""),
            "fingerprint": fingerprint, "questions": len(pairs), "agents": models,
            "models": models, "reps": args.reps}
    directory = args.report_dir or cache.root / "reports" / (
        "self-preference-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "self_preference.json").write_text(json.dumps(
        {"meta": meta, "result": result,
         "answers": [{"pair": p, "agent": a, "answer": judge.scrub(t)}
                     for (p, a), t in sorted(answers.items())],
         "rows": rows}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    md = self_preference.markdown(result, meta)
    (directory / "self_preference.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"report: {directory / 'self_preference.md'}")
    return 0


def report_drift(args: argparse.Namespace, directory: Path, summary: dict[str, Any],
                 meta: dict[str, Any], calibration: Any, records: Path) -> int:
    """R3-e: the mini-run against the recorded report → ``drift.{json,md}``; exit 1 on drift
    or when the current fingerprint has no record to compare with."""
    import json

    baseline_path = args.drift_against or records / f"{meta['fingerprint']}.json"
    if not baseline_path.is_file():
        print(f"DRIFT WATCH: no recorded calibration report at {baseline_path} — run "
              f"`make judge-calibration` for fingerprint {meta['fingerprint']}")
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    result = calibration.drift(summary, baseline["summary"])
    meta = {**meta, "baseline": str(baseline_path),
            "baseline_fingerprint": baseline.get("fingerprint")}
    (directory / "drift.json").write_text(json.dumps({"meta": meta, **result},
                                                     ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    md = calibration.drift_markdown(result, meta)
    (directory / "drift.md").write_text(md, encoding="utf-8")
    print(md)
    print(f"drift report: {directory / 'drift.md'}")
    return 1 if result["drift"] else 0


def _quiet() -> None:
    """CLI only (a test calling ``main`` keeps its process's logging and warnings)."""
    warnings.simplefilter("ignore")
    logging.basicConfig(level=logging.WARNING)
    for noisy in ("strands", "strands_evals", "botocore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


if __name__ == "__main__":
    _quiet()
    sys.exit(main())
