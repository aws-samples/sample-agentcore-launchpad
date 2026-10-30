#!/usr/bin/env python3
"""Quality metrics board — aggregates the CN quality evidence into one page.

Sources (all read-only; every one is optional — a missing file yields an empty section):

  repo   docs/cn/evidence/round-*/**/REPORT.md  (+ sibling json/run-summary.json, progress.md)
  repo   tests/e2e/cn/QUARANTINE.md
  repo   docs/cn/reports/bugs-round-*.md
  repo   docs/cn/reports/security-scan-*.md        (latest by file-name date)
  repo   docs/cn/reports/mutation-baseline-*.md    (latest by file-name date; round-16 mutation score)
  extern deploy-ops ledger (default /home/ubuntu/workspace/launchpad-cn/deploy-ops-ledger.md)
  extern verify gate logs  (default /tmp/verify-*.log)

The registers also feed the "Discovery" section (round-16 bug-discovery plan §0 KPIs: findings per
class, severity-weighted score, escaped findings, channel A/B capture–recapture, exploratory yield)
through optional row markers — see the constants under "round-16 discovery markers" below and
docs/cn/interfaces/testing.md § "Mutation testing & discovery KPIs".

Outputs:

  docs/cn/reports/quality-metrics.md            tables + a 12-line summary with trend arrows
  docs/cn/reports/quality-metrics-history.jsonl one snapshot per day (re-running on the same
                                                day replaces that day's line — idempotent)

Rules: stdlib only; deterministic for a given input tree and --date; never prints secrets
(every free-text fragment that reaches the outputs goes through redact()); the classification
vocabulary (ENV / TEST / PRODUCT) is the one in tests/e2e/README.md § "Failure classification".
Heuristics are documented in docs/cn/interfaces/testing.md § "Quality metrics board".
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import Counter, OrderedDict

DEFAULT_OPS_LEDGER = "/home/ubuntu/workspace/launchpad-cn/deploy-ops-ledger.md"
DEFAULT_VERIFY_GLOB = "/tmp/verify-*.log"
REPORT_REL = "docs/cn/reports/quality-metrics.md"
HISTORY_REL = "docs/cn/reports/quality-metrics-history.jsonl"

# --------------------------------------------------------------------------- redaction (C4)

_REDACT_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ASIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(aws_)?secret(_access)?_key\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|passwd|pwd|bearer|authorization)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential. Applied to every free-text fragment emitted."""
    if not text:
        return text
    out = text
    for pat in _REDACT_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# --------------------------------------------------------------------------- helpers

def read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def md_rows(text: str):
    """Yield (header_cells or None, cells) for every table row of a markdown text.

    The header is the most recent row followed by a |---| separator; cells are stripped.
    """
    lines = text.splitlines()
    header = None
    for i, line in enumerate(lines):
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{3,}:?", c) for c in cells if c) and any(cells):
            continue  # separator
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if nxt.startswith("|") and re.fullmatch(r"\|(\s*:?-{3,}:?\s*\|)+\s*", nxt.strip()):
            header = [c.lower() for c in cells]
            continue
        yield header, cells


def col(header, cells, *names):
    """Cell under the first header column whose name contains one of *names* (else None)."""
    if not header:
        return None
    for idx, name in enumerate(header):
        for want in names:
            if want in name and idx < len(cells):
                return cells[idx]
    return None


def strip_md(s: str) -> str:
    return re.sub(r"[*`_]", "", s or "").strip()


def code_span(s: str) -> str:
    """The content of a markdown code span / emphasised cell with `_` KEPT (`chat_session` is a
    name, not emphasis) — strips only backticks and `*` (rev-r16 on r16-o: `strip_md` dropped the
    underscores of five graph names)."""
    return re.sub(r"[*`]", "", s or "").strip()


def parse_date(s: str) -> dt.date | None:
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def fmt_min(seconds):
    if seconds is None:
        return "—"
    return f"{seconds / 60:.1f} min"


def pct(n, d):
    return f"{(100.0 * n / d):.1f} %" if d else "—"


# --------------------------------------------------------------------------- storyline runs

ENV_RE = re.compile(
    r"internalServerException|ServiceUnavailable|Throttling|Try your request again|"
    r"too many requests|read timeout|ReadTimeout|HTTP 50[234]|\b50[234]\b|upstream transient|"
    r"host update in progress|host-update window|tunnel|DNS",
    re.I,
)
TEST_RE = re.compile(
    r"scenario state .{0,40}missing|test-side|strictness|runner planning error|counting nit|"
    r"blank screenshot|selection error|too-strict|stale assertion|missing wait|planning error",
    re.I,
)
PRODUCT_RE = re.compile(r"product bug|register row|\bN\d+[a-z0-9]*-bug\d+|PRODUCT", re.I)
EXPLICIT_CLASS_RE = re.compile(r"\*\*(ENV|TEST|PRODUCT)\*\*")
STEP_RE = re.compile(r"\bs\d{2}[a-z]?\b")


def classify_failure(context: str) -> str:
    """ENV / TEST / PRODUCT / UNCLASSIFIED from the lines of a REPORT that mention the step.

    Order follows the README: an explicit **CLASS** tag wins; else the raw error text decides
    (ENV set from `UPSTREAM_TRANSIENT_RE`), then TEST markers, then PRODUCT markers.
    """
    m = EXPLICIT_CLASS_RE.search(context)
    if m:
        return m.group(1)
    if ENV_RE.search(context):
        return "ENV"
    if TEST_RE.search(context):
        return "TEST"
    if PRODUCT_RE.search(context):
        return "PRODUCT"
    return "UNCLASSIFIED"


def _run_summary(report_dir: str):
    path = os.path.join(report_dir, "json", "run-summary.json")
    text = read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _duration_from_header(head: str):
    m = re.search(r"(\d+)\s*h\s*(\d+)\s*min", head)
    if m:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60
    m = re.search(r"\((\d+(?:\.\d+)?)\s*min\)", head)
    if m:
        return int(float(m.group(1)) * 60)
    return None


def parse_report(path: str, repo: str) -> dict:
    text = read_text(path) or ""
    lines = text.splitlines()
    head = "\n".join(lines[:15])
    rel = os.path.relpath(path, repo)
    run_dir = os.path.dirname(path)
    run_id = os.path.relpath(run_dir, os.path.join(repo, "docs", "cn", "evidence"))

    date = parse_date(head) or parse_date(text)
    host = None
    m = re.search(r"\bhost\s+\**([0-9a-f]{7,8})\b", head) or re.search(r"\bhost\s+\**([0-9a-f]{7,8})\b", text)
    if m:
        host = m.group(1)
    if host is None:
        prog = read_text(os.path.join(run_dir, "progress.md")) or ""
        m = re.search(r"\bhost\s+\**([0-9a-f]{7,8})\b", prog)
        host = m.group(1) if m else None

    summary = _run_summary(run_dir)
    if summary and not any(t.get("outcome") in ("expected", "unexpected", "flaky") for t in summary):
        summary = None  # a stub summary (every test skipped) carries no verdict — fall back to the REPORT text
    passed = failed = skipped = None
    failing_steps: set[str] = set()
    source = "regex"
    context_from = 0  # first line whose text may describe the counted attempt (repair-run REPORTs list several)
    counts_re = re.compile(r"(\d+)\s+passed\s*/\s*(\d+)\s+failed(?:\s*/\s*(\d+)\s+skipped)?(?:[^()\n]{0,30}\(([^)]*)\))?")
    if summary:
        source = "run-summary.json"
        passed = sum(1 for t in summary if t.get("outcome") in ("expected", "flaky"))
        failed = sum(1 for t in summary if t.get("outcome") == "unexpected")
        skipped = sum(1 for t in summary if t.get("outcome") == "skipped")
        failing_steps = {str(t.get("step") or t.get("title") or "?") for t in summary if t.get("outcome") == "unexpected"}
    else:
        matches = list(counts_re.finditer(head)) or list(counts_re.finditer(text))
        if matches:
            m = matches[-1]  # the last match of the header (else of the text) = the latest attempt
            context_from = text[: m.start()].count("\n")
            passed, failed = int(m.group(1)), int(m.group(2))
            skipped = int(m.group(3)) if m.group(3) else 0
            if m.group(4):
                failing_steps.update(STEP_RE.findall(m.group(4)))
        for hdr, cells in md_rows(text):
            if len(cells) >= 2 and re.fullmatch(r"\**FAIL\**(?:\s*\(.*\))?", cells[1]) and STEP_RE.search(strip_md(cells[0])):
                failing_steps.add(STEP_RE.search(strip_md(cells[0])).group(0))
        failing_steps.update(re.findall(r"\*\*(s\d{2}[a-z]?)\s+FAIL\b", text))
        failing_steps.update(re.findall(r"`(s\d{2}[a-z]?)-FAILED-", text))  # Playwright failure shots named by the runner
    failing_steps = sorted(failing_steps)

    duration_s = _duration_from_header(head)
    if duration_s is None and summary:
        duration_s = int(sum(float(t.get("duration_s") or 0) for t in summary))

    verdict = None
    m = re.search(r"\*\*Result:.*?→\s*\**(PASS|FAIL|PARTIAL)", head) or re.search(r"\b(FAIL|PASS|PARTIAL) by (?:plan )?§6", text)
    if m:
        verdict = m.group(1)
    elif failed == 0 and passed:
        verdict = "PASS"

    failures = []
    classes: dict[str, str] = {}
    contexts: dict[str, str] = {}
    for step in failing_steps:
        step_re = re.compile(r"\b" + re.escape(step) + r"\b")
        ctx = "\n".join(ln for ln in lines[context_from:] if step_re.search(ln))
        if not ctx:
            ctx = "\n".join(ln for ln in lines if step_re.search(ln))
        if source == "regex" and context_from < len(lines):
            ctx = lines[context_from] + "\n" + ctx  # the counted attempt's own line names the failure reason
        contexts[step] = ctx
        classes[step] = classify_failure(ctx)
    for step in failing_steps:
        ctx = contexts[step]
        spec = None
        sm = re.search(r"`?([a-z0-9-]+\.spec\.ts)(?::\d+)?`?", ctx)
        if sm:
            spec = sm.group(1)
        cls = classes[step]
        cm = re.search(r"cascade (?:of|from) (s\d{2}[a-z]?)", ctx)
        note = None
        if cm and cm.group(1) in classes and cm.group(1) != step:
            cls = classes[cm.group(1)]  # a cascade failure inherits the class of the step that caused it
            note = f"cascade of {cm.group(1)}"
        failures.append({"step": step, "spec": spec, "class": cls, "note": note})

    by_class = Counter(f["class"] for f in failures)
    return {
        "run": run_id,
        "report": rel,
        "date": date.isoformat() if date else None,
        "host_sha": host,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "duration_s": duration_s,
        "verdict": verdict,
        "counts_source": source,
        "failures": failures,
        "failures_by_class": {k: by_class.get(k, 0) for k in ("ENV", "TEST", "PRODUCT", "UNCLASSIFIED")},
    }


