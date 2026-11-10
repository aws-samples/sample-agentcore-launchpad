"""r17-bm — TypeScript agents are extracted by Global AgentCore Evaluations' TypeScript rules
(``supported-frameworks-langgraph`` "TypeScript agent support", ``supported-frameworks-strands``).

Fixture ``tests/fixtures/langgraph/typescript.json`` (provenance in ``build_fixtures.py``): one
real 3-turn session per TypeScript agent — Strands Agents TS, and a LangGraph.js
``createReactAgent`` under each of Global's three TypeScript LangChain instrumentations (scopes
``@aws/aws-distro-opentelemetry-instrumentation-langchain``,
``@arizeai/openinference-instrumentation-langchain``, ``@traceloop/instrumentation-langchain``).
Before the fix only the Python LangGraph scopes were known: the OpenInference session had no
convertible span, the Traceloop one gave every turn no prompt and an empty answer, and the
readiness gate never paired a Global-shape ADOT root (content only on its ``chat`` spans).
The truth is the agent's own reply per turn and the deterministic tools (which echo their
arguments).

``typescript_runtime.json`` holds the same four agents' sessions from AgentCore Runtime
``aws/spans`` (t1, exported through a TEST-ONLY SigV4 exporter, N17-obs459): the Runtime shape
the mapper must follow. Every extraction and readiness case runs on both sets.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.telemetry import probe_latest_session
from app.services.strands_eval.langgraph_mapper import LANGGRAPH_SCOPES, is_langgraph
from tests.evaluation.test_langgraph_extraction import _data, _turns

_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "langgraph"
FIXTURE = json.loads((_DIR / "typescript.json").read_text())["cases"]
RUNTIME = json.loads((_DIR / "typescript_runtime.json").read_text())["cases"]
SETS = {"capture": FIXTURE, "runtime": RUNTIME}
PROMPTS = ["hello, what can you do?", "Find me flights SEA to NYC on 2025-03-15",
           "And the weather in NYC?"]
TOOLS = [[], ["search_flights"], ["get_weather"]]
ADOT, OPENINFERENCE, TRACELOOP = ("@aws/aws-distro-opentelemetry-instrumentation-langchain",
                                  "@arizeai/openinference-instrumentation-langchain",
                                  "@traceloop/instrumentation-langchain")


def _check(case: dict[str, Any], turns: list[dict[str, Any]]) -> None:
    assert [t["prompt"] for t in turns] == PROMPTS
    assert [t["answer"].strip() for t in turns] == [r.strip() for r in case["replies"]]
    assert [[name for name, *_ in t["tools"]] for t in turns] == TOOLS
    for name, arguments, result, call_id in (x for t in turns for x in t["tools"]):
        echoed = json.loads(result)  # the tools echo their arguments
        assert arguments == {k: v for k, v in echoed.items() if k in arguments}, name
        assert set(arguments) == ({"origin", "destination", "date"} if name == "search_flights"
                                  else {"city"})
        assert call_id and call_id.startswith("call_")


@pytest.mark.parametrize("source", sorted(SETS))
def test_the_typescript_langchain_scopes_are_langgraph_scopes(source):
    assert {ADOT, OPENINFERENCE, TRACELOOP} <= LANGGRAPH_SCOPES
    for variant in ("lg-adot", "lg-openinference", "lg-traceloop"):
        assert is_langgraph(SETS[source][variant]["records"])
    assert not is_langgraph(SETS[source]["strands-ts"]["records"])  # scope strands-agents


@pytest.mark.parametrize("source", sorted(SETS))
@pytest.mark.parametrize("variant", ["strands-ts", "lg-adot", "lg-openinference",
                                     "lg-traceloop"])
def test_each_typescript_session_maps_turn_by_turn(variant, source):
    case = SETS[source][variant]
    _check(case, _turns(_data(case["records"], case["session_id"], "unified")))


def _container_only(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Global's ADOT example (0.12): the invoke_agent span carries no conversation."""
    out = copy.deepcopy(records)
    for record in out:
        if (record.get("attributes") or {}).get("gen_ai.operation.name") == "invoke_agent":
            for key in ("gen_ai.input.messages", "gen_ai.output.messages"):
                record["attributes"].pop(key, None)
    return out


