"""Real-record accuracy corpus (eval accuracy item 1): every corpus session — 17 real CN
sessions (1-8 turns) and the edge shapes built from them — replayed through the production
``LaunchpadSpansProvider`` in the ``both``, runtime-only and unified (converted) modes, and
checked turn by turn against the transcript the console shows (fixtures and derivation:
``tests/fixtures/eval_corpus/README.md``).

Per turn: the prompt, the answer and the tool names equal the expected ones; the answer
carries no other turn's answer and no tool-result text; the turns come in trace order. The
comparison is ``extraction_check.normalise`` equality — the production reconciliation rule.
"""

from __future__ import annotations

from typing import Any

import pytest
from strands_evals.extractors.trace_extractor import TraceExtractor
from strands_evals.mappers import CloudWatchSessionMapper
from strands_evals.types.trace import AgentInvocationSpan, EvaluationLevel, ToolExecutionSpan

from app.services import observability
from app.services.strands_eval import extraction_check
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation import corpus_support as cs

norm = extraction_check.normalise
SESSIONS = {p.stem: cs.load(p) for p in cs.corpus_files()}
EDGES = {e["name"]: e for e in cs.load(cs.CORPUS_DIR / "edges.json")}
CASES = [(name, mode) for name in sorted(SESSIONS) for mode in cs.MODES] + [
    (name, mode) for name in sorted(EDGES) for mode in cs.MODES]
# a long enough normalised text to be a meaningful "contains" probe (short answers like
# "好的" occur inside unrelated text)
PROBE_MIN = 12


def _case(name: str) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if name in SESSIONS:
        corpus = SESSIONS[name]
        return corpus, corpus["records"], corpus["expected"]["turns"]
    edge = EDGES[name]
    corpus = SESSIONS[edge["base"]]
    return corpus, cs.TRANSFORMS[edge["transform"]](corpus["records"]), edge["expected"]["turns"]


def _extracted(data: Any) -> list[dict[str, Any]]:
    turns = []
    for trace in data["trajectory"].traces:
        agent = next((s for s in trace.spans if isinstance(s, AgentInvocationSpan)), None)
        if agent is None:
            continue
        turns.append({"trace_id": trace.trace_id, "user_prompt": agent.user_prompt,
                      "answer": agent.agent_response,
                      "tools": [s.tool_call.name for s in trace.spans
                                if isinstance(s, ToolExecutionSpan)]})
    return turns


def _tool_results(corpus: dict[str, Any], records: list[dict[str, Any]]) -> list[str]:
    turns = observability.eval_tool_turns_from_content_logs(
        "g", corpus["session_id"], None, None, logs=cs.GroupLogs(records))
    return [t["text"] for t in turns if t["role"] == "TOOL_RESULT" and t.get("text")]


def test_corpus_is_the_research_sessions_and_the_edge_shapes():
    assert len(SESSIONS) == 23
    # 66 turns: f0469605c5f0 is a scenario replayed twice in ONE session (the first attempt
    # died on internalServerException after its tool call — no answer, scored ""); the two
    # eval-loop probe sessions add 9 (A2A peer, N17-obs380) + 3 (GLM max_tokens, obs381)
    assert sum(len(c["expected"]["turns"]) for c in SESSIONS.values()) == 66
    multi = [c for c in SESSIONS.values() if len(c["expected"]["turns"]) > 1]
    assert len(multi) == 14 and max(len(c["expected"]["turns"]) for c in multi) == 9
    a2a = SESSIONS["probe-multi-a2a-mt8-9d8c"]["records"]
    services = {((x["record"].get("resource") or {}).get("attributes") or {}).get("service.name")
                for x in a2a if x["group"] == "aws/spans"}
    assert len(services - {None}) == 2  # the orchestrator's and the peer's spans, one trace
    glm = SESSIONS["probe-glm-mt3-e12df09adf"]["expected"]["turns"]
    assert glm[2]["tools"] == []  # 10 written calculator calls, none executed
    assert SESSIONS["d38bac2b014e"]["colliding_span_ids"] > 0  # a REAL span-id collision
    assert [t["answer"] for t in SESSIONS["3cdd38248181"]["expected"]["turns"][2:]] == ["", ""]
    assert set(EDGES) == {"257f17b16cbb-ends_after_tool", "257f17b16cbb-empty_final",
                          "35f49b824328-collide"}


