"""N17-obs439 — observability token counts include the prompt cache, on real span records.

Strands emits each Bedrock call twice: the terminal provider span (``gen_ai.system=
aws.bedrock``) carries ``input_tokens`` / ``output_tokens`` only, and its wrapper chat span
(``gen_ai.system=strands-agents``) the same plus ``cache_read_input_tokens``,
``cache_write_input_tokens`` and ``total_tokens``. ``input_tokens`` excludes both cache counts;
``total_tokens`` = input + output + cache read + cache write. Every aggregate counted the
provider span only, so cache reads / writes read 0 and every total missed them (live: a trace
of 5,074 tokens showed 2,875).

The fixture (``tests/fixtures/obs439_cache_spans.json``) is two real traces from research17
(2026-10-23): Kimi K3 with cache reads AND writes, GLM-5.3 with cache reads — each with a
Titan embedding call that has no wrapper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.db import SessionLocal
from app.services import observability as obs

from .conftest import ws_ctx
from .test_observability import FakeLogs

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "obs439_cache_spans.json").read_text())
TRACES = FIXTURE["traces"]
USAGE = "gen_ai.usage."


def _usage(span):
    a = span["attributes"]
    return {k[len(USAGE):]: float(v) for k, v in a.items() if k.startswith(USAGE)}


def _calls(spans):
    """(provider spans, wrapper chat spans) — the two halves of a Strands LLM call."""
    def is_call(s):
        return s["attributes"].get("gen_ai.operation.name") in ("chat", "text_completion")
    providers = [s for s in spans if is_call(s)
                 and s["attributes"].get("gen_ai.system") != "strands-agents" and _usage(s)]
    wrappers = [s for s in spans if is_call(s)
                and s["attributes"].get("gen_ai.system") == "strands-agents" and _usage(s)]
    return providers, wrappers


def _truth(spans):
    """What the trace really used: every provider call's input / output, the cache counts from
    the wrappers — and its total = the wrappers' own total_tokens + the wrapper-less calls."""
    providers, wrappers = _calls(spans)
    wrapped = sum(_usage(w)["total_tokens"] for w in wrappers)
    wrapped_io = sum(_usage(w)["input_tokens"] + _usage(w)["output_tokens"] for w in wrappers)
    unwrapped = sum(_usage(p).get("input_tokens", 0) + _usage(p).get("output_tokens", 0)
                    for p in providers) - wrapped_io
    return {
        "input": round(sum(_usage(p).get("input_tokens", 0) for p in providers)),
        "output": round(sum(_usage(p).get("output_tokens", 0) for p in providers)),
        "cache_read": round(sum(_usage(w).get("cache_read_input_tokens", 0) for w in wrappers)),
        "cache_write": round(sum(_usage(w).get("cache_write_input_tokens", 0) for w in wrappers)),
        "total": round(wrapped + unwrapped),
    }


def test_the_fixture_is_what_production_records():
    """The premise, on the real records: provider spans carry no cache counts, and a wrapper's
    total_tokens is input + output + cache read + cache write (writes included — Kimi)."""
    writes = 0
    for spans in TRACES.values():
        providers, wrappers = _calls(spans)
        assert providers and wrappers
        assert not any("cache_read_input_tokens" in _usage(p) for p in providers)
        for w in wrappers:
            u = _usage(w)
            assert u["total_tokens"] == (u["input_tokens"] + u["output_tokens"]
                                         + u.get("cache_read_input_tokens", 0)
                                         + u.get("cache_write_input_tokens", 0))
            writes += u.get("cache_write_input_tokens", 0) > 0
    assert writes == 3


@pytest.mark.parametrize("label, expected", [
    ("kimi", {"input": 39, "output": 1110, "cache_read": 15919, "cache_write": 1274,
              "total": 18342}),
    ("glm", {"input": 31433, "output": 25272, "cache_read": 85360, "cache_write": 0,
             "total": 142065}),
])
def test_trace_detail_counts_the_cache_from_the_wrapper_spans(label, expected):
    spans = TRACES[label]
    trace_id = spans[0]["traceId"]
    assert _truth(spans) == expected
    fake = FakeLogs({f'filter traceId = "{trace_id}"': [
        {"@message": json.dumps(span)} for span in spans]})
    db = SessionLocal()
    meta = obs.get_trace(trace_id, "24h", db, ws_ctx(), logs=fake)["meta"]
    db.close()
    assert meta["tokens"] == expected


def _stats_row(spans):
    """The row the aggregate queries' `stats` return for these spans — the per-span terms of
    `_IS_LLM_FIELDS` (provider call → llm_*, wrapper chat span → wrap_cache_*) summed."""
    providers, wrappers = _calls(spans)
    return {
        "tokens_in": sum(_usage(p).get("input_tokens", 0) for p in providers),
        "tokens_out": sum(_usage(p).get("output_tokens", 0) for p in providers),
        "cache_read": "", "cache_write": "",  # the provider spans report none (null sum)
        "wrapper_cache_read": sum(_usage(w).get("cache_read_input_tokens", 0) for w in wrappers),
        "wrapper_cache_write": sum(_usage(w).get("cache_write_input_tokens", 0)
                                   for w in wrappers),
    }


@pytest.mark.parametrize("label", ["kimi", "glm"])
def test_the_aggregate_rows_take_the_cache_from_the_wrappers(label):
    spans = TRACES[label]
    row = {k: str(v) for k, v in _stats_row(spans).items()}
    truth = _truth(spans)
    trace = obs._trace_row({**row, "traceId": spans[0]["traceId"]}, None, lambda s: s)
    assert trace["tokens"] == truth
    session = obs._session_row({**row, "session_id": "s" * 64}, lambda s: s, set())
    assert session["tokens"] == truth
    (model,) = obs._tokens_by_model_rows([{**row, "model": "m", "wrapper_in": "0",
                                           "wrapper_out": "0"}])
    assert {k: model[k] for k in truth} == truth


def test_every_token_query_sums_the_wrapper_cache_counts():
    for query in (obs.q_trace_aggregates(), obs.q_session_aggregates(), obs.q_tokens_by_model()):
        assert "sum(wrap_cache_read) as wrapper_cache_read" in query
        assert "sum(wrap_cache_write) as wrapper_cache_write" in query
        assert "gen_ai.usage.cache_read_input_tokens" in query
    assert "is_wrap_call" in obs.q_trace_aggregates()


def test_a_provider_that_reports_its_own_cache_is_not_counted_twice():
    """A non-Strands provider span carrying the cache counts itself (no wrapper): its sums win."""
    row = {"tokens_in": "10", "tokens_out": "5", "cache_read": "100", "cache_write": "1",
           "wrapper_cache_read": "100", "wrapper_cache_write": "1"}
    assert obs._trace_row(row, None, lambda s: s)["tokens"] == {
        "input": 10, "output": 5, "cache_read": 100, "cache_write": 1, "total": 116}