def collect_runs(repo: str) -> list[dict]:
    paths = sorted(glob.glob(os.path.join(repo, "docs", "cn", "evidence", "round-*", "**", "REPORT.md"), recursive=True))
    runs = [parse_report(p, repo) for p in paths]
    runs.sort(key=lambda r: (r["date"] or "", r["run"]))
    return runs


# --------------------------------------------------------------------------- quarantine

def collect_quarantine(repo: str, today: dt.date) -> dict:
    text = read_text(os.path.join(repo, "tests", "e2e", "cn", "QUARANTINE.md"))
    entries = []
    if text:
        for hdr, cells in md_rows(text):
            if not hdr or len(cells) < 3 or "spec" not in hdr[0]:
                continue
            first = parse_date(col(hdr, cells, "first") or "")
            last = parse_date(col(hdr, cells, "last") or "")
            state = strip_md(col(hdr, cells, "state") or "")
            spec = strip_md(cells[0])
            sm = re.search(r"([a-z0-9-]+\.spec\.ts)", spec)
            entries.append(
                {
                    "spec": redact(sm.group(1) if sm else spec[:60]),
                    "ids": redact(strip_md(col(hdr, cells, "id") or "")[:120]),
                    "first": first.isoformat() if first else None,
                    "last": last.isoformat() if last else None,
                    "age_days": (today - first).days if first else None,
                    "exit_criterion": redact(strip_md(col(hdr, cells, "exit") or "")[:240]),
                    "state": redact(state[:60]),
                    "active": "quarantin" in state.lower() and "exit" not in state.lower() and "released" not in state.lower(),
                }
            )
    active = [e for e in entries if e["active"]]
    ages = [e["age_days"] for e in active if e["age_days"] is not None]
    return {"count": len(active), "total_rows": len(entries), "max_age_days": max(ages) if ages else 0, "entries": entries}


# --------------------------------------------------------------------------- registers

ID_RE = re.compile(r"^\**((?:[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*?-(?:obs|bug)\d+[a-z]?)|WB-\d{3}(?:-obs\d+)?)\b")
SEV_RE = re.compile(r"\b(Critical|High|Medium|Moderate|Low|Info|P[0-3])\b", re.I)  # round-14/15 cells spell `medium (PRODUCT)`
SEV_MAP = {"P0": "Critical", "P1": "High", "P2": "Medium", "P3": "Low", "Moderate": "Medium"}
SEV_ORDER = ["Critical", "High", "Medium", "Low", "Info", "unknown"]
FIXED_RE = re.compile(r"(?<!not )(?<!un)\b(fixed|resolved|closed)\b", re.I)  # "not fixed" / "unfixed" are pending
LIVE_PENDING_RE = re.compile(r"live re-?test pending|pending live|live confirmation pending|pending (?:a )?live|not yet (?:re-)?tested live", re.I)
PENDING_RE = re.compile(r"\b(open|pending|scheduled|deferred|candidate|recorded|todo|not fixed|unfixed|blocked)\b", re.I)
USER_RE = re.compile(r"user[- ]reported|user finding|reported by the user|found by the user|the user (?:saw|hit|reported|found|noticed)|\(user\)", re.I)

# --- round-16 discovery markers (BUG-DISCOVERY-PLAN.md §0 / §3; docs/cn/interfaces/testing.md
# § "Mutation testing & discovery KPIs"). All optional, anywhere in a register row, usually in the id
# cell: `class: PRODUCT|TEST|ENV` · `channel: A|B|C` (several: `A+B`) · `repro-hash: <8 hex>` (two ids
# with the same hash are one defect seen by both channels) · `found-by: user` (an ESCAPED finding) ·
# `session: <id>/<hours>h` (the exploratory session a channel-B row came from) · `module: Enn` ·
# `kind: process|ruling` (a bookkeeping row — excluded from the discovery counts).
CLASS_MARK_RE = re.compile(r"class:\s*(PRODUCT|TEST|ENV)\b")
CLASS_WORD_RE = re.compile(r"\b(PRODUCT|TEST|ENV)\b")
CHANNEL_RE = re.compile(r"channel:\s*([ABC](?:\s*[+,/&]\s*[ABC])*)")
REPRO_HASH_RE = re.compile(r"repro-hash:\s*([0-9a-f]{8})\b")
SESSION_RE = re.compile(r"session:\s*([A-Za-z0-9._-]+)\s*/\s*(\d+(?:\.\d+)?)\s*h\b")
MODULE_RE = re.compile(r"module:\s*(E\d{2})\b")
KIND_RE = re.compile(r"kind:\s*(process|ruling)\b")
# Escaped = somebody outside the round's channels found it first. `USER` as a word (not `USER
# DECISION`, the round-5 ruling rows) and 逃逸 are the lead's spellings; `found-by: user` the marker.
ESCAPED_RE = re.compile(r"found-by:\s*user|user[- ]report(?:ed)?|逃逸", re.I)
USER_WORD_RE = re.compile(r"\bUSER\b(?!\s+DECISION)")
SEV_WEIGHT = {"Critical": 5, "High": 5, "Medium": 3, "Low": 1}  # P1=5 P2=3 P3=1; Critical weighs as P1
# A register row the GRAPH + LOOP layer opened (testing.md §6.7 / §6.8): the graph executor, a random
# walk, a neighbourhood path or a cross-graph walk named in the row. Feeds the stop rule's
# "consecutive rounds with zero new graph findings".
GRAPH_ROW_RE = re.compile(r"lifecycle graph|random walks?|cross-graph|neighbourhood|graph `|REFERENCE graph", re.I)
# BUG-DISCOVERY-PLAN.md §4 — the four deep-dive paths and the plan-§1 modules each one covers; the
# capture–recapture estimate is computed per path over the rows carrying a `module:` marker.
DEEP_DIVE_PATHS = {
    "create-deploy-invoke (E03/E04)": {"E03", "E04"},
    "evaluation (E09–E12/E14)": {"E09", "E10", "E11", "E12", "E14"},
    "assistant + canary (E02/E13)": {"E02", "E13"},
    "auth / isolation / ownership (E18/E07/E16/E19)": {"E18", "E07", "E16", "E19"},
}


def repro_hash(text: str) -> str:
    """The `repro-hash:` convention: sha1 of the lower-cased, whitespace-collapsed minimal repro, 8 hex."""
    import hashlib

    norm = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:8]


def capture_recapture(ids: dict) -> dict:
    """Chapman's estimator over channels A and B of the given rows: ``{n_a, n_b, overlap,
    estimate_total, estimate_remaining}`` (the estimates None until both channels are present)."""
    a_ids = {rid for rid, r in ids.items() if "A" in r["channels"]}
    b_ids = {rid for rid, r in ids.items() if "B" in r["channels"]}
    b_hashes = {r["repro_hash"] for rid, r in ids.items() if rid in b_ids and r["repro_hash"]}
    overlap = sum(
        1 for rid in a_ids if rid in b_ids or (ids[rid]["repro_hash"] and ids[rid]["repro_hash"] in b_hashes and any(
            o != rid and ids[o]["repro_hash"] == ids[rid]["repro_hash"] for o in b_ids))
    )
    n_a, n_b = len(a_ids), len(b_ids)
    estimate = remaining = None
    if n_a and n_b:
        estimate = (n_a + 1) * (n_b + 1) / (overlap + 1) - 1
        remaining = max(estimate - (n_a + n_b - overlap), 0.0)
    return {
        "n_a": n_a, "n_b": n_b, "overlap": overlap,
        "estimate_total": round(estimate, 1) if estimate is not None else None,
        "estimate_remaining": round(remaining, 1) if remaining is not None else None,
    }


def discovery_paths(ids: dict) -> dict:
    """Capture–recapture per deep-dive path (plan §4): the rows whose `module:` marker falls in the
    path's module set; `findings` counts them, the estimate stays None until both channels tag rows."""
    out = {}
    for path, modules in DEEP_DIVE_PATHS.items():
        subset = {rid: r for rid, r in ids.items() if r.get("module") in modules}
        out[path] = {"findings": len(subset), **capture_recapture(subset)}
    return out