@pytest.mark.parametrize(("name", "mode"), CASES)
def test_corpus_session_turns_match_the_transcript(name, mode):
    corpus, records, expected = _case(name)
    data = cs.provider_data(corpus, records, mode)
    got = _extracted(data)
    # one turn per expected turn, in trace (time) order — no merged or reordered turns
    assert [t["trace_id"] for t in got] == [t["trace_id"] for t in expected]
    tool_texts = [norm(t) for t in _tool_results(corpus, records)]
    for index, (turn, want) in enumerate(zip(got, expected, strict=True), 1):
        where = f"{name}/{mode} turn {index}"
        assert norm(turn["user_prompt"]) == norm(want["user_prompt"]), where
        assert norm(turn["answer"]) == norm(want["answer"]), where
        assert turn["tools"] == want["tools"], where
        answer, wanted = norm(turn["answer"]), norm(want["answer"])
        for other_index, other in enumerate(expected, 1):
            probe = norm(other["answer"])
            if other_index != index and len(probe) >= PROBE_MIN and probe not in wanted:
                assert probe not in answer, f"{where}: carries turn {other_index}'s answer"
        for probe in tool_texts:
            if len(probe) >= PROBE_MIN and probe not in wanted:
                assert probe not in answer, f"{where}: carries a tool result"
    assert norm(data["output"]) == norm(expected[-1]["answer"])
    # the SESSION judges' history is the same per-turn answers, in the same order
    history = TraceExtractor(EvaluationLevel.SESSION_LEVEL).extract(data["trajectory"])
    assert [norm(h.agent_response.text) for h in history.session_history] == [
        norm(t["answer"]) for t in expected]


@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_corpus_expectations_are_the_console_transcript(name):
    # anchoring: the stored expectations equal what the console's own extractors show for
    # the same records, except the deviations the fixture documents (and the README explains)
    corpus = SESSIONS[name]
    logs = cs.GroupLogs(corpus["records"])
    console = observability.eval_turns_from_content_logs(
        "g", corpus["session_id"], None, None, logs=logs)
    console_tools: dict[str, list[str]] = {}
    for turn in observability.eval_tool_turns_from_content_logs(
            "g", corpus["session_id"], None, None, logs=logs):
        if turn["role"] == "TOOL_CALL":
            console_tools.setdefault(str(turn["trace_id"]), []).append(turn["name"])
    assert cs._console_deviations(corpus["expected"]["turns"], console, console_tools) == (
        corpus["expected"]["console_deviations"])
    for turn in corpus["expected"]["turns"]:  # the expected calls are the recorded executions
        assert turn["tools"] == cs.executed_tools(corpus["records"], turn["trace_id"])
    turns, deviations = cs.expected_turns(corpus["records"], corpus["session_id"])
    assert (turns, deviations) == (corpus["expected"]["turns"],
                                   corpus["expected"]["console_deviations"])


@pytest.mark.parametrize("name", sorted(SESSIONS))
def test_corpus_reconciles_ok_against_its_own_transcript(name):
    # the production self-check on real records: extracted turns vs the transcript digests
    corpus = SESSIONS[name]
    data = cs.provider_data(corpus, corpus["records"], "both")
    ref = extraction_check.reference(extraction_check.SOURCE_CHAT_LEDGER,
                                     [t["answer"] for t in corpus["expected"]["turns"]])
    result = extraction_check.session_check(data, ref)
    assert result["status"] == "ok", result
    assert result["turns_checked"] == len(corpus["expected"]["turns"])


class _LibraryMapper(CloudWatchSessionMapper):
    """strands-evals' own mapper — the pre-N17-obs315 extraction."""


@pytest.mark.parametrize("name", ["257f17b16cbb", "7b394e2feeaa", "a696365333a4",
                                  "b0c5a4e1918f"])
def test_corpus_catches_the_pre_obs315_extraction(name):
    # the corpus has teeth: the library mapper in "both" mode scored the preamble (single
    # turn) and the previous turn's answer (multi-turn) — the reconciliation flags it
    corpus = SESSIONS[name]
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]),
                                      [corpus["log_group"], SPANS_LOG_GROUP],
                                      mapper=_LibraryMapper())
    data = provider.get_evaluation_data(corpus["session_id"])
    ref = extraction_check.reference(extraction_check.SOURCE_REPLAY,
                                     [t["answer"] for t in corpus["expected"]["turns"]])
    assert extraction_check.session_check(data, ref)["status"] == "mismatch"


