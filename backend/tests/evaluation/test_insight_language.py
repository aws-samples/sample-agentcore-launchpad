"""N17-obs448 — insight cluster names and summaries in the conversations' language.

Insights run 9913db33ebf2 on hr-eval-demo (Chinese HR conversations) listed English
intents / approaches. The summariser now writes its labels and sentences in the dominant
language of the analysed sessions (their user / agent text, never the tool calls), the same
language for the whole run, English when there is no text to judge by. Only the output
language changes: what is summarised and how labels group is the same, except that a
Chinese label is now a key of its own (the ASCII-only key folded them all into "other").
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.strands_eval import insights
from tests.evaluation.fakes import FakeJudge

ZH_TURNS = [  # hr-eval-demo, run 9913db33ebf2's dataset (hr-eval-demo-golden)
    ("入职两年的全职员工一年有多少天带薪年假？",
     "第 1 步：确认提问者。按 HR-PTO-01，入职满一年不满三年的全职员工每年 15 天带薪年假。"),
    ("我还剩几天年假？员工编号 EMP-1024",
     "按 HR-PTO-03，可用年假 = 年度总额 − 已用 − 待审批：25 − 16 − 3 = 6 天。"),
]
EN_TURNS = [
    ("How many vacation days do I have left? Employee EMP-1024",
     "Per HR-PTO-03 you have 6 days available."),
    ("Can I carry over unused days?",
     "Up to 5 unused days carry over to the next year (HR-PTO-05)."),
]
TOOL_ARGS = '{"employee_id": "EMP-1024", "fields": ["pto_balance", "pending"]}'


def _session(sid, turns, tool="hr_lookup"):
    spans = []
    for prompt, answer in turns:
        spans.append(SimpleNamespace(user_prompt=prompt, agent_response=None, tool_call=None))
        spans.append(SimpleNamespace(user_prompt=None, agent_response=None,
                                     tool_call=SimpleNamespace(name=tool, arguments=TOOL_ARGS)))
        spans.append(SimpleNamespace(user_prompt=None, agent_response=answer, tool_call=None))
    return SimpleNamespace(session_id=sid, traces=[SimpleNamespace(spans=spans)])


class _Provider:
    def __init__(self, sessions):
        self.sessions = sessions

    def session(self, sid):
        return self.sessions[sid]


class _PromptJudge(FakeJudge):
    """Records every system prompt the summariser sends (the read-only prompt dry run)."""

    def __init__(self, labels):
        super().__init__(by_tool={"SessionSummary": lambda: dict(labels)})
        self.system_prompts: list[str] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.system_prompts.append(str(system_prompt))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


ZH_LABELS = {"intent_label": "年假余额查询", "intent": "员工询问剩余年假天数",
             "summary_label": "查询 HR 系统", "summary": "查询员工记录后按政策给出可用天数"}
EN_LABELS = {"intent_label": "Leave balance", "intent": "asks for PTO left",
             "summary_label": "HR lookup", "summary": "looked up HR and answered"}
KINDS = [insights.USER_INTENT, insights.EXECUTION_SUMMARY]


def test_language_is_judged_by_the_conversation_not_the_tool_calls():
    zh = insights._conversation_text(_session("s", ZH_TURNS))
    assert "hr_lookup" not in zh and "employee_id" not in zh
    assert insights.dominant_language([zh]) == "zh"
    en = insights._conversation_text(_session("e", EN_TURNS))
    assert insights.dominant_language([en]) == "en"
    assert insights.dominant_language([]) == "en"
    assert insights.dominant_language([""], default="zh") == "zh"


def test_the_prompt_names_the_output_language():
    assert insights.summariser_prompt("zh").startswith(insights.SUMMARISER_SYSTEM_PROMPT)
    assert "Simplified Chinese (简体中文)" in insights.summariser_prompt("zh")
    assert insights.summariser_prompt("en").endswith(
        "Write every label and sentence in English, whatever language the tool calls or "
        "system text are in.")
    assert insights.summariser_prompt("fr") == insights.summariser_prompt("en")


@pytest.mark.parametrize("turns, labels, language", [
    (ZH_TURNS, ZH_LABELS, "Simplified Chinese (简体中文)"),
    (EN_TURNS, EN_LABELS, "English"),
])
def test_a_run_summarises_in_its_sessions_language(turns, labels, language):
    judge = _PromptJudge(labels)
    sessions = {f"s{i}": _session(f"s{i}", turns) for i in range(3)}
    out = insights.diagnose_sessions(_Provider(sessions), list(sessions), KINDS, judge)
    assert judge.system_prompts and all(f"in {language}," in p for p in judge.system_prompts)
    (intent,) = out["userIntents"]  # three sessions, one label: one cluster
    assert intent["name"] == labels["intent_label"] and intent["affectedSessionCount"] == 3
    assert out["executionSummaries"][0]["name"] == labels["summary_label"]


def test_a_mixed_run_uses_its_dominant_language_for_every_session():
    judge = _PromptJudge(ZH_LABELS)
    sessions = {"z1": _session("z1", ZH_TURNS), "z2": _session("z2", ZH_TURNS),
                "e1": _session("e1", EN_TURNS[:1])}
    insights.diagnose_sessions(_Provider(sessions), list(sessions), KINDS, judge)
    assert len(judge.system_prompts) == 3
    assert {p.rsplit("Write every label", 1)[1] for p in judge.system_prompts} == {
        " and sentence in Simplified Chinese (简体中文), whatever language the tool calls or "
        "system text are in."}


def test_chinese_labels_cluster_by_label_not_into_other():
    """Distinct Chinese labels stay distinct; the same one groups — ASCII keys as before."""
    assert insights._label_key("年假余额查询") != insights._label_key("报销流程咨询")
    assert insights._label_key("年假余额查询") == insights._label_key(" 年假余额查询 ")
    assert insights._label_key("Leave  balance!") == insights._label_key("leave balance")
    assert insights._label_key("PTO_balance") == "pto balance"
    assert insights._label_key("") == "other"
    summary = insights.SessionSummary(**ZH_LABELS)
    other = insights.SessionSummary(**{**ZH_LABELS, "intent_label": "报销流程咨询"})
    clusters = insights._summary_clusters({"a": (summary, "q", "a"), "b": (other, "q", "a"),
                                           "c": (summary, "q", "a")}, 3,
                                          kind=insights.USER_INTENT)
    assert [(c["name"], c["affectedSessionCount"]) for c in clusters] == [
        ("年假余额查询", 2), ("报销流程咨询", 1)]