def discovery_for_round(rows: dict) -> dict:
    """Round-level discovery KPIs from the per-id rows of one register (see collect_registers)."""
    ids = {rid: r for rid, r in rows.items() if not r.get("kind")}
    by_class = Counter(r["class"] for r in ids.values())
    weighted = sum(SEV_WEIGHT.get(r["severity"], 0) for r in ids.values())
    escaped = sum(1 for r in ids.values() if r["user_reported"])
    chan = Counter()
    for r in ids.values():
        if r["channels"]:
            chan.update(r["channels"])
        else:
            chan["untagged"] += 1
    a_ids = {rid for rid, r in ids.items() if "A" in r["channels"]}
    b_ids = {rid for rid, r in ids.items() if "B" in r["channels"]}
    b_hashes = {r["repro_hash"] for rid, r in ids.items() if rid in b_ids and r["repro_hash"]}
    overlap = sum(
        1 for rid in a_ids if rid in b_ids or (ids[rid]["repro_hash"] and ids[rid]["repro_hash"] in b_hashes and any(
            o != rid and ids[o]["repro_hash"] == ids[rid]["repro_hash"] for o in b_ids))
    )
    n_a, n_b = len(a_ids), len(b_ids)
    estimate = remaining = None
    if n_a and n_b:
        estimate = (n_a + 1) * (n_b + 1) / (overlap + 1) - 1  # Chapman's capture–recapture estimator
        remaining = max(estimate - (n_a + n_b - overlap), 0.0)
    sessions: dict[str, float] = {}
    for r in ids.values():
        for sid, hours in r["sessions"]:
            sessions[sid] = max(sessions.get(sid, 0.0), hours)
    hours = sum(sessions.values())
    return {
        "findings": len(ids),
        "by_class": {k: by_class.get(k, 0) for k in ("PRODUCT", "TEST", "ENV", "unclassified")},
        "weighted_score": weighted,
        "escaped": escaped,
        "escape_ratio": (escaped / len(ids)) if ids else None,
        "channels": {k: chan.get(k, 0) for k in ("A", "B", "C", "untagged")},
        "overlap_ab": overlap,
        "estimate_total": round(estimate, 1) if estimate is not None else None,
        "estimate_remaining": round(remaining, 1) if remaining is not None else None,
        "session_hours": hours,
        "exploratory_yield": (n_b / hours) if hours else None,
        "by_module": dict(sorted(Counter(r["module"] for r in ids.values() if r["module"]).items())),
        "graph_findings": sum(1 for r in ids.values() if r.get("graph_row")),
        "paths": discovery_paths(ids),
    }


def classify_state(cell: str | None, row_text: str, header, cells=()) -> tuple[str, bool]:
    """Return (state, live_retest_pending). State ∈ fixed / pending / recorded / n-a.

    `recorded` = an observation row with neither a state cell nor a state keyword (older registers).
    """
    live = bool(LIVE_PENDING_RE.search(row_text))
    if cell is not None:  # an explicit state / status cell decides
        if FIXED_RE.search(cell):
            return "fixed", live
        if PENDING_RE.search(cell):
            return "pending", live
        if strip_md(cell) in ("—", "-", "n/a", ""):
            return "n-a", live
    fix_cell = col(header, cells, "fix", "commit") if header else None
    if fix_cell and strip_md(fix_cell) not in ("—", "-", "n/a", ""):
        return "fixed", live  # a non-empty fix / commit cell is a fix row
    if cell is None:
        if FIXED_RE.search(row_text):
            return "fixed", live
        if PENDING_RE.search(row_text):
            return "pending", live
    return "recorded", live


def round_label(fname: str) -> tuple[tuple[int, str], str]:
    m = re.search(r"bugs-round-(\d+)(?:-([a-z0-9]+))?\.md$", fname)
    if not m:
        return (999, fname), fname
    num, suffix = int(m.group(1)), (m.group(2) or "")
    return (num, suffix), f"round {num}" + (f" ({suffix})" if suffix else "")


def collect_registers(repo: str) -> dict:
    files = sorted(glob.glob(os.path.join(repo, "docs", "cn", "reports", "bugs-round-*.md")), key=lambda p: round_label(os.path.basename(p))[0])
    rounds = []
    totals = Counter()
    sev_total = Counter()
    for path in files:
        text = read_text(path) or ""
        key, label = round_label(os.path.basename(path))
        rows: "OrderedDict[str, dict]" = OrderedDict()
        for hdr, cells in md_rows(text):
            if not cells:
                continue
            m = ID_RE.match(strip_md(cells[0]))
            if not m:
                continue
            rid = m.group(1)
            row_text = " | ".join(cells)
            sev_cell = col(hdr, cells, "severity", "sev")
            sev = None
            sm = SEV_RE.search(strip_md(sev_cell)) if sev_cell else None
            if not sm and sev_cell is None:
                sm = SEV_RE.search(strip_md(row_text))
            if sm:
                tok = sm.group(1).upper() if sm.group(1)[0] in "pP" else sm.group(1).capitalize()
                sev = SEV_MAP.get(tok, tok)
            state_cell = col(hdr, cells, "state", "status")
            state, live = classify_state(state_cell, row_text, hdr, cells)
            user = rid.startswith("WB-") or bool(USER_RE.search(row_text)) or bool(ESCAPED_RE.search(row_text)) or bool(USER_WORD_RE.search(row_text))
            cm = CLASS_MARK_RE.search(row_text) or CLASS_WORD_RE.search(row_text)
            klass = cm.group(1) if cm else None
            chm = CHANNEL_RE.search(row_text)
            channels = set(re.findall(r"[ABC]", chm.group(1))) if chm else set()
            rh = REPRO_HASH_RE.search(row_text)
            mm = MODULE_RE.search(row_text)
            km = KIND_RE.search(row_text)
            sessions = [(sid, float(h)) for sid, h in SESSION_RE.findall(row_text)]
            graph_row = bool(GRAPH_ROW_RE.search(" | ".join(cells[:2])))  # the id + subsystem cells
            prev = rows.get(rid)
            if prev is None:
                rows[rid] = {
                    "id": rid, "severity": sev or "unknown", "state": state, "live_retest_pending": live, "user_reported": user, "rows": 1,
                    "class": klass or "unclassified", "channels": channels, "repro_hash": rh.group(1) if rh else None,
                    "module": mm.group(1) if mm else None, "kind": km.group(1) if km else None, "sessions": sessions,
                    "graph_row": graph_row,
                }
            else:
                prev["rows"] += 1
                if prev["severity"] == "unknown" and sev:
                    prev["severity"] = sev
                if state not in ("recorded", "n-a"):  # later rows are state updates
                    prev["state"] = state
                prev["live_retest_pending"] = prev["live_retest_pending"] or live
                prev["user_reported"] = prev["user_reported"] or user
                if klass and (prev["class"] == "unclassified" or CLASS_MARK_RE.search(row_text)):
                    prev["class"] = klass
                prev["channels"] |= channels
                prev["repro_hash"] = prev["repro_hash"] or (rh.group(1) if rh else None)
                prev["module"] = prev["module"] or (mm.group(1) if mm else None)
                prev["kind"] = prev["kind"] or (km.group(1) if km else None)
                prev["sessions"] += sessions
                prev["graph_row"] = prev["graph_row"] or graph_row
        c = Counter(r["state"] for r in rows.values())
        sev_c = Counter(r["severity"] for r in rows.values())
        user_n = sum(1 for r in rows.values() if r["user_reported"])
        entry = {
            "round": label,
            "file": os.path.relpath(path, repo),
            "ids": len(rows),
            "fixed": c.get("fixed", 0),
            "pending": c.get("pending", 0),
            "n_a": c.get("n-a", 0),
            "recorded": c.get("recorded", 0),
            "live_retest_pending": sum(1 for r in rows.values() if r["live_retest_pending"]),
            "user_reported": user_n,
            "internal": len(rows) - user_n,
            "by_severity": {s: sev_c.get(s, 0) for s in SEV_ORDER},
            "discovery": discovery_for_round(rows),
        }
        rounds.append(entry)
        totals.update({k: entry[k] for k in ("ids", "fixed", "pending", "n_a", "recorded", "live_retest_pending", "user_reported", "internal")})
        sev_total.update(sev_c)
    ids = totals.get("ids", 0)
    return {
        "rounds": rounds,
        "totals": {k: totals.get(k, 0) for k in ("ids", "fixed", "pending", "n_a", "recorded", "live_retest_pending", "user_reported", "internal")},
        "by_severity": {s: sev_total.get(s, 0) for s in SEV_ORDER},
        "defect_escape_ratio": (totals.get("user_reported", 0) / ids) if ids else None,
    }


# --------------------------------------------------------------------------- security

def collect_security(repo: str) -> dict:
    files = sorted(glob.glob(os.path.join(repo, "docs", "cn", "reports", "security-scan-*.md")))
    if not files:
        return {"report": None, "date": None, "counts": {}, "surfaces": []}
    path = files[-1]
    text = read_text(path) or ""
    counts = Counter()
    surfaces = []
    for hdr, cells in md_rows(text):
        if not hdr or not any("critical" in h for h in hdr) or not any("high" in h for h in hdr):
            continue
        row = {"surface": redact(strip_md(cells[0])[:80])}
        for idx, name in enumerate(hdr):
            if idx == 0 or idx >= len(cells):
                continue
            key = None
            if "critical" in name:
                key = "Critical"
            elif "high" in name:
                key = "High"
            elif "moderate" in name or "medium" in name:
                key = "Medium"
            elif name.strip() == "low" or name.startswith("low"):
                key = "Low"
            elif "info" in name:
                key = "Info"
            if key:
                m = re.search(r"\d+", cells[idx])
                val = int(m.group(0)) if m else 0
                row[key] = val
                counts[key] += val
        surfaces.append(row)
    date = parse_date(os.path.basename(path))
    return {
        "report": os.path.relpath(path, repo),
        "date": date.isoformat() if date else None,
        "counts": {k: counts.get(k, 0) for k in ("Critical", "High", "Medium", "Low", "Info")},
        "surfaces": surfaces,
    }


# --------------------------------------------------------------------------- mutation baseline

