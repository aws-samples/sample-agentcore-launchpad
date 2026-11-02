"""A conversation's title from its first message (prod QA 2026-11-02: the whole first
line became a paragraph-long title that stretched headings and dialogs)."""

from app.assistant.service import TITLE_MAX, conversation_title

QA_PROMPT = (
    "公司差旅报销政策问答助手，不需要外部系统。面向全体员工，在内部聊天里回答差旅标准、"
    "报销流程、发票要求和审批时限。政策内容直接写进提示词即可：国内出差住宿一线城市每晚"
    "上限 600 元、其他城市 400 元；餐补每天 100 元；交通标准为高铁"
)


def test_stops_at_a_short_first_sentence():
    assert conversation_title(QA_PROMPT) == "公司差旅报销政策问答助手，不需要外部系统"
    assert conversation_title("Build an HR FAQ bot. It answers leave questions.") == (
        "Build an HR FAQ bot"
    )


def test_cuts_a_long_sentence_with_an_ellipsis():
    title = conversation_title("一" * 100)
    assert len(title) == TITLE_MAX
    assert title.endswith("…")
    long_en = "word " * 30
    assert conversation_title(long_en).endswith("…")
    assert len(conversation_title(long_en)) <= TITLE_MAX


def test_keeps_a_short_message_and_uses_the_first_non_blank_line():
    assert conversation_title("  帮我做一个 IT 服务台助手  ") == "帮我做一个 IT 服务台助手"
    assert conversation_title("\n\n  hello   world \nsecond line") == "hello world"
    assert conversation_title("") == ""
    assert conversation_title("   \n ") == ""


def test_a_too_early_sentence_end_does_not_make_a_stub_title():
    # "Hi." is not a title; the line is short enough to keep whole
    prompt = "Hi. Please design a support agent"
    assert conversation_title(prompt) == prompt
    # a decimal point is not a sentence end
    assert conversation_title("预算 1.5 万元以内的客服助手") == "预算 1.5 万元以内的客服助手"
