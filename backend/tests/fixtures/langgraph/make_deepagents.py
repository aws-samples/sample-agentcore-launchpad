"""Record a Deep Agents session in the CloudWatch record shapes (r17-bf7 LangGraph fixtures).

A ``deepagents`` agent (``SkillsMiddleware`` over a real ``FilesystemBackend``) with a
scripted offline chat model, instrumented by opentelemetry-instrumentation-langchain;
spans are captured in memory and written as unified records (content on span attributes)
and as split records (span + event records, made by ADOT's own ``LLOHandler``). No
network, no credentials. Run in a throwaway venv, NOT the backend's::

    uv venv /tmp/da && VIRTUAL_ENV=/tmp/da uv pip install deepagents \\
        "opentelemetry-instrumentation-langchain>=0.55" opentelemetry-sdk aws-opentelemetry-distro
    /tmp/da/bin/python -I make_deepagents.py /tmp/r17bf7-da   # then build_fixtures.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from opentelemetry.instrumentation.langchain import LangchainInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/r17bf7-da")
SID = "r17bf7-deepagents-session-0001"
SKILLS = {
    "haiku-writer": ("Write a haiku (5-7-5 syllables) on a topic the user names.",
                     "# Haiku writer\n\n1. Ask nothing; pick the season word from the topic.\n"
                     "2. Write exactly three lines: 5, 7 and 5 syllables.\n"
                     '3. End with the line "-- haiku-writer v1".\n'),
    "trip-plan": ("Plan a short business trip itinerary.",
                  "# Trip plan\nList flights, hotel and a one-line agenda per day.\n"),
}

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
LangchainInstrumentor().instrument(tracer_provider=provider)

from deepagents import create_deep_agent  # noqa: E402 — after instrumentation
from deepagents.backends import FilesystemBackend  # noqa: E402
from langchain_core.language_models.chat_models import BaseChatModel  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from langchain_core.outputs import ChatGeneration, ChatResult  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402


class ScriptedModel(BaseChatModel):
    """A "haiku" prompt reads the skill, then answers; anything else answers at once."""

    @property
    def _llm_type(self) -> str:
        return "r17bf7-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedModel:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        last = messages[-1]
        n = sum(isinstance(m, HumanMessage) for m in messages)
        if isinstance(last, ToolMessage):
            msg = AIMessage(content=f"FINAL-T{n}: Leaves drift on cold wind / ... / "
                                    "-- haiku-writer v1")
        elif "haiku" in str(last.content).lower():
            msg = AIMessage(content=f"PREAMBLE-T{n}: Let me read the haiku skill.", tool_calls=[{
                "name": "read_file", "id": f"call_read_{n}", "type": "tool_call",
                "args": {"file_path": "/skills/haiku-writer/SKILL.md", "limit": 1000}}])
        else:
            msg = AIMessage(content=f"ANSWER-T{n}: You're welcome.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _span(span: Any) -> dict[str, Any]:
    attributes = {k: list(v) if isinstance(v, tuple) else v
                  for k, v in (span.attributes or {}).items()}
    record = {"traceId": f"{span.context.trace_id:032x}",
              "spanId": f"{span.context.span_id:016x}",
              "name": span.name, "kind": "INTERNAL",
              "scope": {"name": span.instrumentation_scope.name,
                        "version": span.instrumentation_scope.version or ""},
              "startTimeUnixNano": span.start_time, "endTimeUnixNano": span.end_time,
              "attributes": {**attributes, "session.id": SID},
              "status": {"code": span.status.status_code.name}}
    if span.parent:
        record["parentSpanId"] = f"{span.parent.span_id:016x}"
    return record


def main() -> None:
    for name, (description, body) in SKILLS.items():
        path = OUT / "skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}")
    agent = create_deep_agent(
        model=ScriptedModel(), tools=[], skills=["/skills/"], name="haiku_agent",
        backend=FilesystemBackend(root_dir=str(OUT), virtual_mode=True),
        checkpointer=InMemorySaver())
    turns = []
    for prompt in ["Write a haiku about autumn", "thanks!", "Now a haiku about rain"]:
        exporter.clear()
        agent.invoke({"messages": [HumanMessage(content=prompt)]},
                     config={"configurable": {"thread_id": "r17bf7-da"}})
        turns.append(list(exporter.get_finished_spans()))
    (OUT / "unified.json").write_text(json.dumps([_span(s) for t in turns for s in t]))

    from amazon.opentelemetry.distro.llo_handler import LLOHandler
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor

    logs = InMemoryLogExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(logs))
    split = [_span(s) for t in turns for s in LLOHandler(logger_provider).process_spans(t)]
    for data in logs.get_finished_logs():
        rec = getattr(data, "log_record", data)
        scope = getattr(data, "instrumentation_scope", None)
        split.append({"traceId": f"{rec.trace_id:032x}", "spanId": f"{rec.span_id:016x}",
                      "timeUnixNano": rec.timestamp, "scope": {"name": scope.name if scope else ""},
                      "attributes": {**dict(rec.attributes or {}), "session.id": SID},
                      "body": rec.body})
    (OUT / "split.json").write_text(json.dumps(split))


if __name__ == "__main__":
    main()
