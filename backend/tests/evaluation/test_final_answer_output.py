"""N17-obs315 — evaluation scores the agent's FINAL answer, not its pre-tool preamble.

Live shape (ADOT ``llo_handler._group_messages_by_type``): a Strands ``chat`` span's content
record files EVERY assistant message under ``body.output.messages`` — the history the span
replays (``gen_ai.assistant.message`` → ``{"content": …}``) before the span's own answer
(``gen_ai.choice`` → ``{"message": …, "finish_reason": …}``). strands-evals'
``CloudWatchSessionMapper`` returned the FIRST assistant text there: the preamble the model
wrote before its first tool call. Both ``actual_output`` and the TRACE judges'
``agent_response`` read it.

Replay path: ``runtime_invoke.invoke_text`` (dataset / simulated / canary replay, optimize
rollouts) returns the answer bubble the chat shows last — the text after the last tool call.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.strands_eval import engine_core, runtime_invoke
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation.fakes import FakeLogs

SID = "sess-" + "f" * 40
GROUP = "/aws/bedrock-agentcore/runtimes/travel-DEFAULT"
TRACE = "e" * 32
T0 = 1_760_000_000_000_000_000
PROMPT = "成都出差 5 天住宿标准是多少？"
PREAMBLE = "我先查一下差旅标准和您的职级信息"
FINAL = "已查到：成都住宿上限 450 元/晚，5 天出差可报销 4 晚，合计 1800 元。"
REFUSAL = "抱歉，EMP-2088 的薪资属于他人隐私信息，我无权提供。"


def _blocks(*items: dict[str, Any]) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _text(text: str) -> dict[str, Any]:
    return {"text": text}


def _tool_use(tid: str = "tu-1") -> dict[str, Any]:
    return {"toolUse": {"toolUseId": tid, "name": "kb_search", "input": {"q": "差旅标准"}}}


def _user(text: str) -> dict[str, Any]:
    return {"role": "user", "content": {"content": _blocks(_text(text))}}


def _history(*items: dict[str, Any]) -> dict[str, Any]:
    """A replayed assistant message (``gen_ai.assistant.message``) — ADOT files it as OUTPUT."""
    return {"role": "assistant", "content": {"content": _blocks(*items)}}


def _choice(*items: dict[str, Any], finish: str) -> dict[str, Any]:
    """The span's own answer (``gen_ai.choice``)."""
    return {"role": "assistant", "content": {"message": _blocks(*items), "finish_reason": finish}}


def _span(span_id: str, name: str, start_ms: int, parent: str | None = "agent") -> dict[str, Any]:
    rec: dict[str, Any] = {
        "traceId": TRACE, "spanId": span_id, "name": name, "kind": "INTERNAL",
        "startTimeUnixNano": T0 + start_ms * 1_000_000,
        "endTimeUnixNano": T0 + (start_ms + 1) * 1_000_000,
        "attributes": {"session.id": SID}, "scope": {"name": "strands.telemetry.tracer"},
    }
    if parent:
        rec["parentSpanId"] = parent
    return rec


def _content(span_id: str, at_ms: int, inputs: list[dict[str, Any]],
             outputs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "scope": {"name": "strands.telemetry.tracer"}, "traceId": TRACE, "spanId": span_id,
        "timeUnixNano": T0 + at_ms * 1_000_000, "attributes": {"session.id": SID},
        "body": {"input": {"messages": inputs}, "output": {"messages": outputs}},
    }