def _pct_cell(cell) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", cell or "")
    return float(m.group(1)) if m else None


def _int_cell(cell) -> int:
    m = re.search(r"\d+", cell or "")
    return int(m.group(0)) if m else 0


def collect_mutation(repo: str) -> dict:
    """Latest docs/cn/reports/mutation-baseline-*.md: per-side (backend / frontend) file rows + totals.

    A side is the `## …` section whose heading names it (backend / mutmut, frontend / Stryker); a
    row is any table with `file` and `score` columns. Score = killed / (killed + survived + timeout +
    uncovered) in percent — "no tests" / "no coverage" mutants count as survived (a behaviour no test
    reaches is untested); `score (covered)` leaves them out.
    """
    files = sorted(glob.glob(os.path.join(repo, "docs", "cn", "reports", "mutation-baseline-*.md")))
    out = {"report": None, "date": None, "sides": {}}
    if not files:
        return out
    path = files[-1]
    text = read_text(path) or ""
    out["report"] = os.path.relpath(path, repo)
    date = parse_date(os.path.basename(path))
    out["date"] = date.isoformat() if date else None
    sections = re.split(r"^(?=## )", text, flags=re.M)
    for sec in sections:
        head = sec.splitlines()[0].lower() if sec.strip() else ""
        side = "backend" if ("backend" in head or "mutmut" in head) else "frontend" if ("frontend" in head or "stryker" in head) else None
        if side is None:
            continue
        rows = []
        total = None
        for hdr, cells in md_rows(sec):
            if not hdr or "file" not in hdr[0] or not any("score" in h for h in hdr) or len(cells) < 3:
                continue
            row = {
                "file": redact(strip_md(cells[0])[:80]),
                "mutants": _int_cell(col(hdr, cells, "mutants", "total")),
                "killed": _int_cell(col(hdr, cells, "killed")),
                "survived": _int_cell(col(hdr, cells, "survived")),
                "timeout": _int_cell(col(hdr, cells, "timeout")),
                "uncovered": _int_cell(col(hdr, cells, "no tests", "no cov", "uncovered")),
                "score": _pct_cell(col(hdr, cells, "score")),
                "score_covered": _pct_cell(col(hdr, cells, "covered")),
            }
            if strip_md(cells[0]).lower() == "total":
                total = row
            else:
                rows.append(row)
        if not rows and total is None:
            continue
        if total is None:
            total = {k: sum(r[k] for r in rows) for k in ("mutants", "killed", "survived", "timeout", "uncovered")}
            denom = total["killed"] + total["survived"] + total["timeout"] + total["uncovered"]
            total["score"] = round(100.0 * total["killed"] / denom, 1) if denom else None
            cov = denom - total["uncovered"]
            total["score_covered"] = round(100.0 * total["killed"] / cov, 1) if cov else None
            total["file"] = "total"
        out["sides"][side] = {"files": rows, "total": total}
    return out


# --------------------------------------------------------------------------- lifecycle graphs (r16-o)


def collect_graph(repo: str) -> dict:
    """docs/cn/reports/graph-coverage.md (`make graph-coverage`, round 16 r16-o): per lifecycle graph the
    edges / prime paths / edge pairs and how many the hermetic executor walked; the `**total**` row carries
    the board's numbers. Missing report → every value None (the board shows "—")."""
    path = os.path.join(repo, "docs", "cn", "reports", "graph-coverage.md")
    out = {"report": None, "graphs": [], "total": None, "loop": {"graphs": [], "total": None},
           "rounds": []}
    text = read_text(path)
    if text is None:
        return out
    out["report"] = os.path.relpath(path, repo)
    for m in LOOP_ROUND_RE.finditer(text):
        ids = [i.strip() for i in (m.group("ids") or "").split(",") if i.strip()]
        out["rounds"].append({"round": int(m.group("n")), "method": m.group("method").strip(),
                              "findings": int(m.group("k")), "ids": ids,
                              "streak": int(m.group("streak"))})
    out["rounds"].sort(key=lambda r: r["round"])
    for hdr, cells in md_rows(text):
        if hdr and "graph" in hdr[0] and any(h.strip() == "walks" for h in hdr) and len(cells) >= 5:
            # the round-17 loop-engine table (testing.md §6.8): walks / findings / neighbourhood / cross
            row = {
                "graph": code_span(cells[0]),
                "walks": _int_cell(col(hdr, cells, "walks")),
                "walk_findings": _int_cell(col(hdr, cells, "walk findings")),
                "neighbourhood_paths": _int_cell(col(hdr, cells, "neighbourhood paths")),
                "neighbourhood_new": _int_cell(col(hdr, cells, "newly covered")),
                "cross_walks": _int_cell(col(hdr, cells, "cross-graph walks")),
                "cross_findings": _int_cell(col(hdr, cells, "cross findings")),
                # round 6 (r17-l): the fault-injected walks (testing.md §6.9) — absent on an
                # older report, so the keys read None there
                "fault_walks": _int_cell(col(hdr, cells, "fault walks")),
                "fault_findings": _int_cell(col(hdr, cells, "fault findings")),
            }
            if row["graph"] == "total":
                out["loop"]["total"] = row
            else:
                out["loop"]["graphs"].append(row)
            continue
        if not hdr or "graph" not in hdr[0] or not any("prime" in h for h in hdr) or len(cells) < 6:
            continue
        row = {
            "graph": code_span(cells[0]),
            "edges": _int_cell(col(hdr, cells, "edges")),
            "edges_covered": _int_cell(col(hdr, cells, "edges covered")),
            "prime_paths": _int_cell(col(hdr, cells, "prime paths")),
            "prime_paths_covered": _int_cell(col(hdr, cells, "prime paths covered")),
            "edge_pairs": _int_cell(col(hdr, cells, "edge pairs")),
            "edge_pairs_covered": _int_cell(col(hdr, cells, "pairs covered")),
            "gaps": _int_cell(col(hdr, cells, "gaps")),
        }
        if row["graph"] == "total":
            out["total"] = row
        else:
            out["graphs"].append(row)
    if out["total"] is None and out["graphs"]:
        out["total"] = {k: sum(g[k] for g in out["graphs"]) for k in
                        ("edges", "edges_covered", "prime_paths", "prime_paths_covered",
                         "edge_pairs", "edge_pairs_covered", "gaps")}
        out["total"]["graph"] = "total"
    if out["loop"]["total"] is None and out["loop"]["graphs"]:
        out["loop"]["total"] = {k: sum(g[k] or 0 for g in out["loop"]["graphs"]) for k in
                                ("walks", "walk_findings", "neighbourhood_paths", "neighbourhood_new",
                                 "cross_walks", "cross_findings", "fault_walks", "fault_findings")}
        out["loop"]["total"]["graph"] = "total"
    return out


# One line per loop round in graph-coverage.md (`tests/graph/loop_report.render_rounds`):
# `- round N · <method> · findings K (ids) · streak S[ · STOP]`
LOOP_ROUND_RE = re.compile(r"^- round (?P<n>\d+) · (?P<method>.+?) · findings (?P<k>\d+)(?: \((?P<ids>[^)]*)\))? · streak (?P<streak>\d+)", re.M)
LOOP_STOP_STREAK = 3


def loop_rule(rounds: list) -> dict:
    """The user's loop rule (2026-10-19, testing.md §6.8): the loop runs round after round; a
    round that found bugs keeps its method, a zero-yield round forces a different method next,
    and the loop stops only after THREE consecutive zero-yield rounds (any finding resets)."""
    if not rounds:
        return {"round": None, "method": None, "findings": None, "streak": 0, "stop": False,
                "switch_method": False}
    last = rounds[-1]
    return {"round": last["round"], "method": last["method"], "findings": last["findings"],
            "streak": last["streak"], "stop": last["streak"] >= LOOP_STOP_STREAK,
            "switch_method": last["findings"] == 0}


def stop_rule(graph_total: dict | None, registers: dict) -> dict:
    """REFERENCE metrics of the GRAPH + LOOP layer (the stop decision itself is `loop_rule`):
    prime paths covered %, the number of consecutive registers (latest first) that opened zero
    graph findings, and the latest register's capture–recapture remaining (None until a register
    carries channels A and B)."""
    g = graph_total or {}
    pct = None
    if g.get("prime_paths"):
        pct = round(100.0 * (g.get("prime_paths_covered") or 0) / g["prime_paths"], 1)
    zero = 0
    for r in reversed(registers.get("rounds") or []):
        if r["discovery"].get("graph_findings", 0) == 0:
            zero += 1
        else:
            break
    latest = (registers.get("rounds") or [None])[-1]
    remaining = latest["discovery"]["estimate_remaining"] if latest else None
    return {
        "prime_paths_pct": pct,
        "zero_finding_rounds": zero,
        "estimate_remaining": remaining,
    }


# --------------------------------------------------------------------------- ops (deploy-ops ledger)

LEDGER_LINE_RE = re.compile(r"^-\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z?)\s+(.*)$")
HOST_UPDATE_RE = re.compile(r"host-update done(?:\s+\**([0-9a-f]{7,8})\**\s*(?:→|->)\s*\**([0-9a-f]{7,8})\**)?")
HOST_RE = re.compile(r"\b(t1|default)\b")
RESTART_RE = re.compile(r"\brestarted\b|\brestart\s*(?:\+\s*health\s*)?(?:ok|done|OK)\b|bootstrap\+restart|backend restart", re.I)
ROLLBACK_RE = re.compile(r"\brolled back\b|\broll(?:ed)?[- ]?back\b|\brollback\b", re.I)
DRILL_RE = re.compile(r"\bDRILL\b")
DRILL_TIMING_RE = re.compile(r"stop\s+[\d:.]+Z?\s*(?:->|→)\s*healthy\s+[\d:.]+Z?\s*=\s*([\d.]+)\s*s")
UPDATE_TIMING_RE = re.compile(r"\d{2}:\d{2}(?::\d{2})?Z?\s*(?:->|→)\s*~?\d{2}:\d{2}(?::\d{2})?Z?\s*~?\s*(\d+(?:\.\d+)?)\s*min")