@pytest.mark.parametrize("source", sorted(SETS))
def test_an_adot_agent_span_without_content_takes_the_turn_from_its_chat_spans(source):
    case = SETS[source]["lg-adot"]
    _check(case, _turns(_data(_container_only(case["records"]), case["session_id"],
                              "unified")))


def test_reasoning_parts_are_not_the_answer():
    # GLM writes a reasoning part after the text in the same assistant message
    case = FIXTURE["lg-adot"]
    root = next(r for r in case["records"] if r["name"] == "invoke_agent LangGraph"
                and '"reasoning"' in r["attributes"].get("gen_ai.output.messages", ""))
    turns = _turns(_data(case["records"], case["session_id"], "unified"))
    assert all("reasoning" not in t["answer"].casefold() for t in turns)
    assert root  # the probe exists in the real records


@pytest.mark.parametrize("source", sorted(SETS))
def test_a_traceloop_tool_is_named_by_its_tool_message_not_the_langchain_class(source):
    case = SETS[source]["lg-traceloop"]
    tools = [r for r in case["records"]
             if (r.get("attributes") or {}).get("traceloop.span.kind") == "task"]
    assert {r["attributes"]["traceloop.entity.name"] for r in tools} == {"DynamicStructuredTool"}
    assert all(json.loads(r["attributes"]["traceloop.entity.input"]).keys() == {"args"}
               for r in tools)
    _check(case, _turns(_data(case["records"], case["session_id"], "unified")))


# ── readiness (app.evaluation.telemetry) ────────────────────────────────────────────────


class _Logs:
    def __init__(self, spans: list[dict[str, Any]]) -> None:
        self.spans = spans

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        return {"events": self.spans if kwargs["logGroupName"] == "aws/spans" else []}


def _events(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"message": json.dumps(r), "timestamp": r["endTimeUnixNano"] // 1_000_000,
             "ingestionTime": r["endTimeUnixNano"] // 1_000_000 + 500} for r in records]


@pytest.mark.parametrize(("variant", "framework"), [
    ("strands-ts", "strands"), ("lg-adot", "genai"), ("lg-openinference", "openinference"),
    ("lg-traceloop", "traceloop")])
@pytest.mark.parametrize("source", sorted(SETS))
def test_the_readiness_gate_pairs_each_typescript_session(variant, framework, source):
    case = SETS[source][variant]
    probe = probe_latest_session(_Logs(_events(case["records"])),
                                 session_id=case["session_id"],
                                 content_log_group="/aws/bedrock-agentcore/runtimes/ts-DEFAULT")
    assert (probe.paired, probe.framework) == (True, framework), probe.detail


@pytest.mark.parametrize("source", sorted(SETS))
def test_the_readiness_gate_pairs_an_adot_root_whose_content_is_on_its_chat_spans(source):
    case = SETS[source]["lg-adot"]
    records = _container_only(case["records"])
    probe = probe_latest_session(_Logs(_events(records)), session_id=case["session_id"],
                                 content_log_group="/aws/bedrock-agentcore/runtimes/ts-DEFAULT")
    last_root = max((r for r in records if r["name"] == "invoke_agent LangGraph"),
                    key=lambda r: r["endTimeUnixNano"])
    assert (probe.paired, probe.root_span_id) == (True, last_root["spanId"]), probe.detail
    # the watermark is the newest content-carrying span of the root's trace
    chats = [r for r in records if r["traceId"] == last_root["traceId"]
             and "gen_ai.output.messages" in r["attributes"]]
    assert probe.content_ingestion_ms == max(r["endTimeUnixNano"] // 1_000_000 + 500
                                             for r in chats)
