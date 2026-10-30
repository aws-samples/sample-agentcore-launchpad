"""r17-bf7 — LangGraph sessions are extracted by Global AgentCore Evaluations' LangGraph
rules (``supported-frameworks-langgraph``), in every delivery mode.

Fixtures (``tests/fixtures/langgraph``, provenance in ``build_fixtures.py``): Global's doc
example spans (OpenTelemetry / OpenInference × split / unified) with probes the page omits,
a real BYOC ``create_agent`` session from AgentCore Runtime (its third turn ends on a
``return_direct`` tool) and a Deep Agents session that loads a skill with ``read_file`` in
turns 1 and 3. The library's LangChain mappers missed the tool call of a split turn, found
no session in unified OpenTelemetry / both OpenInference modes or without span records
(``runtime_logs``), and answered a turn that ended after its tool call with the USER's
prompt.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from strands_evals.types.trace import AgentInvocationSpan, ToolExecutionSpan

from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation import corpus_support as cs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "langgraph"
GROUP = "/aws/bedrock-agentcore/runtimes/lg-DEFAULT"
DOC = json.loads((FIXTURES / "doc_examples.json").read_text())
LIVE = json.loads((FIXTURES / "live_create_agent.json").read_text())
RETURN_DIRECT = json.loads((FIXTURES / "live_return_direct.json").read_text())
DEEP = {name: json.loads((FIXTURES / f"deepagents_{name}.json").read_text())
        for name in ("split", "unified")}
DOC_PROMPT = "Hey, how can you help me"
DOC_ANSWER = "Hello! I'm your travel planning assistant ..."
DOC_ARGS = {"origin": "SEA", "destination": "NYC", "date": "2025-03-15"}


def _is_span(record: dict[str, Any]) -> bool:
    return "startTimeUnixNano" in record


def _data(records: list[dict[str, Any]], session_id: str, mode: str) -> Any:
    """``both`` (runtime group + ``aws/spans``), ``runtime_only`` (``spans_source=
    runtime_logs``: event records only) or ``unified`` (span records only)."""
    return _provider(records, mode).get_evaluation_data(session_id)


def _provider(records: list[dict[str, Any]], mode: str) -> LaunchpadSpansProvider:
    if mode == "runtime_only":
        records = [r for r in records if not _is_span(r)]
    elif mode == "unified":
        records = [r for r in records if _is_span(r)]
    rows = [{"group": SPANS_LOG_GROUP if _is_span(r) else "runtime",
             "timestamp": r.get("endTimeUnixNano") or r.get("timeUnixNano") or 0,
             "record": r} for r in records]
    if mode == "runtime_only":
        return LaunchpadSpansProvider(cs.GroupLogs(rows), [GROUP], spans_source="runtime_logs")
    return LaunchpadSpansProvider(cs.GroupLogs(rows), [GROUP, SPANS_LOG_GROUP])


def _turns(data: Any) -> list[dict[str, Any]]:
    out = []
    for trace in data["trajectory"].traces:
        agents = [s for s in trace.spans if isinstance(s, AgentInvocationSpan)]
        tools = [s for s in trace.spans if isinstance(s, ToolExecutionSpan)]
        out.append({"prompt": agents[0].user_prompt if agents else None,
                    "answer": agents[0].agent_response if agents else None,
                    "metadata": agents[0].metadata if agents else None,
                    "tools": [(t.tool_call.name, t.tool_call.arguments, t.tool_result.content,
                               t.tool_call.tool_call_id) for t in tools]})
    return out


_DOC_MODES = [(case, mode) for case in ("langgraph-otel-split", "langgraph-oi-split")
              for mode in ("both", "runtime_only")] + [
    ("langgraph-otel-unified", "unified"), ("langgraph-oi-unified", "unified")]


@pytest.mark.parametrize(("case", "mode"), _DOC_MODES)
def test_global_doc_example_spans_give_prompt_answer_and_tool_call(case, mode):
    data = _data(DOC["cases"][case]["records"], DOC["session_id"], mode)
    turns = _turns(data)
    agents = [t for t in turns if t["prompt"] is not None]
    assert [(t["prompt"], t["answer"]) for t in agents] == [(DOC_PROMPT, DOC_ANSWER)]
    tools = [tool for t in turns for tool in t["tools"]]
    assert [(name, args) for name, args, _r, _id in tools] == [("search_flights", DOC_ARGS)]
    assert '"flights"' in tools[0][2]
    assert data["output"] == DOC_ANSWER


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_a_turn_that_ends_on_its_tool_result_answers_that_result_not_the_user_prompt(mode):
    # N17-obs412 (user ruling): the graph ended on the ToolMessage — that is what the user saw
    case = DOC["cases"]["lg-2turn-ends-after-tool"]
    turns = _turns(_data(case["records"], DOC["session_id"], mode))
    assert [t["prompt"] for t in turns] == ["Hey, how can you help me",
                                            "Find me flights SEA to NYC on 2025-03-15"]
    assert turns[0]["answer"] == "TURN-1 ANSWER: I can plan trips."
    assert turns[1]["answer"] == '{"origin": "SEA", "destination": "NYC", "flights": [ ... ]}'


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_a_turn_with_no_message_after_its_prompt_answers_nothing(mode):
    # only a turn that produced nothing answers "" — never the prompt nor an earlier answer
    records = copy.deepcopy(DOC["cases"]["lg-2turn-ends-after-tool"]["records"])
    last = max((r for r in records if _is_span(r)), key=lambda r: r["startTimeUnixNano"])
    body = next(r for r in records if not _is_span(r) and r["spanId"] == last["spanId"])["body"]
    out = body["output"]["messages"][0]
    state = json.loads(out["content"])
    state["outputs"]["messages"] = state["outputs"]["messages"][:3]  # H1, A1, H2
    out["content"] = json.dumps(state)
    turns = _turns(_data(records, DOC["session_id"], mode))
    assert [t["answer"] for t in turns] == ["TURN-1 ANSWER: I can plan trips.", ""]


@pytest.mark.parametrize("mode", ["both", "runtime_only"])
def test_replayed_history_and_reasoning_blocks_are_not_the_answer(mode):
    final = _turns(_data(DOC["cases"]["lg-2turn-final"]["records"], DOC["session_id"], mode))
    assert [t["answer"] for t in final] == ["TURN-1 ANSWER: I can plan trips.",
                                            "TURN-2 FINAL: Here are 3 flights ..."]
    blocks = _turns(_data(DOC["cases"]["lg-final-blocks"]["records"], DOC["session_id"], mode))
    assert [t["answer"] for t in blocks] == ["TURN-2 FINAL: blocks"]


LIVE_TURNS = [
    ("hello, what can you do?", "ANSWER-T1: I can search flights and check the weather.", []),
    ("Find me flights SEA to NYC on 2025-03-15",
     "FINAL-T2: two flights SEA->NYC on 2025-03-15: AL-101 08:00 and AL-205 13:30.",
     [("search_flights", DOC_ARGS, "call_flights_1")]),
    # return_direct: the state ends on the ToolMessage — the user saw the tool's result
    ("And the weather in NYC?", '{"city": "NYC", "weather": "sunny", "temp_c": 21}',
     [("get_weather", {"city": "NYC"}, "call_weather_1")]),
]


@pytest.mark.parametrize("mode", ["both", "runtime_only", "unified"])
def test_the_live_byoc_langgraph_session_maps_turn_by_turn(mode):
    data = _data(LIVE["records"], LIVE["session_id"], mode)
    turns = _turns(data)
    assert [(t["prompt"], t["answer"], [(n, a, i) for n, a, _r, i in t["tools"]])
            for t in turns] == LIVE_TURNS
    assert "AL-101 08:00" in turns[1]["tools"][0][2]
    assert data["output"] == LIVE_TURNS[-1][1]
    assert all(not (t["metadata"] or {}).get("span_status") == "ERROR" for t in turns)
    if mode != "runtime_only":  # the catalogue: every tool the agent ran, with descriptions
        agent = next(s for s in data["trajectory"].traces[0].spans
                     if isinstance(s, AgentInvocationSpan))
        assert {t.name: t.description for t in agent.available_tools} == {
            "search_flights": "Search for available flights between cities.",
            "get_weather": "Current weather for a city."}


@pytest.mark.parametrize("mode", ["both", "runtime_only", "unified"])
def test_a_return_direct_turn_answers_the_tool_result_the_user_saw(mode):
    # N17-obs412, eval-wb's live session lg-live-19ed4cef… T3. Deliberate deviation from Global:
    # Global took T2's answer ("FINAL-T2: two flights…") from the replayed history and judged
    # it as T3's; the user saw the weather tool's JSON
    data = _data(RETURN_DIRECT["records"], RETURN_DIRECT["session_id"], mode)
    turns = _turns(data)
    assert [(t["prompt"], t["answer"]) for t in turns] == [(p, a) for p, a, _t in LIVE_TURNS]
    assert not turns[2]["answer"].startswith("FINAL-T2")
    assert data["output"] == '{"city": "NYC", "weather": "sunny", "temp_c": 21}'


def test_an_errored_langgraph_turn_is_a_failed_turn():
    records = copy.deepcopy(LIVE["records"])
    last_trace = max((r for r in records if _is_span(r)),
                     key=lambda r: r["startTimeUnixNano"])["traceId"]
    workflow = next(r for r in records if _is_span(r) and r["traceId"] == last_trace
                    and r["name"].endswith(".workflow"))
    workflow["status"] = {"code": "ERROR"}
    turns = _turns(_data(records, LIVE["session_id"], "both"))
    assert [(t["metadata"] or {}).get("span_status") for t in turns] == ["UNSET", "UNSET",
                                                                         "ERROR"]
    # without span records: the graph never returned its state
    record = next(r for r in records if not _is_span(r)
                  and r.get("spanId") == workflow["spanId"])
    record["body"] = {"input": record["body"]["input"]}
    turns = _turns(_data(records, LIVE["session_id"], "runtime_only"))
    assert [(t["metadata"] or {}).get("span_status") for t in turns] == [None, None, "ERROR"]


DEEP_MODES = [("split", "both"), ("split", "runtime_only"), ("unified", "unified")]


@pytest.mark.parametrize(("variant", "mode"), DEEP_MODES)
def test_deep_agents_skill_reads_are_skill_loads_of_their_turns(variant, mode):
    from strands_evals.extractors.skills import extract_skill_load_events

    fixture = DEEP[variant]
    data = _data(fixture["records"], fixture["session_id"], mode)
    turns = _turns(data)
    assert [(t["prompt"], t["answer"]) for t in turns] == [
        ("Write a haiku about autumn",
         "FINAL-T1: Leaves drift on cold wind / ... / -- haiku-writer v1"),
        ("thanks!", "ANSWER-T2: You're welcome."),
        ("Now a haiku about rain",
         "FINAL-T3: Leaves drift on cold wind / ... / -- haiku-writer v1")]
    assert [[(n, a, i) for n, a, _r, i in t["tools"]] for t in turns] == [
        [("read_file", {"file_path": "/skills/haiku-writer/SKILL.md", "limit": 1000},
          "call_read_1")], [],
        [("read_file", {"file_path": "/skills/haiku-writer/SKILL.md", "limit": 1000},
          "call_read_3")]]
    loads = extract_skill_load_events(data["trajectory"])
    assert [(e.name, e.status, e.call_id) for e in loads] == [
        ("haiku-writer", "loaded", "call_read_1"), ("haiku-writer", "loaded", "call_read_3")]
    assert all("End with the line" in (e.body or "") for e in loads)


@pytest.mark.parametrize(("variant", "mode"), DEEP_MODES)
def test_deep_agents_skill_results_are_one_per_load_on_the_triggering_turn(variant, mode):
    from app.services.strands_eval import engine_core
    from tests.evaluation.fakes import FakeJudge

    fixture = DEEP[variant]
    provider = _provider(fixture["records"], mode)
    traces = provider.get_evaluation_data(fixture["session_id"])["trajectory"].traces
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.SkillSelectionAccuracy"}],
                                          lambda *_a, **_k: judge)
    rows = engine_core.evaluate_session(provider, fixture["session_id"], evaluators, None,
                                        job_id="j", sleep=lambda _s: None)
    assert [r["trace_id"] for r in rows] == [traces[0].trace_id, traces[2].trace_id]
    assert all(r["tool_name"] == "read_file" for r in rows)
    prompts = [json.dumps(c["messages"], ensure_ascii=False) for c in judge.calls]
    assert len(prompts) == 2
    assert "Write a haiku about autumn" in prompts[0]
    assert "Now a haiku about rain" in prompts[1]
    assert "Now a haiku about rain" not in prompts[0]


def test_a_strands_session_with_langchain_records_stays_a_strands_session():
    # ADOT instruments LangChain in any process that imports it: a Strands agent calling a
    # LangChain tool writes both scopes — its answers still come from the Strands mapper
    corpus = cs.load_session(cs.corpus_files()[0].stem)
    plain = cs.provider_data(corpus, corpus["records"], "both")
    stray = copy.deepcopy(next(x for x in corpus["records"] if x["group"] == SPANS_LOG_GROUP))
    stray["record"].update(name="RunnableSequence.task", spanId="f" * 16,
                           scope={"name": "opentelemetry.instrumentation.langchain"})
    mixed = cs.provider_data(corpus, [*corpus["records"], stray], "both")
    assert mixed["output"] == plain["output"]
    assert [len(t.spans) for t in mixed["trajectory"].traces] == [
        len(t.spans) for t in plain["trajectory"].traces]