def collect_ops(ledger_path: str) -> dict:
    text = read_text(ledger_path)
    out = {"ledger": ledger_path if text is not None else None, "lines": 0, "host_updates": [], "updates_per_host": {}, "restarts": 0, "rollbacks": [], "drills": []}
    if text is None:
        return out
    for line in text.splitlines():
        m = LEDGER_LINE_RE.match(line.strip())
        if not m:
            continue
        out["lines"] += 1
        ts, body = m.group(1), m.group(2)
        hm = HOST_UPDATE_RE.search(body)
        if hm:
            hh = HOST_RE.search(body)
            tm = UPDATE_TIMING_RE.search(body)
            out["host_updates"].append(
                {"ts": ts, "host": hh.group(1) if hh else "?", "from": hm.group(1), "to": hm.group(2), "minutes": float(tm.group(1)) if tm else None}
            )
        if RESTART_RE.search(body):
            out["restarts"] += 1
        if ROLLBACK_RE.search(body) and "disable-rollback" not in body.lower() and not re.search(r"canary|control-wins|experiment", body, re.I):
            out["rollbacks"].append({"ts": ts, "text": redact(body[:120])})
        if DRILL_RE.search(body):
            tm = DRILL_TIMING_RE.search(body)
            hh = HOST_RE.search(body)
            out["drills"].append(
                {"ts": ts, "host": hh.group(1) if hh else "?", "restore_s": float(tm.group(1)) if tm else None, "reverted": "reverted" in body.lower(), "text": redact(body[:100])}
            )
    out["updates_per_host"] = dict(sorted(Counter(u["host"] for u in out["host_updates"]).items()))
    return out


# --------------------------------------------------------------------------- gate (verify logs)

PYTEST_SUMMARY_RE = re.compile(r"^(?:(\d+) failed, )?(\d+) passed(?:, (\d+) skipped)?.*? in ([\d.]+)s", re.M)


def collect_gate(verify_glob: str, last_n: int) -> dict:
    paths = glob.glob(verify_glob)
    items = []
    for p in paths:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        items.append((mtime, p))
    items.sort()
    runs = []
    for mtime, p in items[-last_n:]:
        text = read_text(p) or ""
        verdicts = re.findall(r"verify:\s*(PASS|FAIL)", text)
        rc = re.findall(r"verify_rc=(\d+)", text)
        pm = None
        for m in PYTEST_SUMMARY_RE.finditer(text):
            if pm is None or float(m.group(4)) > float(pm.group(4)):
                pm = m  # the backend suite is the longest summary line
        sections = re.findall(r"^── ([^:]+): (OK|FAIL)", text, re.M)
        failed_sections = sorted({name.strip() for name, res in sections if res == "FAIL"})
        runs.append(
            {
                "log": os.path.basename(p),
                "mtime": dt.datetime.fromtimestamp(mtime, dt.UTC).strftime("%Y-%m-%dT%H:%MZ"),
                "verdict": verdicts[-1] if verdicts else "in progress",
                "rc": int(rc[-1]) if rc else ({"PASS": 0, "FAIL": 1}.get(verdicts[-1]) if verdicts else None),
                "pytest_s": float(pm.group(4)) if pm else None,  # longest suite = backend
                "pytest_failed": int(pm.group(1) or 0) if pm else None,
                "pytest_passed": int(pm.group(2)) if pm else None,
                "failed_sections": [redact(s) for s in failed_sections][:6],
            }
        )
    passes = sum(1 for r in runs if r["verdict"] == "PASS")
    graded = sum(1 for r in runs if r["verdict"] in ("PASS", "FAIL"))
    return {"glob": verify_glob, "runs": runs, "pass": passes, "total": graded, "listed": len(runs)}


# --------------------------------------------------------------------------- snapshot + trends

def build_snapshot(repo: str, ops_ledger: str, verify_glob: str, today: dt.date, gate_last: int) -> dict:
    runs = collect_runs(repo)
    quarantine = collect_quarantine(repo, today)
    registers = collect_registers(repo)
    security = collect_security(repo)
    ops = collect_ops(ops_ledger)
    gate = collect_gate(verify_glob, gate_last)
    mutation = collect_mutation(repo)
    graph = collect_graph(repo)
    graph_total = graph["total"] or {}
    loop_total = graph["loop"]["total"] or {}
    stop = stop_rule(graph["total"], registers)
    loop = loop_rule(graph["rounds"])
    last_run = runs[-1] if runs else None
    counted = [r for r in runs if r["passed"] is not None]
    latest_reg = registers["rounds"][-1] if registers["rounds"] else None
    disc = latest_reg["discovery"] if latest_reg else None
    mut_be = (mutation["sides"].get("backend") or {}).get("total") or {}
    mut_fe = (mutation["sides"].get("frontend") or {}).get("total") or {}
    kpis = {
        "runs": len(runs),
        "last_run_pass_rate": (last_run["passed"] / (last_run["passed"] + last_run["failed"])) if last_run and last_run.get("passed") is not None and (last_run["passed"] + last_run["failed"]) else None,
        "runs_env_failures": sum(r["failures_by_class"]["ENV"] for r in runs),
        "runs_test_failures": sum(r["failures_by_class"]["TEST"] for r in runs),
        "runs_product_failures": sum(r["failures_by_class"]["PRODUCT"] for r in runs),
        "runs_unclassified": sum(r["failures_by_class"]["UNCLASSIFIED"] for r in runs),
        "quarantine_count": quarantine["count"],
        "quarantine_max_age_days": quarantine["max_age_days"],
        "register_ids": registers["totals"]["ids"],
        "register_fixed": registers["totals"]["fixed"],
        "register_pending": registers["totals"]["pending"],
        "register_live_retest_pending": registers["totals"]["live_retest_pending"],
        "register_critical_high": registers["by_severity"]["Critical"] + registers["by_severity"]["High"],
        "defect_escape_ratio": registers["defect_escape_ratio"],
        "security_critical": security["counts"].get("Critical", 0),
        "security_high": security["counts"].get("High", 0),
        "ops_host_updates": len(ops["host_updates"]),
        "ops_restarts": ops["restarts"],
        "ops_rollbacks": len(ops["rollbacks"]),
        "ops_drills": len(ops["drills"]),
        "gate_pass": gate["pass"],
        "gate_total": gate["total"],
        "gate_last_rc": next((r["rc"] for r in reversed(gate["runs"]) if r["rc"] is not None), None),
        "gate_last_pytest_s": next((r["pytest_s"] for r in reversed(gate["runs"]) if r["pytest_s"] is not None), None),
        # discovery KPIs of the latest register (BUG-DISCOVERY-PLAN.md §0)
        "discovery_round": latest_reg["round"] if latest_reg else None,
        "discovery_findings": disc["findings"] if disc else None,
        "discovery_product": disc["by_class"]["PRODUCT"] if disc else None,
        "discovery_test": disc["by_class"]["TEST"] if disc else None,
        "discovery_env": disc["by_class"]["ENV"] if disc else None,
        "discovery_weighted": disc["weighted_score"] if disc else None,
        "discovery_escaped": disc["escaped"] if disc else None,
        "discovery_escape_ratio": disc["escape_ratio"] if disc else None,
        "discovery_est_remaining": disc["estimate_remaining"] if disc else None,
        "discovery_exploratory_yield": disc["exploratory_yield"] if disc else None,
        "mutation_score_backend": mut_be.get("score"),
        "mutation_score_frontend": mut_fe.get("score"),
        # lifecycle-graph coverage (round 16 r16-o, docs/cn/reports/graph-coverage.md)
        "graph_edges": graph_total.get("edges"),
        "graph_edges_covered": graph_total.get("edges_covered"),
        "graph_prime_paths": graph_total.get("prime_paths"),
        "graph_prime_paths_covered": graph_total.get("prime_paths_covered"),
        "graph_contract_gaps": graph_total.get("gaps"),
        # round-17 loop engine (testing.md §6.8) — additive
        "graph_walks": loop_total.get("walks"),
        "graph_walk_findings": loop_total.get("walk_findings"),
        "neighbourhood_paths": loop_total.get("neighbourhood_paths"),
        "neighbourhood_new": loop_total.get("neighbourhood_new"),
        "graph_cross_walks": loop_total.get("cross_walks"),
        "graph_cross_findings": loop_total.get("cross_findings"),
        "graph_fault_walks": loop_total.get("fault_walks"),
        "graph_fault_findings": loop_total.get("fault_findings"),
        "graph_findings_latest_round": (latest_reg["discovery"].get("graph_findings") if latest_reg else None),
        # reference metrics (not the stop decision)
        "graph_stop_prime_pct": stop["prime_paths_pct"],
        "graph_stop_zero_rounds": stop["zero_finding_rounds"],
        "graph_stop_est_remaining": stop["estimate_remaining"],
        # the user's loop rule (testing.md §6.8): stop after three consecutive zero-yield rounds
        "loop_round": loop["round"],
        "loop_round_method": loop["method"],
        "loop_round_findings": loop["findings"],
        "loop_zero_streak": loop["streak"],
        "loop_stop": loop["stop"],
        "loop_switch_method": loop["switch_method"],
    }
    del counted
    return {
        "date": today.isoformat(),
        "kpis": kpis,
        "storyline": runs,
        "quarantine": quarantine,
        "registers": registers,
        "security": security,
        "ops": {k: v for k, v in ops.items()},
        "gate": gate,
        "mutation": mutation,
        "graph": graph,
        "stop_rule": stop,
        "loop_rule": loop,
    }