@pytest.mark.parametrize(("name", "mode"), [(n, m) for n in sorted(SESSIONS) for m in cs.MODES])
def test_tool_results_never_reappear_as_assistant_text(name, mode):
    # N17-obs401: ADOT files an execute_tool record's result ({"message", "id"}) under
    # body.output as an assistant message — the library re-emitted it as ASSISTANT text in
    # the InferenceSpans, so judges read a loaded SKILL.md (or a calculator's "9") as words
    # the agent wrote. A tool result keeps its tool role there.
    from strands_evals.types.trace import (
        AssistantMessage,
        InferenceSpan,
        TextContent,
        ToolResultContent,
        UserMessage,
    )

    corpus = SESSIONS[name]
    data = cs.provider_data(corpus, corpus["records"], mode)
    tool_texts = {norm(t) for t in _tool_results(corpus, corpus["records"])}
    tool_texts = {t for t in tool_texts if len(t) >= PROBE_MIN}
    answers = {norm(t["answer"]) for t in corpus["expected"]["turns"]}
    assistant_texts, results = [], []
    for trace in data["trajectory"].traces:
        for span in trace.spans:
            if not isinstance(span, InferenceSpan):
                continue
            for message in span.messages:
                for part in message.content:
                    if isinstance(message, AssistantMessage) and isinstance(part, TextContent):
                        assistant_texts.append(norm(part.text))
                    if isinstance(message, UserMessage) and isinstance(part, ToolResultContent):
                        results.append(norm(part.content))
    leaked = [t for t in assistant_texts if t in tool_texts and t not in answers]
    assert leaked == [], f"{name}/{mode}: tool results as assistant text: {leaked[:2]}"
    if tool_texts:  # the results are still there, under the tool role
        assert set(results) & tool_texts


def test_the_sloppy_skill_session_keeps_skill_bodies_out_of_assistant_text():
    from strands_evals.types.trace import AssistantMessage, InferenceSpan, TextContent

    corpus = SESSIONS["05440314cc974b4fb5f52f36"]
    for mode in cs.MODES:
        data = cs.provider_data(corpus, corpus["records"], mode)
        texts = [part.text for trace in data["trajectory"].traces for span in trace.spans
                 if isinstance(span, InferenceSpan) for message in span.messages
                 if isinstance(message, AssistantMessage) for part in message.content
                 if isinstance(part, TextContent)]
        assert not any(t.lstrip().startswith(("# Haiku writer", "# Expense claim writer"))
                       for t in texts), mode


SKILLS_GOOD = "c9f5f7e5e7d647dea62d6f26"
WBTEST_SKILLS = {"wbtest-expense-claim", "wbtest-haiku", "wbtest-leave-request",
                 "wbtest-trip-plan"}


@pytest.mark.parametrize("mode", cs.MODES)
def test_the_offered_skills_catalogue_reaches_the_session(mode):
    # N17-obs399: AgentSkills injects <available_skills> as a system-message text block; the
    # Session kept only user / assistant messages, so parse_available_skills read [] and the
    # skill judges never saw the alternatives — the system prompt now rides on each turn
    from strands_evals.extractors.skills import parse_available_skills

    corpus = SESSIONS[SKILLS_GOOD]
    data = cs.provider_data(corpus, corpus["records"], mode)
    offered = {s.name for s in parse_available_skills(data["trajectory"])}
    assert offered == WBTEST_SKILLS, offered
    agents = [s for t in data["trajectory"].traces for s in t.spans
              if isinstance(s, AgentInvocationSpan)]
    assert all(a.system_prompt and "<available_skills>" in a.system_prompt for a in agents)


def test_skill_judges_get_the_catalogue(monkeypatch):
    from app.services.strands_eval import engine_core
    from tests.evaluation.fakes import FakeJudge

    corpus = SESSIONS[SKILLS_GOOD]
    provider = LaunchpadSpansProvider(cs.GroupLogs(corpus["records"]),
                                      [corpus["log_group"], SPANS_LOG_GROUP])
    data = provider.get_evaluation_data(corpus["session_id"])
    case = engine_core.evaluation_data(corpus["session_id"], data, None)
    units = engine_core.tool_cases(case, engine_core.turn_cases(case))
    assert units and all({s["name"] for s in c.metadata["available_skills"]} == WBTEST_SKILLS
                         for _u, c in units)
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": "Builtin.SkillSelectionAccuracy"}],
                                          lambda *_a, **_k: judge)
    engine_core.evaluate_session(provider, corpus["session_id"], evaluators, None,
                                 job_id="j", sleep=lambda _s: None)
    import json as _json

    prompt = _json.dumps([c["messages"] for c in judge.calls], ensure_ascii=False)
    assert all(name in prompt for name in WBTEST_SKILLS), "the judge saw no catalogue"


