"""Rebuild the LangGraph extraction fixtures (r17-bf7) from their raw sources.

- ``doc_examples.json`` — Global AgentCore Evaluations' ``supported-frameworks-langgraph``
  example spans (both instrumentation libraries × both delivery modes, fetched 2026-10-23)
  plus probes the page omits (a turn that ends after a tool call, a checkpointer's replayed
  history, block-list content): ``/tmp/audit-eval-parity/harness/cases*.py``.
- ``live_create_agent.json`` — a real BYOC LangGraph ``create_agent`` session on AgentCore
  Runtime (ADOT + opentelemetry-instrumentation-langchain 0.62, 3 turns, the third ends on a
  ``return_direct`` tool): ``/tmp/audit-eval-parity/live/raw``.
- ``live_return_direct.json`` — eval-wb's live BYOC LangGraph session
  ``lg-live-19ed4cefd634459f930db419b8c0debf`` (Round 3 close, 2026-10-24; T3 ends on its
  ``return_direct`` weather tool — N17-obs412), from the eval-wb session cache.
- ``typescript.json`` — r17-bm: one 3-turn session per TypeScript agent (Strands Agents TS
  ``@strands-agents/sdk`` 1.22 and a LangGraph.js ``createReactAgent`` under each of Global's three
  TypeScript LangChain instrumentations: ADOT Node 0.13's own, ``@arizeai/…-langchain`` 4.1.5,
  ``@traceloop/instrumentation-langchain`` 0.27.0), GLM-5.3, run locally under ADOT Node with
  the OTLP traces exporter pointed at an aws/spans-shaped sink
  (``designs/eval-ts-20261026/scripts/otlp_sink.py``) — ADOT Node cannot export from
  cn-northwest-1 (evaluation-engine.md §33), so no Runtime records exist. Each case carries
  the agent's own replies (the truth for the answers).
- ``typescript_runtime.json`` — the same four agents deployed as BYOC containers on AgentCore
  Runtime (t1, 2026-10-26), one 3-turn session each from ``aws/spans`` (eval workbench cache
  ``designs/eval-ts-20261026/cache-b``). They export only through a TEST-ONLY SigV4 exporter in
  the test agents (``obs459-testonly.cjs``, never in templates); the truth is the reply the
  console streamed (``raw/sessions-b.jsonl``).
- ``deepagents_{split,unified}.json`` — a Deep Agents (deepagents 0.7) agent with
  ``SkillsMiddleware`` over a real filesystem, a scripted offline model, the same
  instrumentation; split records made by ADOT's own ``LLOHandler``
  (``make_deepagents.py``). Middleware node spans other than ``SkillsMiddleware``'s are
  dropped (size); the mapper ignores node spans either way.

Account ids are masked and resources reduced to ``service.name``.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
AUDIT = Path("/tmp/audit-eval-parity")
DEEPAGENTS = Path("/tmp/r17bf7-da")
EVAL_WB_SESSION = (HERE.parents[4] / "eval-wb/backend/.eval-cache/sessions"
                   / "lg-live-19ed4cefd634459f930db419b8c0debf.json")
TS_CAPTURE = Path("/home/ubuntu/designs/eval-ts-20261026/capture")
DOC_CASES = ("langgraph-otel-split", "langgraph-otel-unified", "langgraph-oi-split",
             "langgraph-oi-unified")
_ACCOUNT = re.compile(r"\b\d{12}\b")


def _clean(record: dict) -> dict:
    record = dict(record)
    service = ((record.get("resource") or {}).get("attributes") or {}).get("service.name")
    record.pop("resource", None)
    if service:
        record["resource"] = {"attributes": {"service.name": service}}
    return json.loads(_ACCOUNT.sub("000000000000", json.dumps(record, ensure_ascii=False)))


def _doc_examples() -> dict:
    sys.path.insert(0, str(AUDIT / "harness"))
    from cases import CASES, SID  # noqa: PLC0415
    from cases_extra import X  # noqa: PLC0415

    cases = {name: CASES[name] for name in DOC_CASES}
    cases.update({name: case for name, case in X.items()})
    return {"session_id": SID, "cases": {
        name: {"records": case["records"]} for name, case in cases.items()}}


def _live() -> dict:
    sid = (AUDIT / "live/sid.txt").read_text().strip()
    rt = [json.loads(e["message"]) for e in json.loads(
        (AUDIT / "live/raw/rt.json").read_text())["events"]]
    spans = [json.loads(e["message"]) for e in json.loads(
        (AUDIT / "live/raw/spans.json").read_text())["events"]]
    rt = [r for r in rt if (r.get("attributes") or {}).get("session.id") == sid]
    records = sorted(spans + rt, key=lambda r: r.get("endTimeUnixNano")
                     or r.get("timeUnixNano") or 0)
    return {"session_id": sid, "records": [_clean(r) for r in records]}


def _live_return_direct() -> dict:
    cached = json.loads(EVAL_WB_SESSION.read_text())
    records = [x["record"] for x in sorted(cached["records"], key=lambda x: x["timestamp"])]
    return {"session_id": cached["session_id"], "records": [_clean(r) for r in records]}


def _deepagents(name: str) -> dict:
    records = json.loads((DEEPAGENTS / f"{name}.json").read_text())
    dropped = {r["spanId"] for r in records if "startTimeUnixNano" in r
               and "Middleware." in r["name"] and "SkillsMiddleware." not in r["name"]}
    kept = [_clean(r) for r in records if r.get("spanId") not in dropped]
    sid = next(r["attributes"]["session.id"] for r in kept)
    return {"session_id": sid, "records": kept}


def _typescript() -> dict:
    sessions = dict(line.split() for line in (TS_CAPTURE / "sessions.txt").read_text().splitlines())
    cases = {}
    for variant, sid in sessions.items():
        records = [json.loads(line) for line in (TS_CAPTURE / f"{variant}.jsonl").open()]
        replies = [json.loads(line)["result"]
                   for line in (TS_CAPTURE / f"{variant}.replies.jsonl").open() if line.strip()]
        records.sort(key=lambda r: r.get("endTimeUnixNano") or 0)
        cases[variant] = {"session_id": sid, "replies": replies,
                          "records": [_clean(r) for r in records]}
    return {"cases": cases}


def _typescript_runtime() -> dict:
    cases = {}
    for line in (TS_CAPTURE.parent / "raw/sessions-b.jsonl").open():
        session = json.loads(line)
        cached = json.loads((TS_CAPTURE.parent / "cache-b/sessions"
                             / f"{session['session_id']}.json").read_text())
        records = [x["record"] for x in sorted(cached["records"], key=lambda x: x["timestamp"])]
        cases[session["tags"]["framework"]] = {
            "session_id": session["session_id"],
            "replies": [t["delta_text"] for t in session["turns"]],
            "records": [_clean(r) for r in records]}
    return {"cases": cases}


def main() -> None:
    out = {"doc_examples.json": _doc_examples(), "live_create_agent.json": _live(),
           "live_return_direct.json": _live_return_direct(),
           "deepagents_split.json": _deepagents("split"),
           "deepagents_unified.json": _deepagents("unified"),
           "typescript.json": _typescript(),
           "typescript_runtime.json": _typescript_runtime()}
    for name, data in out.items():
        (HERE / name).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")


if __name__ == "__main__":
    main()