def load_history(path: str) -> list[dict]:
    text = read_text(path)
    if not text:
        return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def history_entry(snapshot: dict) -> dict:
    return {"date": snapshot["date"], "kpis": snapshot["kpis"]}


def write_history(path: str, snapshot: dict) -> list[dict]:
    """Replace today's line if present, else append. Returns the history before this write."""
    hist = load_history(path)
    prev = [h for h in hist if h.get("date") != snapshot["date"]]
    prev.sort(key=lambda h: h.get("date") or "")
    new = prev + [history_entry(snapshot)]
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for h in new:
            fh.write(json.dumps(h, sort_keys=True, ensure_ascii=False) + "\n")
    return prev


def arrow(cur, prev, higher_is_better: bool | None):
    """→ when unchanged/unknown; ↑/↓ follow the value; (better)/(worse) when a direction is known."""
    if cur is None or prev is None:
        return "→ (no previous)"
    if isinstance(cur, float) or isinstance(prev, float):
        delta = (cur or 0) - (prev or 0)
        same = abs(delta) < 1e-9
    else:
        delta = cur - prev
        same = delta == 0
    if same:
        return "→ unchanged"
    up = delta > 0
    sym = "↑" if up else "↓"
    if higher_is_better is None:
        return f"{sym} from {fmt_val(prev)}"
    good = (up and higher_is_better) or (not up and not higher_is_better)
    return f"{sym} from {fmt_val(prev)} ({'better' if good else 'worse'})"


def fmt_val(v):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.3f}" if v < 1 else f"{v:.1f}"
    return str(v)


def stop_rule_text(k: dict) -> str:
    """`round 1 · <method> · findings 6 · streak 0/3 · next round: same method` — the user's rule;
    the reference metrics follow in `reference_metrics_text`."""
    if k.get("loop_round") is None:
        return "no loop round recorded yet (tests/graph/loop_rounds.yaml)"
    nxt = ("STOP — three consecutive zero-yield rounds" if k.get("loop_stop") else
           "next round: a DIFFERENT method" if k.get("loop_switch_method") else
           "next round: the same method")
    return (f"round {k['loop_round']} · {k.get('loop_round_method') or '—'} · findings "
            f"{fmt_val(k.get('loop_round_findings'))} · zero-yield streak "
            f"{fmt_val(k.get('loop_zero_streak'))}/{LOOP_STOP_STREAK} · {nxt}")


def reference_metrics_text(k: dict) -> str:
    pct = k.get("graph_stop_prime_pct")
    return (f"prime paths {fmt_val(pct) + ' %' if pct is not None else '—'} covered · "
            f"{fmt_val(k.get('graph_stop_zero_rounds'))} consecutive register(s) without a graph "
            f"finding · capture–recapture remaining {fmt_val(k.get('graph_stop_est_remaining'))}")


def summary_lines(snapshot: dict, previous: dict | None) -> list[str]:
    k = snapshot["kpis"]
    p = (previous or {}).get("kpis", {})
    g = lambda key: p.get(key)  # noqa: E731
    last = snapshot["storyline"][-1] if snapshot["storyline"] else None
    last_desc = f"{last['run']} ({last['date']}, host {last['host_sha'] or '?'}): {last['passed']} pass / {last['failed']} fail / {last['skipped']} skip" if last else "none"
    pr = k["last_run_pass_rate"]
    lines = [
        f"1. Storyline runs on record: {k['runs']} {arrow(k['runs'], g('runs'), None)}; latest {last_desc}; pass rate {pct(round((pr or 0) * 100, 1), 100) if pr is not None else '—'} {arrow(round(pr, 3) if pr is not None else None, round(g('last_run_pass_rate'), 3) if g('last_run_pass_rate') is not None else None, True)}.",
        f"2. Failures by class across all runs: ENV {k['runs_env_failures']} {arrow(k['runs_env_failures'], g('runs_env_failures'), False)}, TEST {k['runs_test_failures']} {arrow(k['runs_test_failures'], g('runs_test_failures'), False)}, PRODUCT {k['runs_product_failures']} {arrow(k['runs_product_failures'], g('runs_product_failures'), False)}, unclassified {k['runs_unclassified']}.",
        f"3. Quarantine: {k['quarantine_count']} spec(s) {arrow(k['quarantine_count'], g('quarantine_count'), False)}, oldest {k['quarantine_max_age_days']} day(s) {arrow(k['quarantine_max_age_days'], g('quarantine_max_age_days'), False)}.",
        f"4. Registers: {k['register_ids']} ids {arrow(k['register_ids'], g('register_ids'), None)}; fixed {k['register_fixed']} {arrow(k['register_fixed'], g('register_fixed'), True)}; pending {k['register_pending']} {arrow(k['register_pending'], g('register_pending'), False)}; live re-test pending {k['register_live_retest_pending']} {arrow(k['register_live_retest_pending'], g('register_live_retest_pending'), False)}.",
        f"5. Critical + High register ids: {k['register_critical_high']} {arrow(k['register_critical_high'], g('register_critical_high'), False)}.",
        f"6. Defect escape ratio (user-reported / all ids): {fmt_val(k['defect_escape_ratio'])} {arrow(round(k['defect_escape_ratio'], 3) if k['defect_escape_ratio'] is not None else None, round(g('defect_escape_ratio'), 3) if g('defect_escape_ratio') is not None else None, False)}.",
        f"7. Security (latest scan {snapshot['security']['date'] or '—'}): Critical {k['security_critical']} {arrow(k['security_critical'], g('security_critical'), False)}, High {k['security_high']} {arrow(k['security_high'], g('security_high'), False)} — all third-party advisories unless the scan says otherwise.",
        f"8. Ops: host updates {k['ops_host_updates']} {arrow(k['ops_host_updates'], g('ops_host_updates'), None)}, restart lines {k['ops_restarts']} {arrow(k['ops_restarts'], g('ops_restarts'), None)}, rollbacks {k['ops_rollbacks']} {arrow(k['ops_rollbacks'], g('ops_rollbacks'), False)}, drills {k['ops_drills']} {arrow(k['ops_drills'], g('ops_drills'), True)}.",
        f"9. Verify gate (last {k['gate_total']} logs): {k['gate_pass']} PASS {arrow(k['gate_pass'], g('gate_pass'), True)}; last rc {fmt_val(k['gate_last_rc'])}; last backend pytest {fmt_min(k['gate_last_pytest_s'])} {arrow(k['gate_last_pytest_s'], g('gate_last_pytest_s'), False)}.",
        f"10. Snapshot {snapshot['date']}; previous snapshot {previous['date'] if previous else 'none'}; sources: {len(snapshot['storyline'])} REPORT.md, {len(snapshot['registers']['rounds'])} registers, ledger {'present' if snapshot['ops']['ledger'] else 'absent'}, {k['gate_total']} gate logs.",
        f"11. Discovery ({k['discovery_round'] or 'no register'}): findings {fmt_val(k['discovery_findings'])} {arrow(k['discovery_findings'], g('discovery_findings'), None)} "
        f"(PRODUCT {fmt_val(k['discovery_product'])} · TEST {fmt_val(k['discovery_test'])} · ENV {fmt_val(k['discovery_env'])}); weighted {fmt_val(k['discovery_weighted'])} {arrow(k['discovery_weighted'], g('discovery_weighted'), None)}; "
        f"escaped {fmt_val(k['discovery_escaped'])} ({fmt_val(k['discovery_escape_ratio'])}) {arrow(round(k['discovery_escape_ratio'], 3) if k['discovery_escape_ratio'] is not None else None, round(g('discovery_escape_ratio'), 3) if g('discovery_escape_ratio') is not None else None, False)}; "
        f"capture–recapture remaining {fmt_val(k['discovery_est_remaining'])}; exploratory yield {fmt_val(k['discovery_exploratory_yield'])}/h; "
        f"lifecycle graphs: edges {fmt_val(k['graph_edges_covered'])}/{fmt_val(k['graph_edges'])}, prime paths {fmt_val(k['graph_prime_paths_covered'])}/{fmt_val(k['graph_prime_paths'])} covered; "
        f"loop engine: walks {fmt_val(k['graph_walks'])} ({fmt_val(k['graph_walk_findings'])} findings), neighbourhood paths {fmt_val(k['neighbourhood_paths'])}, cross-graph walks {fmt_val(k['graph_cross_walks'])} ({fmt_val(k['graph_cross_findings'])} findings); "
        f"loop rule: {stop_rule_text(k)}; reference: {reference_metrics_text(k)}.",
        f"12. Mutation score (baseline {snapshot['mutation']['date'] or '—'}): backend {pct(k['mutation_score_backend'], 100) if k['mutation_score_backend'] is not None else '—'} {arrow(k['mutation_score_backend'], g('mutation_score_backend'), True)}, "
        f"frontend {pct(k['mutation_score_frontend'], 100) if k['mutation_score_frontend'] is not None else '—'} {arrow(k['mutation_score_frontend'], g('mutation_score_frontend'), True)} (uncovered mutants count as survived).",
    ]
    return lines


# --------------------------------------------------------------------------- markdown