def _tool_session(final_blocks: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """preamble + toolUse → tool → final answer, in the live ADOT record shape."""
    tool_result = {"role": "tool", "content": {"content": _blocks(
        {"toolResult": {"toolUseId": "tu-1", "status": "success",
                        "content": [_text("二线城市 P2 住宿上限 450 元/晚")]}})}}
    final_text = "\n".join(b["text"] for b in final_blocks if "text" in b) + "\n"
    return [
        _span("root", "POST /invocations", 0, parent=None),
        _span("agent", "invoke_agent Strands Agents", 1, parent="root"),
        _span("llm1", "chat glm-5.3", 2),
        _span("tool1", "execute_tool kb_search", 4),
        _span("llm2", "chat glm-5.3", 6),
        _content("llm1", 3, [_user(PROMPT)],
                 [_choice(_text(PREAMBLE), _tool_use(), finish="tool_use")]),
        _content("tool1", 5, [], []),
        _content("llm2", 7, [_user(PROMPT), tool_result],
                 [_history(_text(PREAMBLE), _tool_use()),
                  _choice(*final_blocks, finish="end_turn")]),
        _content("agent", 8, [_user(PROMPT)],
                 [{"role": "assistant",
                   "content": {"message": final_text, "finish_reason": "end_turn"}}]),
    ]


def _no_tool_session(*blocks: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _span("root", "POST /invocations", 0, parent=None),
        _span("agent", "invoke_agent Strands Agents", 1, parent="root"),
        _span("llm1", "chat glm-5.3", 2),
        _content("llm1", 3, [_user(PROMPT)], [_choice(*blocks, finish="end_turn")]),
    ]


def _provider(records: list[dict[str, Any]], **kw: Any) -> LaunchpadSpansProvider:
    logs = FakeLogs({
        f"filter attributes.session.id = '{SID}' | sort": [
            {"@message": json.dumps(r, ensure_ascii=False)} for r in records],
        "fields spanId, parentSpanId": [
            {"spanId": r["spanId"], "parentSpanId": r.get("parentSpanId", "")}
            for r in records if "startTimeUnixNano" in r],
    })
    return LaunchpadSpansProvider(logs, [GROUP], **kw)


def _agent_responses(data: Any) -> list[str]:
    from strands_evals.types.trace import AgentInvocationSpan

    return [s.agent_response for t in data["trajectory"].traces for s in t.spans
            if isinstance(s, AgentInvocationSpan)]


# ── spans path (batch / session / online / canary scoring) ─────────────────


def test_spans_actual_output_is_the_final_answer_not_the_pre_tool_preamble():
    data = _provider(_tool_session((_text(FINAL),))).get_evaluation_data(SID)
    assert data["output"] == FINAL
    assert PREAMBLE not in data["output"]
    # the TRACE judges read the same agent_response
    assert _agent_responses(data) == [FINAL]
    case = engine_core.evaluation_data(SID, data, None)
    assert case.actual_output == FINAL
    assert case.input == PROMPT


def test_spans_runtime_logs_mode_also_scores_the_final_answer():
    data = _provider(_tool_session((_text(FINAL),)),
                     spans_source="runtime_logs").get_evaluation_data(SID)
    assert PREAMBLE not in data["output"] and FINAL in data["output"]


def test_spans_keep_the_trajectory_tool_calls():
    from strands_evals.types.trace import ToolExecutionSpan

    data = _provider(_tool_session((_text(FINAL),))).get_evaluation_data(SID)
    tools = [s for t in data["trajectory"].traces for s in t.spans
             if isinstance(s, ToolExecutionSpan)]
    assert [t.tool_call.name for t in tools] == ["kb_search"]
    assert "450" in tools[0].tool_result.content


def test_spans_no_tool_answer_is_unchanged():
    data = _provider(_no_tool_session(_text(FINAL))).get_evaluation_data(SID)
    assert data["output"] == FINAL
    assert _agent_responses(data) == [FINAL]


def test_spans_refusal_after_a_tool_less_preamble_keeps_the_whole_final_message():
    # no tool call: preamble and refusal are one message (one chat bubble) — both are the answer
    data = _provider(_no_tool_session(_text(PREAMBLE), _text(REFUSAL))).get_evaluation_data(SID)
    assert PREAMBLE in data["output"] and REFUSAL in data["output"]


def test_spans_refusal_after_a_tool_call_is_the_refusal():
    data = _provider(_tool_session((_text(REFUSAL),))).get_evaluation_data(SID)
    assert data["output"] == REFUSAL


def test_spans_replayed_history_is_never_the_answer():
    # MT1: no message of the span's own ({"message": …}) — only replayed history
    # ({"content": …}, an earlier turn's text): the turn scores "", never the history
    records = _no_tool_session(_text(FINAL))
    records[-1]["body"]["output"]["messages"] = [_history(_text(PREAMBLE)), _history(_text(FINAL))]
    data = _provider(records).get_evaluation_data(SID)
    assert data["output"] == ""
    assert _agent_responses(data) == [""]


def test_spans_turn_ending_after_a_tool_call_never_scores_the_tool_result():
    # max cycles / timeout / tool error: no final chat, the agent span errored (no choice);
    # the tool span's own gen_ai.choice is {"message": <result>, "id": …} — never the answer
    records = [r for r in _tool_session((_text(FINAL),)) if r["spanId"] not in ("llm2", "agent")
               or "body" not in r]
    for r in records:
        if r["spanId"] == "tool1" and "body" in r:
            r["body"]["output"]["messages"] = [{"role": "assistant", "content": {
                "message": _blocks(_text('{"employee_id": "EMP-1024", "name": "Maya Chen"}')),
                "id": "tu-1"}}]
    data = _provider(records).get_evaluation_data(SID)
    assert data["output"] == PREAMBLE
    assert "EMP-1024" not in data["output"]


def test_spans_empty_final_answer_is_scored_as_empty_not_as_the_preamble():
    records = _tool_session((_text(FINAL),))
    for r in records:
        if r["spanId"] == "llm2" and "body" in r:
            r["body"]["output"]["messages"][-1] = _choice({"reasoningContent": {}},
                                                          finish="end_turn")
        if r["spanId"] == "agent" and "body" in r:
            r["body"]["output"]["messages"][0]["content"]["message"] = ""
    data = _provider(records).get_evaluation_data(SID)
    assert data["output"] == ""
    assert _agent_responses(data) == [""]


def test_spans_max_tokens_continuation_scores_the_continuing_agent_output():
    # the template's continuation: the first invoke_agent dies on max_tokens (no agent
    # choice), a second one in the same trace continues and ends the turn — like AgentCore
    # Evaluations, the answer is that invoke_agent span's own output
    records = _no_tool_session(_text("第一部分"))
    records[-1]["body"]["output"]["messages"] = [_choice(_text("第一部分"), finish="max_tokens")]
    records += [
        _span("agent2", "invoke_agent Strands Agents", 4, parent="root"),
        _span("llm2", "chat glm-5.3", 5, parent="agent2"),
        _content("llm2", 6, [_user("请继续")], [_choice(_text("第二部分"), finish="end_turn")]),
        _content("agent2", 7, [_user("请继续")], [{"role": "assistant", "content": {
            "message": "第二部分\n", "finish_reason": "end_turn"}}]),
    ]
    assert _provider(records).get_evaluation_data(SID)["output"] == "第二部分"


def _agent_output(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": {"message": text, "finish_reason": "end_turn"}}


def test_spans_invoke_agent_output_wins_over_the_chat_spans():
    # Global AgentCore Evaluations' rule: the agent response is the invoke_agent span's own
    # output — even when a chat span's record is stamped later
    records = _tool_session((_text("chat 的回答"),))
    for r in records:
        if r["spanId"] == "agent" and "body" in r:
            r["timeUnixNano"] = T0 + 1_500_000
            r["body"]["output"]["messages"] = [_agent_output(FINAL)]
        if r["spanId"] == "agent" and "body" not in r:
            r["endTimeUnixNano"] = T0 + 1_500_000
    data = _provider(records).get_evaluation_data(SID)
    assert data["output"] == FINAL
    assert _agent_responses(data) == [FINAL]


def test_spans_ending_after_a_tool_with_an_empty_or_absent_agent_output_never_scores_the_tool():
    tool_choice = {"role": "assistant", "content": {
        "message": _blocks(_text('{"employee_id": "EMP-1024"}')), "id": "tu-1"}}
    for agent_output in ([], [_agent_output("")]):
        records = [r for r in _tool_session((_text(FINAL),)) if r["spanId"] != "llm2"]
        for r in records:
            if r["spanId"] == "tool1" and "body" in r:
                r["body"]["output"]["messages"] = [tool_choice]
            if r["spanId"] == "agent" and "body" in r:
                r["body"]["output"]["messages"] = list(agent_output)
        out = _provider(records).get_evaluation_data(SID)["output"]
        assert "EMP-1024" not in out
        assert out == ("" if agent_output else PREAMBLE)


def test_spans_multi_turn_each_trace_keeps_its_own_invoke_agent_output():
    answers = ["第一轮答复", "第二轮答复", "第三轮答复"]
    prompts = ["问题一", "问题二", "问题三"]
    records: list[dict[str, Any]] = []
    for turn, (prompt, answer) in enumerate(zip(prompts, answers, strict=True)):
        trace = f"{turn + 1:x}" * 32
        base = turn * 100
        history = [m for p, a in zip(prompts[:turn], answers[:turn], strict=True)
                   for m in (_user(p), _history(_text(a)))]
        for rec in (
            _span("root", "POST /invocations", base, parent=None),
            _span("agent", "invoke_agent Strands Agents", base + 1, parent="root"),
            _span("llm", "chat glm-5.3", base + 2),
            # the chat span replays the earlier turns' answers in its OUTPUT (ADOT)
            _content("llm", base + 3, [_user(prompt)],
                     [*[m for m in history if m["role"] == "assistant"],
                      _choice(_text(answer), finish="end_turn")]),
            _content("agent", base + 4, [_user(prompt)], [_agent_output(answer + "\n")]),
        ):
            rec["traceId"] = trace
            rec["spanId"] = f"{rec['spanId']}-{turn}"
            if rec.get("parentSpanId"):
                rec["parentSpanId"] = f"{rec['parentSpanId']}-{turn}"
            records.append(rec)
    data = _provider(records).get_evaluation_data(SID)
    from strands_evals.types.trace import AgentInvocationSpan

    per_trace = [[(s.user_prompt, s.agent_response) for s in t.spans
                  if isinstance(s, AgentInvocationSpan)] for t in data["trajectory"].traces]
    assert per_trace == [[(p, a)] for p, a in zip(prompts, answers, strict=True)]
    assert data["output"] == answers[-1]


def test_spans_unified_telemetry_reads_the_invoke_agent_inline_choice():
    # unified delivery: no body records — gen_ai events ride inline on the span records
    def span(span_id, name, start, events, parent="agent"):
        rec = _span(span_id, name, start, parent=parent)
        rec["events"] = [{"timeUnixNano": rec["startTimeUnixNano"], "name": n,
                          "attributes": a} for n, a in events]
        return rec

    records = [
        _span("root", "POST /invocations", 0, parent=None),
        span("agent", "invoke_agent Strands Agents", 1, [
            ("gen_ai.user.message", {"content": _blocks(_text(PROMPT))}),
            ("gen_ai.choice", {"message": FINAL + "\n", "finish_reason": "end_turn"})],
            parent="root"),
        span("llm1", "chat glm-5.3", 2, [
            ("gen_ai.user.message", {"content": _blocks(_text(PROMPT))}),
            ("gen_ai.choice", {"message": _blocks(_text(PREAMBLE), _tool_use()),
                               "finish_reason": "tool_use"})]),
        span("tool1", "execute_tool kb_search", 4, [
            ("gen_ai.tool.message", {"content": json.dumps({"q": "差旅标准"}), "id": "tu-1"}),
            ("gen_ai.choice", {"message": _blocks(_text("450 元/晚")), "id": "tu-1"})]),
        span("llm2", "chat glm-5.3", 6, [
            ("gen_ai.user.message", {"content": _blocks(_text(PROMPT))}),
            ("gen_ai.assistant.message", {"content": _blocks(_text(PREAMBLE), _tool_use())}),
            ("gen_ai.choice", {"message": _blocks(_text(FINAL)), "finish_reason": "end_turn"})]),
    ]
    for r in records:
        r["attributes"]["gen_ai.operation.name"] = r["name"].split(" ")[0]
    data = _provider(records).get_evaluation_data(SID)
    assert data["output"] == FINAL
    assert _agent_responses(data) == [FINAL]
    assert engine_core.evaluation_data(SID, data, None).input == PROMPT


# ── the real record shape (run fb8a7e230b11, session 257f…, redacted fixture) ──

REAL = json.loads((Path(__file__).resolve().parents[1] / "fixtures"
                   / "obs315_preamble_session.json").read_text(encoding="utf-8"))
REAL_SID, REAL_GROUP = REAL["session_id"], REAL["log_group"]
REAL_FINAL_CHAT, REAL_FINAL_CYCLE, REAL_AGENT = (
    "32d5f123b0454ee2", "56643db38418e1f6", "50a71f02742d68d6")
REAL_PREAMBLE = "我先查一下差旅标准和您的职级信息"
MODES = {"both": [REAL_GROUP, SPANS_LOG_GROUP], "runtime_only": [REAL_GROUP]}


class _GroupLogs:
    """Logs Insights double that answers per log group, in ``@timestamp`` order."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records, self.queries = records, {}

    def start_query(self, logGroupNames, startTime, endTime, queryString):  # noqa: N803
        qid = str(len(self.queries))
        self.queries[qid] = (list(logGroupNames), queryString)
        return {"queryId": qid}

    def get_query_results(self, queryId):  # noqa: N803
        groups, query = self.queries[queryId]
        spans = [x["record"] for x in self.records if x["group"] == "aws/spans"]
        if query.startswith("fields spanId"):
            rows = [[{"field": "spanId", "value": r["spanId"]},
                     {"field": "parentSpanId", "value": r.get("parentSpanId", "")},
                     {"field": "traceId", "value": r["traceId"]}]
                    for r in spans]
        else:
            pool = [x for x in self.records
                    if x["group"] == "runtime" or SPANS_LOG_GROUP in groups]
            rows = [[{"field": "@message", "value": json.dumps(x["record"], ensure_ascii=False)}]
                    for x in sorted(pool, key=lambda x: x["timestamp"])]
        return {"status": "Complete", "results": rows}


def _real(records: list[dict[str, Any]], mode: str, sid: str = REAL_SID,
          group: str = REAL_GROUP) -> Any:
    groups = [group, SPANS_LOG_GROUP] if mode == "both" else [group]
    return LaunchpadSpansProvider(_GroupLogs(records), groups).get_evaluation_data(sid)


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((Path(__file__).resolve().parents[1] / "fixtures" / name)
                      .read_text(encoding="utf-8"))


def _turns(data: Any) -> list[tuple[str, str]]:
    from strands_evals.types.trace import AgentInvocationSpan

    return [(s.user_prompt, s.agent_response) for t in data["trajectory"].traces
            for s in t.spans if isinstance(s, AgentInvocationSpan)]


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_shape_scores_the_final_answer(mode):
    data = _real(REAL["records"], mode)
    assert data["output"].startswith("已查到，给您整理如下")
    assert REAL_PREAMBLE not in data["output"]
    assert _agent_responses(data) == [data["output"]]


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_shape_ending_after_the_tool_calls_scores_the_preamble_not_the_hr_record(mode):
    records = [x for x in copy.deepcopy(REAL["records"])
               if x["record"]["spanId"] not in (REAL_FINAL_CHAT, REAL_FINAL_CYCLE)]
    for x in records:
        if x["record"]["spanId"] == REAL_AGENT and x["group"] == "runtime":
            x["record"]["body"].pop("output", None)
    out = _real(records, mode)["output"]
    assert REAL_PREAMBLE in out
    assert "employee_id" not in out and "Maya" not in out


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_shape_empty_final_is_empty(mode):
    records = copy.deepcopy(REAL["records"])
    for x in records:
        if x["group"] != "runtime":
            continue
        rec = x["record"]
        if rec["spanId"] == REAL_FINAL_CHAT:
            final = rec["body"]["output"]["messages"][-1]["content"]
            final["message"] = json.dumps([b for b in json.loads(final["message"])
                                           if "text" not in b])
        if rec["spanId"] == REAL_AGENT:
            rec["body"]["output"]["messages"][-1]["content"]["message"] = ""
    data = _real(records, mode)
    assert data["output"] == ""
    assert _agent_responses(data) == [""]


PAIPAI = _fixture("obs315_paipai_session.json")
TWO_TURN = _fixture("obs315_two_turn_session.json")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_failed_turns_never_inherit_an_earlier_turns_text(mode):
    # MT1 (session 3cdd…): turn 2 hit a model read timeout after its tools (scored on the
    # text the user saw), turns 3–4 timed out with no gen_ai.choice at all: their records
    # only replay turn 2's text as history — they score ""
    data = _real(PAIPAI["records"], mode, PAIPAI["session_id"], PAIPAI["log_group"])
    turns = _turns(data)
    assert [p for p, _ in turns] == ["ST-00001的用电趋势是什么样的", "ST-00123\t 这个呢帮我看下",
                                     "你帮我继续查询", "ST-00123这个站点的数据是什么"]
    answers = [a for _, a in turns]
    assert answers[0].startswith("**ST-00001 不在您当前可访问的电站列表中**")
    assert answers[1].startswith("数据齐了")
    assert answers[2:] == ["", ""]
    assert data["output"] == ""


def _tool_calls(data: Any) -> list[list[tuple[str, str]]]:
    from strands_evals.types.trace import ToolExecutionSpan

    return [[(s.tool_call.name, str(s.tool_call.arguments.get("sql", ""))[:30]) for s in t.spans
             if isinstance(s, ToolExecutionSpan)] for t in data["trajectory"].traces]


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_tool_calls_come_from_the_turns_own_execute_tool_records(mode):
    # audit 1.3: turns 3–4 called nothing — their only records replay turn 2's run_sql as
    # history; a turn's tool calls are its own execute_tool spans (Global's rule)
    data = _real(PAIPAI["records"], mode, PAIPAI["session_id"], PAIPAI["log_group"])
    run_sql = "launchpad_gw_paipai___paipai-data___run_sql"
    calls = _tool_calls(data)
    assert calls == [
        [(run_sql, "SELECT to_char(day, 'YYYY-MM')"),
         ("launchpad_gw_paipai___paipai-data___list_stations", "")],
        [(run_sql, "SELECT to_char(day, 'YYYY-MM')"),
         (run_sql, "SELECT MIN(day) AS first_day, ")],
        [], [],
    ]
    from strands_evals.types.trace import AgentInvocationSpan, ToolExecutionSpan

    tools = [s for t in data["trajectory"].traces for s in t.spans
             if isinstance(s, ToolExecutionSpan)]
    assert all(t.tool_result.content and t.tool_call.tool_call_id for t in tools)
    agents = [s for t in data["trajectory"].traces for s in t.spans
              if isinstance(s, AgentInvocationSpan)]
    # obs334: available_tools is the agent's whole catalogue (span attributes, "both"), not
    # the calls — a turn that called nothing still lists every tool; without span records
    # (runtime_logs) it is the tools the turn called
    if mode == "both":
        assert all(run_sql in {c.name for c in a.available_tools} for a in agents)
        assert all(len(a.available_tools) > 10 for a in agents)
    else:
        assert [sorted(c.name for c in a.available_tools) for a in agents][2:] == [[], []]


WB006 = _fixture("obs324_wb006_8turn_session.json")


@pytest.mark.parametrize("mode", sorted(MODES))
def test_real_repeated_tool_call_ids_keep_every_call_and_its_own_result(mode):
    # GLM numbers its tool calls `call_0` in every model call: wb006 turn 4 ran `shell`
    # three times under one id — three calls, each with its own result, in both modes
    from strands_evals.types.trace import ToolExecutionSpan

    data = _real(WB006["records"], mode, WB006["session_id"], WB006["log_group"])
    turn4 = [s for s in data["trajectory"].traces[3].spans if isinstance(s, ToolExecutionSpan)]
    assert [t.tool_call.name for t in turn4] == ["shell", "shell", "shell"]
    assert {t.tool_call.tool_call_id for t in turn4} == {"call_0"}
    results = [t.tool_result.content for t in turn4]
    assert all(results) and len(set(results)) == 3
    assert [t.tool_call.arguments for t in turn4] != [turn4[0].tool_call.arguments] * 3


def test_real_available_tools_are_the_agents_whole_catalogue_with_descriptions():
    # N17-obs334: Global hands the tool judges every tool the agent had, with descriptions
    # (gen_ai.agent.tools on invoke_agent, gen_ai.tool.description on execute_tool) — not
    # only the tools a turn called
    from strands_evals.types.trace import AgentInvocationSpan

    data = _real(WB006["records"], "both", WB006["session_id"], WB006["log_group"])
    agents = [s for t in data["trajectory"].traces for s in t.spans
              if isinstance(s, AgentInvocationSpan)]
    declared = ["shell", "file_operations", "launchpad_gw_hr-database___get_employee"]
    for agent in agents:  # turns 6–8 called nothing and still see all three
        assert [c.name for c in agent.available_tools] == declared
        described = {c.name: c.description for c in agent.available_tools}
        assert described["launchpad_gw_hr-database___get_employee"]
        assert described["shell"]
    assert "hr-database___get_employee" not in {c.name for c in agents[0].available_tools}


def test_spans_tool_definitions_attribute_feeds_descriptions_and_schemas():
    records = _tool_session((_text(FINAL),))
    for r in records:
        if r.get("name", "").startswith("invoke_agent"):
            r["attributes"]["gen_ai.agent.tools"] = json.dumps(["kb_search", "hr_lookup"])
            r["attributes"]["gen_ai.tool.definitions"] = json.dumps([
                {"name": "hr_lookup", "description": "Look up an employee",
                 "inputSchema": {"type": "object"}}])
    from strands_evals.types.trace import AgentInvocationSpan

    data = _provider(records).get_evaluation_data(SID)
    agent = next(s for t in data["trajectory"].traces for s in t.spans
                 if isinstance(s, AgentInvocationSpan))
    tools = {c.name: c for c in agent.available_tools}
    assert list(tools) == ["kb_search", "hr_lookup"]
    assert tools["hr_lookup"].description == "Look up an employee"
    assert tools["hr_lookup"].parameters == {"type": "object"}


def test_spans_mcp_client_span_is_not_a_second_tool_call():
    # ADOT's MCP client span under the Strands execute_tool span also says execute_tool
    records = _tool_session((_text(FINAL),))
    mcp = _span("mcp1", "mcp tools/call kb_search", 4, parent="tool1")
    mcp["scope"] = {"name": "amazon.opentelemetry.distro.instrumentation.mcp"}
    mcp["attributes"].update({"gen_ai.operation.name": "execute_tool",
                              "gen_ai.tool.name": "kb_search"})
    data = _provider([*records, mcp]).get_evaluation_data(SID)
    assert [[n for n, _ in turn] for turn in _tool_calls(data)] == [["kb_search"]]


def _collide(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A snapshot-restored microVM: turn 2 draws turn 1's span-id sequence again."""
    spans = [x["record"] for x in records if x["group"] == "aws/spans"]
    traces = sorted({r["traceId"] for r in spans},
                    key=lambda t: min(r["startTimeUnixNano"] for r in spans if r["traceId"] == t))
    seq = {t: [r["spanId"] for r in sorted((r for r in spans if r["traceId"] == t),
                                            key=lambda r: r["startTimeUnixNano"])]
           for t in traces}
    remap = dict(zip(seq[traces[1]], seq[traces[0]], strict=False))
    out = copy.deepcopy(records)
    for x in out:
        rec = x["record"]
        if rec["traceId"] == traces[1]:
            rec["spanId"] = remap.get(rec["spanId"], rec["spanId"])
            if rec.get("parentSpanId") in remap:
                rec["parentSpanId"] = remap[rec["parentSpanId"]]
    return out


@pytest.mark.parametrize("mode", sorted(MODES))
def test_repeated_span_ids_across_turns_keep_each_turn_and_their_order(mode):
    # MT2: span ids repeat across traces after a microVM restore; they are trace-scoped
    # before parsing and the turns are sorted by their earliest span
    sid, group = TWO_TURN["session_id"], TWO_TURN["log_group"]
    clean = _real(TWO_TURN["records"], mode, sid, group)
    collided = _real(_collide(TWO_TURN["records"]), mode, sid, group)
    assert len(_turns(clean)) == 2
    assert _turns(collided) == _turns(clean)
    assert collided["output"] == clean["output"] == _turns(clean)[-1][1]
    assert [t.trace_id for t in collided["trajectory"].traces] == [
        t.trace_id for t in clean["trajectory"].traces]


# ── replay path (runtime_invoke.invoke_text) ───────────────────────────────


class _Stream:
    def __init__(self, raw: bytes) -> None:
        self._raw = raw

    def read(self) -> bytes:
        return self._raw


class _Runtime:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.raw = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
                           for e in events).encode("utf-8")

    def invoke_agent_runtime(self, **_params: Any) -> dict[str, Any]:
        return {"response": _Stream(self.raw), "contentType": "text/event-stream"}


def _replay(events: list[dict[str, Any]]) -> str:
    return runtime_invoke.invoke_text(_Runtime(events), "arn:aws-cn:x", PROMPT,
                                      session_id=SID)["text"]


def test_replay_text_is_the_answer_after_the_last_tool_call():
    events = [
        {"event": "delta", "text": "我先查一下"}, {"event": "delta", "text": "差旅标准"},
        {"event": "tool", "name": "kb_search", "id": "tu-1"},
        {"event": "tool_result", "id": "tu-1", "name": "kb_search", "status": "success"},
        {"event": "delta", "text": "已查到："}, {"event": "delta", "text": "450 元/晚"},
        {"event": "complete", "result": "已查到：450 元/晚\n"},
    ]
    assert _replay(events) == "已查到：450 元/晚"


def test_replay_no_tool_answer_is_unchanged():
    events = [{"event": "delta", "text": "您好，"}, {"event": "delta", "text": FINAL},
              {"event": "complete", "result": "您好，" + FINAL + "\n"}]
    assert _replay(events) == "您好，" + FINAL


def test_replay_refusal_after_a_tool_less_preamble_keeps_both():
    events = [{"event": "delta", "text": PREAMBLE}, {"event": "delta", "text": REFUSAL},
              {"event": "complete", "result": PREAMBLE + REFUSAL + "\n"}]
    assert _replay(events) == PREAMBLE + REFUSAL


def test_replay_with_nothing_after_the_last_tool_uses_the_complete_result():
    events = [{"event": "delta", "text": PREAMBLE}, {"event": "tool", "name": "kb_search"},
              {"event": "complete", "result": FINAL}]
    assert _replay(events) == FINAL


@pytest.mark.parametrize(("name", "operation"), [
    ("invoke_agent TravelAgent", "invoke_agent"),     # a named Strands agent
    ("invoke_agent Strands Agent", None),              # TypeScript SDK, name only
    ("agent run", "invoke_agent"),                     # operation attribute only
])
def test_the_agent_span_is_recognised_whatever_the_agent_is_called(name, operation):
    # N17-obs336 (the readiness gate's rule): gen_ai.operation.name first, the name prefix
    # only as a fallback — a named agent's answer and status come from its own agent span
    records = _tool_session((_text("chat 的回答"),))
    for r in records:
        if r["spanId"] == "agent" and "body" in r:
            r["timeUnixNano"] = T0 + 1_500_000
            r["body"]["output"]["messages"] = [
                {"role": "assistant", "content": {"message": FINAL, "finish_reason": "end_turn"}}]
        if r["spanId"] == "agent" and "body" not in r:
            r["name"] = name
            r["endTimeUnixNano"] = T0 + 1_500_000
            r["status"] = {"code": "ERROR"}
            if operation:
                r["attributes"]["gen_ai.operation.name"] = operation
            else:
                r["attributes"].pop("gen_ai.operation.name", None)
    data = _provider(records).get_evaluation_data(SID)
    from strands_evals.types.trace import AgentInvocationSpan

    agent = next(s for t in data["trajectory"].traces for s in t.spans
                 if isinstance(s, AgentInvocationSpan))
    assert agent.agent_response == FINAL  # the agent span's own output, not the chat's
    assert agent.metadata["span_status"] == "ERROR"


def test_a_span_merely_starting_with_the_operation_name_is_not_an_agent_span():
    # ONE rule with the readiness gate: the provider imports telemetry's predicate
    from app.evaluation import telemetry
    from app.services.strands_eval import spans_provider

    assert spans_provider.is_agent_span is telemetry.is_agent_span
    assert spans_provider.root_kind is telemetry.root_kind
    assert not telemetry.is_agent_span({"name": "invoke_agentic_tool", "attributes": {}})
    assert telemetry.is_agent_span({"name": "invoke_agent", "attributes": {}})


def test_a_framework_root_kind_names_the_own_agent_without_an_invoke_agent_span():
    # N17-obs336 / obs380: another framework's root (OpenInference AGENT) decides whose
    # records the trace keeps — a nested peer AGENT of another service is dropped
    from app.services.strands_eval.spans_provider import own_agent_records

    def span(span_id, parent, service, kind):
        return {"traceId": TRACE, "spanId": span_id, "parentSpanId": parent, "name": span_id,
                "startTimeUnixNano": T0, "attributes": {"openinference.span.kind": kind},
                "resource": {"attributes": {"service.name": service}}}

    records = [span("root", None, "orchestrator", "AGENT"),
               span("tool", "root", "orchestrator", "TOOL"),
               span("peer", "tool", "peer-agent", "AGENT"),
               span("peer-llm", "peer", "peer-agent", "LLM")]
    assert [r["spanId"] for r in own_agent_records(records)] == ["root", "tool"]
