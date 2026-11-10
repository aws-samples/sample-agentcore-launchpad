#!/usr/bin/env python3
"""Mutation baseline report helper — turns the raw mutmut / Stryker results into markdown.

Reads (both optional):

  backend/mutants/**/*.meta              mutmut 3 per-file results (exit code per mutant) — written by
                                          `make mutation`; the diff of a survivor comes from mutmut's own
                                          index, so this script re-executes itself inside the backend venv
                                          (`uv run python … --dump-backend`) to read it
  frontend/reports/mutation/mutation.json Stryker JSON report — written by `make mutation-frontend`

Prints markdown to stdout: per-file score tables in the shape `scripts/quality_metrics.py`
parses (`| file | mutants | killed | survived | timeout | no tests | score | score (covered) |`) and
one appendix table of surviving mutants per side (`file:line`, operator, `original → mutant`).
Score = killed / (killed + survived + timeout + uncovered): a mutant no test reaches counts as
survived. The report itself (docs/cn/reports/mutation-baseline-<date>.md) is hand-finished: the
grouped findings and the "how to read" paragraph are not generated.

Stdlib only; read-only; no AWS. Not part of `make verify`.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))

STATUS = {1: "killed", 3: "killed", 0: "survived", 5: "no tests", 33: "no tests", 34: "skipped",
          35: "suspicious", 36: "timeout", -24: "timeout", 37: "type-check", None: "not checked"}

# --------------------------------------------------------------------------- backend (mutmut)


def _mangled_to_def(mangled: str) -> tuple[str | None, str]:
    """`x_name` → (None, name); `xǁClassǁmethod` → (Class, method)."""
    base = re.sub(r"__mutmut_\d+$", "", mangled)
    if base.startswith("xǁ"):
        _, cls, meth = base.split("ǁ", 2)
        return cls, meth
    return None, base[2:] if base.startswith("x_") else base


def _def_line(source_lines: list[str], cls: str | None, name: str) -> int | None:
    start = 0
    if cls:
        for i, ln in enumerate(source_lines):
            if re.match(rf"\s*class\s+{re.escape(cls)}\b", ln):
                start = i
                break
    pat = re.compile(rf"\s*(?:async\s+)?def\s+{re.escape(name)}\s*\(")
    for i in range(start, len(source_lines)):
        if pat.match(source_lines[i]):
            return i + 1
    return None


def _operator(old: str, new: str) -> str:
    o, n = old.strip(), new.strip()
    if re.search(r'"XX.*XX"|\'XX.*XX\'', n) or (re.search(r"[\"']", o) and re.search(r"[\"']", n) and o.replace('"', "'") != n.replace('"', "'") and re.sub(r"[\"'][^\"']*[\"']", "S", o) == re.sub(r"[\"'][^\"']*[\"']", "S", n)):
        return "string literal"
    if "None" in n and "None" not in o:
        return "→ None"
    if re.sub(r"\d+(\.\d+)?", "N", o) == re.sub(r"\d+(\.\d+)?", "N", n) and o != n:
        return "number ±1"
    for a, b, label in ((" and ", " or ", "boolean op"), (" or ", " and ", "boolean op"), (" not ", " ", "negation"), ("True", "False", "boolean"), ("False", "True", "boolean")):
        if a in o and b in n and o.replace(a, b, 1) == n:
            return label
    cmp_o = re.findall(r"==|!=|<=|>=|<|>|\bnot in\b|\bin\b|\bis not\b|\bis\b", o)
    cmp_n = re.findall(r"==|!=|<=|>=|<|>|\bnot in\b|\bin\b|\bis not\b|\bis\b", n)
    if cmp_o != cmp_n:
        return "comparison"
    if re.search(r"\b(return|break|continue|raise)\b", o) and not re.search(r"\b(return|break|continue|raise)\b", n):
        return "control flow"
    if re.search(r"[-+*/%]=?", o) and re.sub(r"[-+*/%]", "@", o) == re.sub(r"[-+*/%]", "@", n):
        return "arithmetic"
    if o.split("(")[0] == n.split("(")[0] and o.count(",") != n.count(","):
        return "argument removed"
    if "lambda" in o:
        return "lambda"
    return "statement"


def dump_backend() -> dict:
    """Run from backend/ inside its venv: survivors with their diffs. Prints JSON."""
    sys.path.insert(0, os.getcwd())
    from mutmut.mutation.diff_apply import get_diff_for_mutant  # type: ignore

    out: dict = {"files": OrderedDict()}
    for meta_path in sorted(glob.glob(os.path.join("mutants", "**", "*.meta"), recursive=True)):
        rel = os.path.relpath(meta_path[: -len(".meta")], "mutants")  # app/services/ownership.py
        meta = json.load(open(meta_path, encoding="utf-8"))
        codes = meta.get("exit_code_by_key", {})
        if not codes:
            continue
        counts = Counter(STATUS.get(c, "suspicious") for c in codes.values())
        src_lines = open(rel, encoding="utf-8").read().splitlines()
        survivors = []
        for name, code in codes.items():
            st = STATUS.get(code, "suspicious")
            if st not in ("survived", "timeout", "suspicious"):
                continue
            mangled = name.rsplit(".", 1)[-1]
            cls, fn = _mangled_to_def(mangled)
            def_line = _def_line(src_lines, cls, fn)
            try:
                diff = get_diff_for_mutant(name, path=rel)  # mutmut prepends mutants/ itself
            except Exception as exc:  # noqa: BLE001 — a missing index entry must not stop the dump
                diff = f"(diff unavailable: {exc})"
            old = new = ""
            line = def_line
            hunk_old = 0
            seen = 0
            for ln in diff.splitlines():
                m = re.match(r"@@ -(\d+)", ln)
                if m:
                    hunk_old, seen = int(m.group(1)), 0
                    continue
                if ln.startswith("---") or ln.startswith("+++"):
                    continue
                if ln.startswith("-") and not old:
                    old = ln[1:]
                    if def_line is not None:
                        line = def_line + hunk_old - 1 + seen
                elif ln.startswith("+") and not new:
                    new = ln[1:]
                if ln.startswith((" ", "-")):
                    seen += 1
            survivors.append({"name": name, "status": st, "function": (cls + "." if cls else "") + fn, "line": line,
                              "operator": _operator(old, new), "old": old.strip(), "new": new.strip()})
        survivors.sort(key=lambda s: (s["line"] or 0, s["name"]))
        out["files"][rel] = {"counts": dict(counts), "total": len(codes), "survivors": survivors}
    return out


def collect_backend() -> dict | None:
    backend = os.path.join(REPO, "backend")
    if not glob.glob(os.path.join(backend, "mutants", "**", "*.meta"), recursive=True):
        return None
    proc = subprocess.run(["uv", "run", "python", os.path.abspath(__file__), "--dump-backend"], cwd=backend,
                          capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr[-2000:])
        return None
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------- frontend (Stryker)


def collect_frontend() -> dict | None:
    path = os.path.join(REPO, "frontend", "reports", "mutation", "mutation.json")
    if not os.path.exists(path):
        return None
    data = json.load(open(path, encoding="utf-8"))
    out: dict = {"files": OrderedDict()}
    for rel, fv in sorted(data.get("files", {}).items()):
        src = fv.get("source", "").splitlines()
        counts = Counter()
        survivors = []
        for m in fv.get("mutants", []):
            st = {"Killed": "killed", "Survived": "survived", "NoCoverage": "no tests", "Timeout": "timeout",
                  "RuntimeError": "error", "CompileError": "error", "Ignored": "skipped"}.get(m["status"], m["status"].lower())
            counts[st] += 1
            if st in ("survived", "timeout"):
                s, e = m["location"]["start"], m["location"]["end"]
                if s["line"] == e["line"] and s["line"] - 1 < len(src):
                    old = src[s["line"] - 1][s["column"] - 1:e["column"] - 1]
                else:
                    old = (src[s["line"] - 1][s["column"] - 1:] if s["line"] - 1 < len(src) else "") + " …"
                survivors.append({"line": s["line"], "status": st, "operator": m["mutatorName"], "old": old.strip(),
                                  "new": (m.get("replacement") or "").strip().replace("\n", " ")})
        survivors.sort(key=lambda s: s["line"])
        out["files"][rel] = {"counts": dict(counts), "total": len(fv.get("mutants", [])), "survivors": survivors}
    return out


# --------------------------------------------------------------------------- markdown


def _pct(n, d):
    return f"{100.0 * n / d:.1f} %" if d else "—"


def score_table(side: dict, uncovered_label: str) -> list[str]:
    """`mutants` = every generated mutant; `ignored` (Stryker static mutants, mutmut skipped) are not in any denominator."""
    L = [f"| file | mutants | ignored | pending | killed | survived | timeout | {uncovered_label} | score | score (covered) |", "|---|---|---|---|---|---|---|---|---|---|"]
    tot = Counter()
    for rel, fv in side["files"].items():
        c = fv["counts"]
        k, s, t, u = c.get("killed", 0), c.get("survived", 0) + c.get("suspicious", 0), c.get("timeout", 0), c.get("no tests", 0)
        g, pe = c.get("skipped", 0), c.get("not checked", 0)  # pending = an interrupted run's unvisited mutants
        tot.update({"m": fv["total"], "g": g, "p": pe, "k": k, "s": s, "t": t, "u": u})
        L.append(f"| `{rel}` | {fv['total']} | {g} | {pe} | {k} | {s} | {t} | {u} | {_pct(k, k + s + t + u)} | {_pct(k, k + s + t)} |")
    L.append(f"| **total** | {tot['m']} | {tot['g']} | {tot['p']} | {tot['k']} | {tot['s']} | {tot['t']} | {tot['u']} | **{_pct(tot['k'], tot['k'] + tot['s'] + tot['t'] + tot['u'])}** | {_pct(tot['k'], tot['k'] + tot['s'] + tot['t'])} |")
    return L


def _cell(s: str, width: int = 90) -> str:
    s = s.replace("|", "\\|").replace("`", "'")
    return s if len(s) <= width else s[: width - 1] + "…"


def survivors_table(side: dict, with_function: bool) -> list[str]:
    L = ["| mutant | status | operator | original → mutant |", "|---|---|---|---|"]
    for rel, fv in side["files"].items():
        for s in fv["survivors"]:
            where = f"`{rel}:{s['line'] if s['line'] is not None else '?'}`" + (f" {s['function']}" if with_function and s.get("function") else "")
            L.append(f"| {where} | {s['status']} | {s['operator']} | `{_cell(s['old'])}` → `{_cell(s['new'])}` |")
    return L


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dump-backend", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="print the collected data as JSON instead of markdown")
    args = ap.parse_args(argv)
    if args.dump_backend:
        print(json.dumps(dump_backend(), ensure_ascii=False))
        return 0
    be, fe = collect_backend(), collect_frontend()
    if args.json:
        print(json.dumps({"backend": be, "frontend": fe}, ensure_ascii=False, indent=1))
        return 0
    L: list[str] = []
    if be:
        L += ["## Backend (mutmut) — per file", ""] + score_table(be, "no tests") + [""]
    if fe:
        L += ["## Frontend (Stryker) — per file", ""] + score_table(fe, "no coverage") + [""]
    if be:
        L += ["## Appendix A — surviving backend mutants", ""] + survivors_table(be, True) + [""]
    if fe:
        L += ["## Appendix B — surviving frontend mutants", ""] + survivors_table(fe, False) + [""]
    if not L:
        L = ["No mutation results found (run `make mutation` / `make mutation-frontend` first)."]
    print("\n".join(L))
    return 0


if __name__ == "__main__":
    sys.exit(main())
