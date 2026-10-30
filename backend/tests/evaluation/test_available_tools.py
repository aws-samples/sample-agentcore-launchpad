"""Audit item 10 — ``available_tools`` lists every tool the agent had, with its description.

Global (create-evaluator): ``available_tools`` is "The set of available tool calls across each
turn, including tool ID, parameters, and description". The mapper listed only the tools the
agent CALLED, by name, so ToolSelectionAccuracy could not see a better tool the agent skipped
(inflated) and GoalSuccessRate / custom judges read a tool list without descriptions. The
real records carry the full list: ``gen_ai.agent.tools`` on Strands' ``invoke_agent`` span,
``gen_ai.tool.definitions`` (OpenAI-style) and ``gen_ai.tool.description`` on each
``execute_tool`` span.
"""

import copy
import json
from pathlib import Path

from strands_evals.types.trace import AgentInvocationSpan, ToolConfig

from app.services.strands_eval import engine_core, tool_catalog
from app.services.strands_eval import evaluators as ev
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_final_answer_output import _GroupLogs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REAL = json.loads((FIXTURES / "obs315_preamble_session.json").read_text(encoding="utf-8"))
ATTRIBUTES = json.loads((FIXTURES / "tool_attributes_257f17b16cbb.json")
                        .read_text(encoding="utf-8"))["attributes"]
# gen_ai.agent.tools of the real invoke_agent span: 7 tools, of which the turn called 2
AGENT_TOOLS = ["code_interpreter", "shell", "file_operations", "kb_search", "kb_deep_search",
               "launchpad_gw_hr-database___get_employee", "skills"]
CALLED = ["kb_search", "launchpad_gw_hr-database___get_employee"]


def _records(with_attributes: bool = True) -> list[dict]:
    records = copy.deepcopy(REAL["records"])
    if with_attributes:
        for x in records:
            extra = ATTRIBUTES.get(x["record"].get("spanId"))
            if extra and x["group"] == SPANS_LOG_GROUP:
                x["record"].setdefault("attributes", {}).update(extra)
    return records


def _data(records, mode="both"):
    groups = [REAL["log_group"], SPANS_LOG_GROUP] if mode == "both" else [REAL["log_group"]]
    return LaunchpadSpansProvider(_GroupLogs(records), groups).get_evaluation_data(
        REAL["session_id"])


def _agent_tools(data) -> list[list[ToolConfig]]:
    return [s.available_tools for t in data["trajectory"].traces for s in t.spans
            if isinstance(s, AgentInvocationSpan)]


def test_the_real_session_lists_every_agent_tool_not_only_the_called_ones():
    (tools,) = _agent_tools(_data(_records()))
    assert sorted(t.name for t in tools) == sorted(AGENT_TOOLS)
    by_name = {t.name: t for t in tools}
    assert by_name["kb_search"].description.startswith(
        "Search the mounted knowledge bases with one k-NN similarity search")
    assert by_name["launchpad_gw_hr-database___get_employee"].description.startswith(
        "Look up an employee record by employee id")
    assert by_name["kb_deep_search"].description is None  # never called: no description


def test_without_span_attributes_the_called_tools_are_the_fallback():
    (tools,) = _agent_tools(_data(_records(with_attributes=False)))
    assert sorted(t.name for t in tools) == sorted(CALLED)
    (runtime_only,) = _agent_tools(_data(_records(), mode="runtime_only"))
    assert sorted(t.name for t in runtime_only) == sorted(CALLED)


def test_tool_selection_accuracy_sees_the_tools_the_agent_skipped():
    data = _data(_records())
    case = engine_core.evaluation_data(REAL["session_id"], data, None)
    judge = FakeModel(structured=[{"reasoning": "ok", "score": "Yes"}] * 4)
    ev.build({"id": "Builtin.ToolSelectionAccuracy"}, judge).evaluate(case)
    sent = json.dumps([c.get("messages") for c in judge.calls], ensure_ascii=False)
    assert "## Available tool-calls" in sent
    for skipped in ("kb_deep_search", "code_interpreter", "skills"):
        assert skipped in sent


def test_the_custom_judge_placeholder_renders_names_and_descriptions():
    from app.services.strands_eval import judge_placeholders as jp

    case = engine_core.evaluation_data(REAL["session_id"], _data(_records()), None)
    rendered = jp.render_placeholders("{available_tools}", case)
    for name in AGENT_TOOLS:
        assert name in rendered
    assert "Look up an employee record by employee id" in rendered


def _span(trace, name, attributes, op):
    return {"traceId": trace, "spanId": f"{trace}-{name}", "name": name,
            "attributes": {"gen_ai.operation.name": op, **attributes}}


def test_openai_style_definitions_carry_descriptions_and_parameters():
    spans = [_span("t1", "invoke_agent", {"gen_ai.tool.definitions": json.dumps([
        {"type": "function", "function": {"name": "weather", "description": "Forecast",
                                          "parameters": {"type": "object"}}},
        {"name": "calculator", "description": "Arithmetic"},
    ])}, "invoke_agent")]
    tools = {t.name: t for t in tool_catalog.build(spans).tools()}
    assert tools["weather"].description == "Forecast"
    assert tools["weather"].parameters == {"type": "object"}
    assert tools["calculator"].description == "Arithmetic"


def test_a_malformed_tool_list_is_ignored_not_fatal():
    spans = [_span("t1", "invoke_agent", {"gen_ai.agent.tools": "[not json"}, "invoke_agent")]
    assert not tool_catalog.build(spans)
    assert tool_catalog.build(spans).tools() == []


def test_the_session_list_is_merged_across_turns():
    spans = [
        _span("t1", "invoke_agent", {"gen_ai.agent.tools": json.dumps(["a", "b"])},
              "invoke_agent"),
        _span("t1", "b", {"gen_ai.tool.name": "b", "gen_ai.tool.description": "B tool"},
              "execute_tool"),
        _span("t2", "invoke_agent", {"gen_ai.agent.tools": json.dumps(["b"]),
                                     "gen_ai.tool.definitions": json.dumps(
                                         [{"name": "c", "parameters": {"type": "object"}}])},
              "invoke_agent"),
    ]
    by_name = {t.name: t for t in tool_catalog.build(spans).tools()}
    assert sorted(by_name) == ["a", "b", "c"]
    assert by_name["b"].description == "B tool"
    assert by_name["c"].parameters == {"type": "object"}
