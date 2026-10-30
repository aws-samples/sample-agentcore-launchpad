"""``report.json`` / ``report.md`` and the one-line ledger row (pure: dicts in, text out).

The ledger row has the columns of the ledger in ``plans/EVAL-LOOP-ROUNDS.md``:
``| Round | Data | Methods | Findings | Fixed | M1 | M2 | M3 | M4 | M5 | M7 | M8 |`` — the
workbench fills Round, Data, Methods and M1–M4; the others stay for the lead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evalwb.judge import scrub

EXCERPT = 60


def _cell(text: Any, width: int = EXCERPT) -> str:
    raw = "∅" if text is None else str(text)
    raw = raw.replace("\n", " ").replace("|", "\\|").strip()
    return raw if len(raw) <= width else raw[:width - 1] + "…"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f} %"


def m1_cell(summary: dict[str, Any]) -> str:
    if not summary.get("denominator"):
        return "n/a"
    return f"{summary['matched']}/{summary['denominator']}"


def m1_table(sessions: list[dict[str, Any]]) -> str:
    lines = ["| session | turn | trace | status | prompt | tools (extracted / origin) "
             "| extracted answer | origin final answer |",
             "|---|---|---|---|---|---|---|---|"]
    for session in sessions:
        for row in session["rows"]:
            prompt = {True: "ok", False: "DIFF", None: "–"}[row["prompt_ok"]]
            tools = f"{len(row['extracted_tools'])} / {len(row['origin_tools'])}"
            if row["tools_ok"] is False:
                tools += " DIFF"
            lines.append(
                f"| {session['session_id'][:12]} | {row['turn']} | {row['trace_id'][:8]} "
                f"| {row['status']} | {prompt} | {tools} | {_cell(row['extracted_answer'])} "
                f"| {_cell(row['origin_answer'])} |")
        if session.get("error"):
            lines.append(f"| {session['session_id'][:12]} | – | – | ERROR | – | – "
                         f"| {_cell(session['error'], 120)} | |")
        elif not session.get("order_ok", True):
            lines.append(f"| {session['session_id'][:12]} | – | – | ORDER | – | – "
                         "| turns out of time order | |")
    return "\n".join(lines)


def corpus_sections(data: dict[str, Any]) -> list[str]:
    lines = ["### M1 per tag", "", "| dimension | value | M1 | sessions clean | failed turns "
             "(wrong) |", "|---|---|---|---|---|"]
    for dim, values in data["tags"].items():
        for value, s in values.items():
            lines.append(f"| {dim} | {value} | {m1_cell(s)} ({_pct(s['rate'])}) "
                         f"| {s['sessions_ok']}/{s['sessions']} | {s['failed_turns']} "
                         f"({s['failed_wrong']}) |")
    summary = data["error_summary"]
    lines += ["", "### Error turns (client saw an error frame) — N17-obs373", "",
              f"{summary['error_turns']} error turns; in the logs {summary['in_logs']}; "
              f"judged by the extraction {summary['judged']} (with a non-empty answer "
              f"{summary['judged_nonempty']}); kinds {summary['kinds']}", "",
              "| session | turn | error | in logs | status | judged | extracted answer |",
              "|---|---|---|---|---|---|---|"]
    for row in data["error_turns"]:
        lines.append(f"| {row['session_id'][:40]} | {row['turn']} | {_cell(row['error'], 70)} "
                     f"| {row['in_logs']} | {row['status']} | {row['judged']} "
                     f"| {_cell(row['extracted_answer'])} |")
    return lines + [""]


def _metric_cells(comparison: dict[str, dict[str, Any]], key: str) -> str:
    def short(evaluator: str) -> str:
        return evaluator.removeprefix("Builtin.")

    parts = []
    for evaluator, row in sorted(comparison.items()):
        if key == "label_rate" and row["labelled"]:
            parts.append(f"{short(evaluator)} {row['label_agree']}/{row['labelled']}")
        elif key == "mad" and row["mad"] is not None:
            parts.append(f"{short(evaluator)} {row['mad']:.2f}")
        elif key == "m4" and row.get("m4_turns"):
            parts.append(f"{short(evaluator)} {min(row['m4_scored'], row['m4_turns'])}"
                         f"/{row['m4_turns']}")
    return " · ".join(parts) or "n/a"


def ledger_row(report: dict[str, Any]) -> str:
    data = report["input"]
    what = (f"run {data['run']} ({len(data['sessions'])} sessions, {data['host']})"
            if data.get("run") else f"{len(data['sessions'])} sessions ({data.get('source')})")
    m1 = " · ".join(f"{m1_cell(v['summary'])} {mode}" for mode, v in report["m1"].items())
    vs_global = (report.get("comparisons") or {}).get("local_vs_global") or {}
    methods = f"eval-wb @{report['code']['sha'][:8]}{'+dirty' if report['code']['dirty'] else ''}"
    return (f"| {report.get('round') or '?'} | {what} | {methods} | | | {m1} "
            f"| {_metric_cells(vs_global, 'label_rate')} | {_metric_cells(vs_global, 'mad')} "
            f"| {_metric_cells(vs_global, 'm4')} | n/a | Strands | n/a |")


def _comparison_table(title: str, comparison: dict[str, dict[str, Any]],
                      variance: dict[str, dict[str, Any]] | None = None,
                      errors: dict[str, dict[str, int]] | None = None) -> list[str]:
    lines = [f"### {title}", "",
             "| evaluator | M2 labels agree | M3 mean abs diff (bias) | M4 scored/turns "
             "| matched/reference | turn-mean gap | row on another trace | reference errors "
             "| Global rerun: identical / mean std |", "|---|---|---|---|---|---|---|---|---|"]
    for evaluator, row in sorted(comparison.items()):
        var = (variance or {}).get(evaluator) or {}
        rerun = (f"{var['identical']}/{var['units']} / "
                 f"{'n/a' if var.get('mean_std') is None else format(var['mean_std'], '.3f')}"
                 if var.get("units") else "–")
        mad = ("n/a" if row["mad"] is None else f"{row['mad']:.3f} ({row['bias']:+.3f})")
        gap = "–" if row.get("turn_mean_gap") is None else f"{row['turn_mean_gap']:.3f}"
        m4 = (f"{min(row['m4_scored'], row['m4_turns'])}/{row['m4_turns']} ({_pct(row['m4'])})"
              if row.get("m4_turns") else "n/a")
        errs = ", ".join(f"{k} {v}" for k, v in ((errors or {}).get(evaluator) or {}).items())
        lines.append(
            f"| {evaluator} | {row['label_agree']}/{row['labelled']} ({_pct(row['label_rate'])}) "
            f"| {mad} | {m4} | {row['matched']}/{row['reference']} | {gap} "
            f"| {row.get('stamped_elsewhere', 0)} | {errs or '–'} | {rerun} |")
    return lines + [""]


def markdown(report: dict[str, Any]) -> str:
    data, code = report["input"], report["code"]
    source = f"run `{data['run']}` on {data['host']}" if data.get("run") else data.get("source")
    lines = [f"# Evaluation workbench report — {report['generated_at']}", "",
             f"- code: `{code['sha']}`{' (uncommitted changes)' if code['dirty'] else ''} "
             f"from `{code['backend']}`",
             f"- input: {source}, {len(data['sessions'])} sessions, log group "
             f"`{data['log_group']}`",
             f"- cache: `{data.get('cache')}`", ""]
    for mode, block in report["m1"].items():
        s = block["summary"]
        lines += [f"## M1 extraction vs origin logs — mode `{mode}`", "",
                  f"**M1 = {m1_cell(s)}** completed turns match ({_pct(s['rate'])}); "
                  f"failed turns {s['failed_turns']} (extracted wrongly {s['failed_wrong']}, leaks "
                  f"{s['leaks']}), extra turns "
                  f"{s['extra']}; sessions fully clean {s['sessions_ok']}/{s['sessions']}.", "",
                  m1_table(block["sessions"]), ""]
        if block.get("corpus"):
            lines += corpus_sections(block["corpus"])
    judge = report.get("judge")
    if judge:
        lines += ["## Local judges", "", f"- judge model: `{judge['model']}`",
                  f"- evaluators: {', '.join(judge['evaluators']) or '–'}"]
        for skip in judge.get("skipped") or []:
            lines.append(f"- skipped `{skip['id']}`: {skip['reason']}")
        lines += ["", "| evaluator | score (mean) | sessions |", "|---|---|---|"]
        lines += [f"| {s['evaluatorId']} | {s['score']} | {s['count']} |"
                  for s in judge.get("scores") or []]
        errors = [r for r in judge["records"] if r.get("error_type")]
        if errors:
            lines.append("")
            lines += [f"- error `{r['evaluator_id']}` {r['session_id'][:12]}: "
                      f"{r['error_type']} {_cell(r.get('error_message'), 160)}" for r in errors]
        lines.append("")
    glob = report.get("global")
    if glob:
        lines += ["## Global AgentCore Evaluate", "",
                  f"- source: {glob['source']}; reps: {glob['reps']}; records: "
                  f"{len(glob['records'])}; errored: "
                  f"{sum(1 for r in glob['records'] if r.get('error_type'))}"]
        for skip in glob.get("skipped") or []:
            lines.append(f"- skipped `{skip['id']}`: {skip['reason']}")
        own = glob.get("own_spans_sessions") or []
        if own:
            lines.append(f"- **A2A workaround** — {len(own)} sessions sent with their "
                         "own-service spans only (a called peer's spans dropped; NOT the SDK "
                         "payload): " + ", ".join(f"`{s}`" for s in own))
        lines.append("")
    comparisons = report.get("comparisons") or {}
    titles = {"local_vs_global": "M2–M4: local judges vs Global (rep 0)",
              "local_vs_global_own_spans": "M2–M4 on the A2A-workaround sessions only "
                                           "(Global sent own-service spans)",
              "local_vs_recorded": "Local judges vs the run's recorded results"}
    for key, title in titles.items():
        if comparisons.get(key):
            suffix = "_own_spans" if key.endswith("_own_spans") else ""
            vs_global = key.startswith("local_vs_global")
            lines += _comparison_table(
                title, comparisons[key],
                comparisons.get("global_rerun" + suffix) if vs_global else None,
                (glob or {}).get("errors" + suffix) if vs_global else None)
    lines += ["## Ledger row (plans/EVAL-LOOP-ROUNDS.md)", "", report["ledger_row"], ""]
    return "\n".join(lines)


def _scrubbed(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrubbed(v) for v in value]
    return scrub(value)


def write(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    clean = _scrubbed(report)
    md, js = directory / "report.md", directory / "report.json"
    md.write_text(markdown(clean), encoding="utf-8")
    js.write_text(json.dumps(clean, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return md, js