def render_markdown(snapshot: dict, previous: dict | None, repo: str) -> str:
    L = []
    L.append("# Quality metrics board (CN)")
    L.append("")
    L.append(f"Generated {snapshot['date']} by `scripts/quality_metrics.py` (`make metrics`). Read-only aggregation of the "
             "storyline evidence, the e2e quarantine list, the findings registers (plus their round-16 discovery markers), the latest "
             "security scan, the deploy-ops ledger, the verify-gate logs and the latest mutation baseline. Heuristics and vocabulary: "
             "`docs/cn/interfaces/testing.md` § \"Quality metrics board\" and § \"Mutation testing & discovery KPIs\"; "
             "failure classes per `tests/e2e/README.md` § \"Failure classification\". History: `quality-metrics-history.jsonl` (one line per day).")
    L.append("")
    L.append("## Summary (trend vs previous snapshot)")
    L.append("")
    L.extend(summary_lines(snapshot, previous))
    L.append("")

    L.append("## 1. Storyline runs (`docs/cn/evidence/round-*/**/REPORT.md`)")
    L.append("")
    L.append("| run | date | host sha | pass | fail | skip | duration | verdict | failures (step · spec · class) | counts from |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in snapshot["storyline"]:
        fl = "; ".join(f"{f['step']} · {f['spec'] or '?'} · {f['class']}" + (f" ({f['note']})" if f.get('note') else "") for f in r["failures"]) or "—"
        L.append(f"| `{r['run']}` | {r['date'] or '—'} | {r['host_sha'] or '—'} | {fmt_val(r['passed'])} | {fmt_val(r['failed'])} | {fmt_val(r['skipped'])} | {fmt_min(r['duration_s'])} | {r['verdict'] or '—'} | {fl} | {r['counts_source']} |")
    if not snapshot["storyline"]:
        L.append("| — | — | — | — | — | — | — | — | — | — |")
    L.append("")
    tot = Counter()
    for r in snapshot["storyline"]:
        tot.update(r["failures_by_class"])
    L.append(f"Failures by class (all runs): ENV {tot.get('ENV', 0)} · TEST {tot.get('TEST', 0)} · PRODUCT {tot.get('PRODUCT', 0)} · UNCLASSIFIED {tot.get('UNCLASSIFIED', 0)}.")
    L.append("")

    q = snapshot["quarantine"]
    L.append("## 2. Quarantine (`tests/e2e/cn/QUARANTINE.md`)")
    L.append("")
    L.append(f"Active: **{q['count']}** of {q['total_rows']} row(s); oldest active entry {q['max_age_days']} day(s).")
    L.append("")
    L.append("| spec | ids | first | last | age (days) | state | exit criterion |")
    L.append("|---|---|---|---|---|---|---|")
    for e in q["entries"]:
        L.append(f"| `{e['spec']}` | {e['ids']} | {e['first'] or '—'} | {e['last'] or '—'} | {fmt_val(e['age_days'])} | {e['state']} | {e['exit_criterion']} |")
    if not q["entries"]:
        L.append("| — | — | — | — | — | — | — |")
    L.append("")

    reg = snapshot["registers"]
    L.append("## 3. Findings registers (`docs/cn/reports/bugs-round-*.md`)")
    L.append("")
    L.append("| round | file | ids | fixed | pending | n/a | recorded | live re-test pending | user-reported | internal | Critical | High | Medium | Low | Info | unknown sev |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in reg["rounds"]:
        s = r["by_severity"]
        L.append(f"| {r['round']} | `{os.path.basename(r['file'])}` | {r['ids']} | {r['fixed']} | {r['pending']} | {r['n_a']} | {r['recorded']} | {r['live_retest_pending']} | {r['user_reported']} | {r['internal']} | {s['Critical']} | {s['High']} | {s['Medium']} | {s['Low']} | {s['Info']} | {s['unknown']} |")
    t, s = reg["totals"], reg["by_severity"]
    L.append(f"| **total** | | **{t['ids']}** | {t['fixed']} | {t['pending']} | {t['n_a']} | {t['recorded']} | {t['live_retest_pending']} | {t['user_reported']} | {t['internal']} | {s['Critical']} | {s['High']} | {s['Medium']} | {s['Low']} | {s['Info']} | {s['unknown']} |")
    L.append("")
    L.append(f"Defect escape ratio = user-reported ids / all ids = {t['user_reported']} / {t['ids']} = **{fmt_val(reg['defect_escape_ratio'])}**. "
             "An id counts once per register; a later row with the same id is a state update. `n/a` = rulings / deletions with a `—` state; "
             "`recorded` = an observation row with neither a state cell nor a state keyword (older 7-column registers); "
             "a non-empty fix / commit cell counts as fixed.")
    L.append("")

    sec = snapshot["security"]
    L.append("## 4. Security (latest `docs/cn/reports/security-scan-*.md`)")
    L.append("")
    if sec["report"]:
        L.append(f"Report `{os.path.basename(sec['report'])}` ({sec['date']}). Advisory severities summed over the surface table:")
        L.append("")
        L.append("| surface | Critical | High | Medium | Low |")
        L.append("|---|---|---|---|---|")
        for s_ in sec["surfaces"]:
            L.append(f"| {s_['surface']} | {s_.get('Critical', 0)} | {s_.get('High', 0)} | {s_.get('Medium', 0)} | {s_.get('Low', 0)} |")
        c = sec["counts"]
        L.append(f"| **total** | **{c['Critical']}** | **{c['High']}** | {c['Medium']} | {c['Low']} |")
    else:
        L.append("No security scan report found.")
    L.append("")

    ops = snapshot["ops"]
    L.append("## 5. Ops (deploy-ops ledger, outside the repo)")
    L.append("")
    if ops["ledger"]:
        L.append(f"Ledger lines parsed: {ops['lines']}. Host updates: {len(ops['host_updates'])} "
                 f"({', '.join(f'{h} {n}' for h, n in ops['updates_per_host'].items()) or '—'}); restart lines {ops['restarts']}; "
                 f"rollbacks {len(ops['rollbacks'])}; drills {len(ops['drills'])}.")
        L.append("")
        L.append("| when (UTC) | host | from → to | unit minutes |")
        L.append("|---|---|---|---|")
        for u in ops["host_updates"][-12:]:
            L.append(f"| {u['ts']} | {u['host']} | {u['from'] or '?'} → {u['to'] or '?'} | {fmt_val(u['minutes'])} |")
        if len(ops["host_updates"]) > 12:
            L.append(f"| … | | {len(ops['host_updates']) - 12} earlier update(s) omitted | |")
        L.append("")
        L.append("| drill (UTC) | host | stop → healthy (s) | reverted |")
        L.append("|---|---|---|---|")
        for d in ops["drills"]:
            L.append(f"| {d['ts']} | {d['host']} | {fmt_val(d['restore_s'])} | {'yes' if d['reverted'] else '—'} |")
        if not ops["drills"]:
            L.append("| — | — | — | — |")
        if ops["rollbacks"]:
            L.append("")
            L.append("Rollback lines: " + "; ".join(f"{r['ts']}" for r in ops["rollbacks"]) + ".")
    else:
        L.append("Deploy-ops ledger not found (pass `--ops-ledger`).")
    L.append("")

    gate = snapshot["gate"]
    L.append(f"## 6. Verify gate (last {gate['listed']} of `{gate['glob']}`)")
    L.append("")
    L.append("| log | mtime (UTC) | verdict | rc | backend pytest | failed sections |")
    L.append("|---|---|---|---|---|---|")
    for r in gate["runs"]:
        py = f"{fmt_val(r['pytest_passed'])} passed / {fmt_val(r['pytest_failed'])} failed in {fmt_min(r['pytest_s'])}" if r["pytest_s"] is not None else "—"
        L.append(f"| `{r['log']}` | {r['mtime']} | {r['verdict'] or '—'} | {fmt_val(r['rc'])} | {py} | {', '.join(r['failed_sections']) or '—'} |")
    if not gate["runs"]:
        L.append("| — | — | — | — | — | — |")
    L.append("")
    L.append(f"PASS {gate['pass']} / {gate['total']} graded (a log without a verdict line is a gate still running). The logs carry no wall clock; the backend pytest line is the only duration recorded, so it stands in for gate duration.")
    L.append("")

    L.append("## 7. Discovery (bug-discovery plan §0 KPIs, per register)")
    L.append("")
    L.append("| round | findings | PRODUCT | TEST | ENV | unclassified | weighted (P1=5 P2=3 P3=1) | escaped | escape ratio | A | B | C | untagged | A∩B | est. total | est. remaining | B session h | B per h |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in reg["rounds"]:
        d = r["discovery"]
        c, ch = d["by_class"], d["channels"]
        L.append(f"| {r['round']} | {d['findings']} | {c['PRODUCT']} | {c['TEST']} | {c['ENV']} | {c['unclassified']} | {d['weighted_score']} | {d['escaped']} | {fmt_val(d['escape_ratio'])} | "
                 f"{ch['A']} | {ch['B']} | {ch['C']} | {ch['untagged']} | {d['overlap_ab']} | {fmt_val(d['estimate_total'])} | {fmt_val(d['estimate_remaining'])} | {fmt_val(d['session_hours'])} | {fmt_val(d['exploratory_yield'])} |")
    if not reg["rounds"]:
        L.append("| — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |")
    L.append("")
    L.append("Markers (any cell of a register row, usually the id cell; all optional): `class: PRODUCT|TEST|ENV` (else the first upper-case "
             "PRODUCT / TEST / ENV word of the row, else unclassified) · `channel: A|B|C` (`A+B` when both) · `repro-hash: <8 hex>` "
             "(two ids with the same hash = one defect seen by both channels; compute with `--repro-hash \"<minimal repro>\"`) · "
             "`found-by: user` (escaped; also `user report`, the word `USER`, `逃逸`, `WB-` ids) · `session: <id>/<hours>h` on a channel-B row · "
             "`module: Enn` · `kind: process|ruling` (excluded from these counts). Weighted = Σ severity weights (Critical and High 5, "
             "Medium 3, Low 1). est. total = Chapman capture–recapture (nA+1)(nB+1)/(m+1) − 1 over channels A and B; est. remaining = "
             "est. total − (nA + nB − m); both blank until a register carries both channels. B per h = channel-B findings ÷ Σ distinct "
             "session hours.")
    L.append("")

    graph = snapshot.get("graph") or {}
    L.append("### 7a. Lifecycle-graph coverage (`make graph-coverage` → `docs/cn/reports/graph-coverage.md`, r16-o)")
    L.append("")
    if graph.get("total"):
        L.append("| graph | edges | edges covered | prime paths | prime paths covered | edge pairs | pairs covered | contract gaps |")
        L.append("|---|---|---|---|---|---|---|---|")
        for g in graph["graphs"]:
            L.append(f"| `{g['graph']}` | {g['edges']} | {g['edges_covered']} | {g['prime_paths']} | {g['prime_paths_covered']} | {g['edge_pairs']} | {g['edge_pairs_covered']} | {g['gaps']} |")
        t = graph["total"]
        L.append(f"| **total** | {t['edges']} | **{t['edges_covered']}** | {t['prime_paths']} | **{t['prime_paths_covered']}** | {t['edge_pairs']} | {t['edge_pairs_covered']} | {t['gaps']} |")
        L.append("")
        L.append("Covered = walked by the hermetic API executor in the recording run and passing; a path pinned on a register finding counts as uncovered until the fix lands. "
                 "Stop rule (plan §4, testing.md §6.7): prime paths 100 % covered, two consecutive rounds with 0 new findings, capture–recapture remaining < 2.")
    else:
        L.append("No graph coverage report yet (`make graph-coverage` writes it).")
    L.append("")
    loop = graph.get("loop") or {}
    k = snapshot["kpis"]
    L.append("### 7b. Loop engine (round 17 r17-b, testing.md §6.8) and the stop rule")
    L.append("")
    if loop.get("total"):
        L.append("| graph | walks | walk findings | neighbourhood paths | newly covered | cross-graph walks | cross findings | fault walks | fault findings |")
        L.append("|---|---|---|---|---|---|---|---|---|")
        for g in loop["graphs"]:
            L.append(f"| `{g['graph']}` | {g['walks']} | {g['walk_findings']} | {g['neighbourhood_paths']} | {g['neighbourhood_new']} | {g['cross_walks']} | {g['cross_findings']} | {fmt_val(g.get('fault_walks'))} | {fmt_val(g.get('fault_findings'))} |")
        t = loop["total"]
        L.append(f"| **total** | **{t['walks']}** | {t['walk_findings']} | {t['neighbourhood_paths']} | {t['neighbourhood_new']} | **{t['cross_walks']}** | {t['cross_findings']} | **{fmt_val(t.get('fault_walks'))}** | {fmt_val(t.get('fault_findings'))} |")
    else:
        L.append("No loop-engine table yet (`make graph-coverage` on a round-17 branch writes it).")
    L.append("")
    L.append(f"**Loop rule (user ruling 2026-10-19):** {stop_rule_text(k)}. A round that found bugs (PRODUCT / TEST / DOC all count) keeps its method; a zero-yield round forces a different method next; the loop stops only after three consecutive zero-yield rounds. Rounds (`tests/graph/loop_rounds.yaml`, rendered into graph-coverage.md):")
    L.append("")
    L.append("| round | method | findings | ids | streak |")
    L.append("|---|---|---|---|---|")
    for r in graph.get("rounds") or []:
        L.append(f"| {r['round']} | {r['method']} | {r['findings']} | {', '.join(r['ids']) or '—'} | {r['streak']}{' · STOP' if r['streak'] >= LOOP_STOP_STREAK else ''} |")
    if not graph.get("rounds"):
        L.append("| — | — | — | — | — |")
    L.append("")
    L.append(f"**Reference metrics:** {reference_metrics_text(k)}. Graph findings in the latest register: {fmt_val(k.get('graph_findings_latest_round'))} "
             "(a row whose id / subsystem cell names the lifecycle graph, a random walk, a neighbourhood path or a cross-graph walk). "
             "Per deep-dive path (plan §4; rows carrying `module:`), Chapman over channels A / B:")
    L.append("")
    L.append("| deep-dive path | findings | A | B | A∩B | est. total | est. remaining |")
    L.append("|---|---|---|---|---|---|---|")
    latest = reg["rounds"][-1] if reg["rounds"] else None
    for path, d in ((latest or {}).get("discovery", {}).get("paths") or {}).items():
        L.append(f"| {path} | {d['findings']} | {d['n_a']} | {d['n_b']} | {d['overlap']} | {fmt_val(d['estimate_total'])} | {fmt_val(d['estimate_remaining'])} |")
    if not latest:
        L.append("| — | — | — | — | — | — | — |")
    L.append("")

    mut = snapshot["mutation"]
    L.append("## 8. Mutation testing (latest `docs/cn/reports/mutation-baseline-*.md`)")
    L.append("")
    if mut["report"]:
        L.append(f"Report `{os.path.basename(mut['report'])}` ({mut['date']}). Score = killed / (killed + survived + timeout + uncovered); uncovered "
                 "(`no tests` / `no coverage`) mutants count as survived — a behaviour no test reaches is untested. `covered` leaves them out.")
        for side in ("backend", "frontend"):
            s_ = mut["sides"].get(side)
            if not s_:
                continue
            L.append("")
            L.append(f"**{side}**")
            L.append("")
            L.append("| file | mutants | killed | survived | timeout | uncovered | score | score (covered) |")
            L.append("|---|---|---|---|---|---|---|---|")
            for r in s_["files"]:
                L.append(f"| `{r['file']}` | {r['mutants']} | {r['killed']} | {r['survived']} | {r['timeout']} | {r['uncovered']} | {pct(r['score'], 100) if r['score'] is not None else '—'} | {pct(r['score_covered'], 100) if r['score_covered'] is not None else '—'} |")
            t_ = s_["total"]
            L.append(f"| **total** | {t_['mutants']} | {t_['killed']} | {t_['survived']} | {t_['timeout']} | {t_['uncovered']} | **{pct(t_['score'], 100) if t_['score'] is not None else '—'}** | {pct(t_['score_covered'], 100) if t_['score_covered'] is not None else '—'} |")
    else:
        L.append("No mutation baseline report found (`make mutation` / `make mutation-frontend`, then write the report).")
    L.append("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- main

def _show(path: str, repo: str) -> str:
    rel = os.path.relpath(path, repo)
    return rel if not rel.startswith("..") else path


def main(argv=None) -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.path.abspath(os.path.join(here, "..")), help="repository root (default: parent of scripts/)")
    ap.add_argument("--ops-ledger", default=DEFAULT_OPS_LEDGER, help="deploy-ops ledger path (outside the repo)")
    ap.add_argument("--verify-glob", default=DEFAULT_VERIFY_GLOB, help="glob of verify gate logs")
    ap.add_argument("--gate-last", type=int, default=10, help="how many gate logs to show")
    ap.add_argument("--date", default=None, help="snapshot date YYYY-MM-DD (default: today UTC)")
    ap.add_argument("--out", default=None, help=f"markdown output (default: <repo>/{REPORT_REL})")
    ap.add_argument("--history", default=None, help=f"history jsonl (default: <repo>/{HISTORY_REL})")
    ap.add_argument("--json", action="store_true", help="print the snapshot JSON to stdout")
    ap.add_argument("--no-write", action="store_true", help="compute only; do not touch the report or the history")
    ap.add_argument("--repro-hash", default=None, metavar="TEXT", help="print the `repro-hash:` marker for a minimal repro and exit")
    ap.add_argument("--stop-rule", action="store_true", help="print the GRAPH + LOOP stop-rule triplet (no write) and exit — `make graph-coverage` ends with it")
    args = ap.parse_args(argv)

    if args.repro_hash is not None:
        print(repro_hash(args.repro_hash))
        return 0
    if args.stop_rule:
        repo = os.path.abspath(args.repo)
        today = parse_date(args.date) if args.date else dt.datetime.now(dt.UTC).date()
        snap = build_snapshot(repo, args.ops_ledger, args.verify_glob, today, args.gate_last)
        k = snap["kpis"]
        print(f"loop rule — {stop_rule_text(k)}")
        print(f"  reference — {reference_metrics_text(k)}")
        print(f"  lifecycle graphs: edges {fmt_val(k['graph_edges_covered'])}/{fmt_val(k['graph_edges'])}, prime paths "
              f"{fmt_val(k['graph_prime_paths_covered'])}/{fmt_val(k['graph_prime_paths'])}; loop engine: walks {fmt_val(k['graph_walks'])} "
              f"({fmt_val(k['graph_walk_findings'])} findings), neighbourhood paths {fmt_val(k['neighbourhood_paths'])} "
              f"({fmt_val(k['neighbourhood_new'])} newly covered), cross-graph walks {fmt_val(k['graph_cross_walks'])} "
              f"({fmt_val(k['graph_cross_findings'])} findings); graph findings in the latest register: {fmt_val(k['graph_findings_latest_round'])}")
        return 0
    today = parse_date(args.date) if args.date else dt.datetime.now(dt.UTC).date()
    if today is None:
        print("quality_metrics: --date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    repo = os.path.abspath(args.repo)
    out_path = args.out or os.path.join(repo, REPORT_REL)
    hist_path = args.history or os.path.join(repo, HISTORY_REL)

    snapshot = build_snapshot(repo, args.ops_ledger, args.verify_glob, today, args.gate_last)
    hist = load_history(hist_path)
    before = sorted([h for h in hist if (h.get("date") or "") < snapshot["date"]], key=lambda h: h.get("date") or "")
    previous = before[-1] if before else None

    if not args.no_write:
        write_history(hist_path, snapshot)
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(snapshot, previous, repo))
    if args.json:
        print(json.dumps(snapshot, sort_keys=True, ensure_ascii=False, indent=1))
    else:
        print("\n".join(summary_lines(snapshot, previous)))
        if not args.no_write:
            print(f"\nwrote {_show(out_path, repo)} and {_show(hist_path, repo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