TWO_LOADS = "d371e407437945f7a28dfa43"


@pytest.mark.parametrize("mode", cs.MODES)
@pytest.mark.parametrize("evaluator", ["Builtin.SkillSelectionAccuracy",
                                       "Builtin.SkillInstructionFollowing"])
def test_each_skill_load_is_its_own_result_stamped_with_its_call(mode, evaluator):
    # N17-obs402: wbtest-trip-plan loaded in turn 1 (premature: Global No) and again in turn 3
    # (Global Yes) — the library folded both into ONE row on the session's first trace;
    # N17-obs342: each row's task is the turn that triggered the load
    from app.services.strands_eval import engine_core
    from tests.evaluation.fakes import FakeJudge

    corpus = SESSIONS[TWO_LOADS]
    groups = {"both": [corpus["log_group"], SPANS_LOG_GROUP],
              "runtime_only": [corpus["log_group"]]}.get(mode)
    records = (cs.to_unified(corpus["records"]) if mode == "unified" else corpus["records"])
    provider = LaunchpadSpansProvider(cs.GroupLogs(records),
                                      groups or [corpus["log_group"], SPANS_LOG_GROUP])
    data = provider.get_evaluation_data(corpus["session_id"])
    traces = data["trajectory"].traces
    loads = [(k, s) for k, t in enumerate(traces) for s in t.spans
             if isinstance(s, ToolExecutionSpan) and s.tool_call.name == "skills"]
    assert [k for k, _s in loads] == [0, 2]
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet([{"id": evaluator}], lambda *_a, **_k: judge)
    rows = engine_core.evaluate_session(provider, corpus["session_id"], evaluators, None,
                                        job_id="j", sleep=lambda _s: None)
    assert [r["trace_id"] for r in rows] == [traces[k].trace_id for k, _s in loads]
    assert [r["span_id"] for r in rows] == [s.span_info.span_id.rsplit(":", 1)[-1]
                                            for _k, s in loads]
    assert all(r["tool_name"] == "skills" for r in rows)
    assert len({r["id"] for r in rows}) == 2
    prompts = [_json_dumps(c["messages"]) for c in judge.calls]
    expected = corpus["expected"]["turns"]
    if evaluator == "Builtin.SkillSelectionAccuracy":  # ## Task = the triggering turn
        assert len(prompts) == 2
        assert expected[0]["user_prompt"][:24] in prompts[0]
        assert expected[2]["user_prompt"][:24] in prompts[1]
        assert expected[2]["user_prompt"][:24] not in prompts[0]


def _json_dumps(value: Any) -> str:
    import json as _json

    return _json.dumps(value, ensure_ascii=False)


@pytest.mark.parametrize("evaluator", ["Builtin.ToolSelectionAccuracy",
                                       "Builtin.ToolParameterAccuracy"])
def test_a_result_unit_is_trace_and_span_never_the_bare_span_id(evaluator):
    # obs315 MT2: a snapshot-restored microVM draws the same span-id sequence in every turn
    # (eval-wb: 2868 of 7348 bare span ids recur across traces) — rows carry the bare id, so
    # only (trace_id, span_id) names one call
    from app.services.strands_eval import engine_core
    from tests.evaluation.fakes import FakeJudge

    corpus = SESSIONS["3cdd38248181"]
    records = cs.collide(corpus["records"])
    provider = LaunchpadSpansProvider(cs.GroupLogs(records),
                                      [corpus["log_group"], SPANS_LOG_GROUP])
    evaluators = engine_core.EvaluatorSet([{"id": evaluator}], lambda *_a, **_k: FakeJudge())
    rows = engine_core.evaluate_session(provider, corpus["session_id"], evaluators, None,
                                        job_id="j", sleep=lambda _s: None)
    units = [(r["trace_id"], r["span_id"]) for r in rows]
    assert len(rows) == 4 and all(trace and span for trace, span in units)
    assert len({span for _t, span in units}) < len(units)  # bare ids collide
    assert len(set(units)) == len(units)  # the pair never does
