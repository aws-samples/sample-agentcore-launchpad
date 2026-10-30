"""Worker-side handler for ``recommend`` jobs — the ``gepa_lite`` reflective round (N12 E2-4).

GEPA (gepa-ai/gepa) evolves a text component by showing an LLM the system's execution
traces with per-example feedback, asking it to diagnose the failures and rewrite the
component. This module runs exactly ONE such reflection — scored sessions + judge
explanations → one revised system prompt and/or revised descriptions for the agent's OWN
tools — and leaves the "does it score better" question to the configuration A/B test the
experiment runs next.

Pure library code (inlined into the evaluation worker): the evidence arrives as plain
dicts in the job spec (the backend collected it from ``eval_results`` + transcripts; the
worker has no ledger), the model arrives through ``judge_factory`` (a strands ``Model``
from ``aws_clients.global_bedrock_model`` on the backend, the bearer-based factory on the
worker — never ``None`` / a ``str``, risk R-3), and text completion is
:func:`model_converse` over that model. ``optimization/providers/gepa_lite.py`` is the
backend-side adapter that turns its dataclasses into these dicts.

Failure contract (mirrors the Global job): anything that stops a usable prompt from being
produced is a FAILED result with a reason and NO recommended prompt — never a truncated
or invented one.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any

from botocore.exceptions import ClientError
from strands import Agent
from strands.models import Model

from app.services.strands_eval import model_calls

PROVIDER_ID = "gepa_lite"
JOB_TYPE = "recommend"

# One Converse-shaped text completion: (model_id, system, messages, max_tokens)
# → (text, usage). ``messages`` are ``[{"role": "user"|"assistant", "text": ...}]``.
ConverseFn = Callable[[str, str, list[dict[str, str]], int], tuple[str, dict[str, Any]]]
Progress = Callable[[str], None]

REFLECTION_SYSTEM = """\
You improve the system prompt (the "instruction") of a deployed AI agent.

You will be given the agent's CURRENT instruction and a set of real sessions the
agent handled under it. Each session shows the conversation and the feedback an
automated evaluation produced: evaluator name, score, label and the evaluator's
explanation. Low scores are failures; high scores show what already works.

Work like a careful prompt engineer:
1. Read every example. Identify the recurring patterns behind the failures, and
   what the successful sessions have in common.
2. Decide which instructions to add, sharpen, reorder or remove to fix the
   failure patterns WITHOUT breaking what works.
3. Write the revised instruction in full. Keep every domain fact, tool name,
   output-format rule, persona and safety constraint from the current
   instruction unless the feedback shows it is wrong. Do not mention the
   evaluation, the examples or this task inside the instruction itself.
4. The revised instruction must be at most {max_chars} characters.
{components}
Answer with ONE JSON object and nothing else:
{{"diagnosis": "<what the current instruction gets wrong, 2-5 sentences>",
  "changes": ["<one concrete change>", "..."],
  "revised_prompt": <the full revised instruction, or null when the instruction is frozen>,
  "tool_descriptions": {{"<tool name>": "<full revised description>"}},
  "tool_changes": ["<one concrete change to a tool description>", "..."]}}
Omit "tool_descriptions" / "tool_changes" (or leave them empty) when tool
descriptions are not part of this task or need no change.
"""

# The components paragraph of REFLECTION_SYSTEM, by what the caller asked for.
COMPONENTS_TEXT = {
    ("system_prompt",): (
        "\nThis task covers the INSTRUCTION ONLY. Tool descriptions are shown for "
        "context and must not be proposed.\n"
    ),
    ("system_prompt", "tool_descriptions"): (
        "\nThis task covers BOTH the instruction AND the descriptions of the agent's "
        "own tools listed under \"Tools the agent owns\". A tool description is what "
        "the model reads to decide WHEN to call the tool and WHAT to pass; rewrite a "
        "description only when the tool-call feedback shows the model picked the wrong "
        "tool, skipped the right one, or passed wrong parameters. Keep each description "
        "self-contained, under 1024 characters, and never rename a tool. Tools listed "
        "as \"seen in traces, not editable\" must not be proposed.\n"
    ),
    ("tool_descriptions",): (
        "\nThe INSTRUCTION IS FROZEN for this task: do not propose changes to it and "
        "return \"revised_prompt\": null. Revise ONLY the descriptions of the agent's "
        "own tools listed under \"Tools the agent owns\", and only where the tool-call "
        "feedback shows the model picked the wrong tool, skipped the right one, or passed "
        "wrong parameters. Keep each description self-contained, under 1024 characters, "
        "never rename a tool, and never propose tools listed as \"seen in traces, not "
        "editable\".\n"
    ),
}
TOOL_DESC_MAX_CHARS = 1024

COMPRESS_SYSTEM = """\
Shorten an AI agent's system prompt to at most {max_chars} characters without
dropping any instruction, constraint, domain fact, tool name or format rule.
Merge redundancy, remove filler, keep the meaning. Answer with ONE JSON object
and nothing else: {{"revised_prompt": "<the shortened instruction>"}}
"""

RETRY_SUFFIX = (
    "\n\nYour previous answer was not a parseable JSON object. Answer again with "
    "ONLY the JSON object described above — no prose, no code fences."
)
TRUNCATED_SUFFIX = (
    "\n\nYour previous answer was cut off by the output limit before the JSON object "
    "closed. Answer again with ONLY the JSON object, and keep it compact: no prose "
    "outside the JSON, short diagnosis, and only the tool descriptions that change."
)
MAX_TOKENS_CEILING = 16000
EXPLANATION_MAX = 1200
DEFAULT_MAX_CHARS = 8000
DEFAULT_MAX_TOKENS = 4096
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_WORD = r"(?<![A-Za-z0-9_]){}(?![A-Za-z0-9_])"


# ─── rendering ───────────────────────────────────────────────────────────────


def tool_mentions(text: str | None, names: list[str]) -> list[str]:
    """Tool names an evaluator explanation mentions (word-boundary match,
    backticks/quotes tolerated). Best effort — TOOL_CALL records carry no tool
    id, so this is the only link from a judgement to a call."""
    if not text:
        return []
    return [n for n in names if n and re.search(_WORD.format(re.escape(n)), text)]


def _render_tool_calls(turns: list[dict[str, Any]], own_tools: list[str]) -> list[str]:
    """The session's tool calls with results, plus which owned tools went unused."""
    calls = [t for t in turns if t.get("role") == "tool_call"]
    results = {t.get("id"): t for t in turns if t.get("role") == "tool_result"}
    notes = [t["text"] for t in turns if t.get("role") == "note"]
    lines = ["Tool calls:"]
    if not calls:
        lines.append("  (none)")
    for i, c in enumerate(calls, 1):
        args = c.get("input") or ("<arguments not recorded>" if c.get("source") == "span" else "{}")
        res = results.get(c.get("id"))
        if res is not None:
            outcome = f" → {res.get('status') or 'done'}: {res.get('text') or '(empty)'}"
        elif c.get("status"):
            outcome = f" → {c['status']}"
        else:
            outcome = ""
        lines.append(f"  {i}. {c.get('name')}({args}){outcome}")
    lines.extend(f"  {n}" for n in notes)
    if own_tools:
        called = {c.get("name") for c in calls}
        unused = [n for n in own_tools if n not in called]
        unused_text = ", ".join(unused) if unused else "(all were called)"
        lines.append(f"Owned tools NOT called: {unused_text}")
    return lines


def render_reflective_dataset(
    evidence: list[dict[str, Any]],
    own_tools: list[str] | None = None,
    with_tools: bool = False,
    known_tool_names: list[str] | None = None,
) -> str:
    """GEPA's ``make_reflective_dataset`` shape: Inputs / Generated Outputs /
    (Tool calls) / Feedback per example, worst sessions first (the caller's order).
    ``evidence`` items are ``{session_id, turns, records, mean_score}`` dicts."""
    names = known_tool_names or own_tools or []
    blocks: list[str] = []
    for k, ev in enumerate(evidence, 1):
        mean = ev.get("mean_score")
        score = "n/a" if mean is None else f"{float(mean):.2f}"
        turns = ev.get("turns") or []
        users = [t["text"] for t in turns if t.get("role") == "user"]
        assistants = [t["text"] for t in turns if t.get("role") == "assistant"]
        sid = str(ev.get("session_id") or "")
        lines = [f"### Example {k}  (mean score {score}, session {sid[:12]}…)"]
        lines.append("Inputs:")
        if users:
            lines.extend(f"  - {u}" for u in users)
        else:
            lines.append("  (no transcript)")
        lines.append("Generated Outputs:")
        if assistants:
            lines.extend(f"  - {a}" for a in assistants)
        else:
            lines.append("  (none)")
        if with_tools:
            lines.extend(_render_tool_calls(turns, own_tools or []))
        lines.append("Feedback:")
        for r in ev.get("records") or []:
            if r.get("error"):
                lines.append(f"  - {r.get('evaluator_id')}: evaluator error — {r['error']}")
                continue
            value = r.get("score")
            val = "n/a" if value is None else f"{float(value):g}"
            label = f" ({r['label']})" if r.get("label") else ""
            why = f' — "{r["explanation"]}"' if r.get("explanation") else ""
            tag = ""
            if with_tools and str(r.get("level") or "").lower() in ("span", "tool_call"):
                mentioned = tool_mentions(r.get("explanation"), names)
                tag = f" [tool-call judgement; mentions: {', '.join(mentioned) or 'n/a'}]"
            lines.append(f"  - {r.get('evaluator_id')} = {val}{label}{why}{tag}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_tools_header(tools: dict[str, str], observed_tools: dict[str, Any]) -> list[str]:
    """The agent's own tools (editable) and other tools seen in traces (context)."""
    lines = ["## Tools the agent owns (current descriptions — the only editable ones)"]
    if tools:
        for name, desc in tools.items():
            seen = observed_tools.get(name) or {}
            usage = f" · called {seen['calls']}×" if seen.get("calls") else " · never called"
            errs = f", {seen['errors']} errors" if seen.get("errors") else ""
            lines.append(f"- `{name}`{usage}{errs}: {desc or '(no description)'}")
    else:
        lines.append("- (none discovered)")
    others = {n: info for n, info in observed_tools.items() if n not in tools}
    if others:
        lines.append("")
        lines.append("## Other tools seen in traces (context only, NOT editable)")
        for name, info in others.items():
            desc = info.get("description_seen") or "(description not recorded)"
            lines.append(f"- `{name}` · called {info.get('calls', 0)}×: {desc}")
    return lines


def build_user_message(
    *,
    current_prompt: str,
    evidence: list[dict[str, Any]],
    stats: dict[str, Any],
    tools: dict[str, str],
    components: tuple[str, ...],
    extra_feedback: list[str],
) -> str:
    with_tools = "tool_descriptions" in components
    observed = dict(stats.get("observed_tools") or {})
    parts = ["## Current instruction", current_prompt or "(empty)", ""]
    if with_tools:
        parts += render_tools_header(tools, observed) + [""]
    parts += [
        f"## Sessions ({len(evidence)} of {stats.get('sessions_scored', 0)} scored, worst first)",
        render_reflective_dataset(
            evidence, own_tools=list(tools), with_tools=with_tools,
            known_tool_names=list(dict.fromkeys(list(tools) + list(observed))),
        ),
    ]
    if extra_feedback:
        parts += ["", "## Failure analysis from the evaluation run"]
        parts += [f"- {line}" for line in extra_feedback]
    return "\n".join(parts)


# ─── parsing / validation ────────────────────────────────────────────────────


def validate_tool_descriptions(
    proposed: Any, current: dict[str, str]
) -> tuple[dict[str, str], dict[str, int]]:
    """Keep only rewrites of tools the agent owns that actually change something
    and fit the budget; report what was dropped and why."""
    kept: dict[str, str] = {}
    dropped = {"unknown": 0, "empty": 0, "too_long": 0, "unchanged": 0}
    if not isinstance(proposed, dict):
        return kept, dropped
    for name, value in proposed.items():
        if name not in current:
            dropped["unknown"] += 1
            continue
        text = str(value or "").strip() if isinstance(value, str | int | float) else ""
        if not text:
            dropped["empty"] += 1
        elif len(text) > TOOL_DESC_MAX_CHARS:
            dropped["too_long"] += 1
        elif text == (current.get(name) or "").strip():
            dropped["unchanged"] += 1
        else:
            kept[str(name)] = text
    return kept, dropped


def parse_reflection(text: str) -> dict[str, Any] | None:
    """The first JSON object in ``text`` (fences tolerated), or None. ``strict=False``
    accepts the raw newlines / tabs long prompts make models emit inside strings."""
    cleaned = _FENCE.sub("", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    body = cleaned[start : end + 1]
    for strict in (True, False):
        try:
            obj = json.loads(body, strict=strict)
        except ValueError:
            continue
        return obj if isinstance(obj, dict) else None
    return None


def describe_unparseable(text: str) -> str:
    """Why ``text`` did not parse, for the operator-facing failure reason."""
    cleaned = _FENCE.sub("", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        head = cleaned[:80].replace("\n", " ")
        return f"no JSON object in the reply (starts: {head!r})"
    try:
        json.loads(cleaned[start : end + 1], strict=False)
    except ValueError as exc:
        return f"JSON error: {str(exc)[:120]}"
    return "reply parsed to a non-object"


def _failed(reason: str, want_tools: bool, meta: dict[str, Any]) -> dict[str, Any]:
    """One call, one outcome: a failure fails every requested component."""
    out: dict[str, Any] = {
        "status": "FAILED", "recommended_prompt": None, "explanation": "",
        "error": reason[:300], "meta": dict(meta), "tool_descriptions": None,
        "tool_status": None, "tool_error": None, "tool_explanation": "",
    }
    if want_tools:
        out.update(tool_status="error", tool_error=reason[:300], tool_descriptions={})
    return out


# ─── the reflection ──────────────────────────────────────────────────────────


def reflect(
    *,
    converse: ConverseFn,
    model_id: str,
    current_prompt: str,
    evidence: list[dict[str, Any]],
    stats: dict[str, Any],
    tools: dict[str, str] | None = None,
    components: tuple[str, ...] = ("system_prompt",),
    extra_feedback: list[str] | None = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    progress: Progress | None = None,
    max_tokens_cap: int | None = None,
) -> dict[str, Any]:
    """One reflective round → ``{status, recommended_prompt, explanation, error, meta,
    tool_descriptions, tool_status, tool_error, tool_explanation}``.

    N-SCN-bug2: a reply cut off by the output limit whose JSON still parses is accepted; an
    unparseable one is asked again with the budget doubled (``TRUNCATED_SUFFIX``), up to
    ``max_tokens_cap`` (default ``MAX_TOKENS_CEILING``) and at most twice; the failure
    reason then names the last budget tried."""
    progress = progress or (lambda _m: None)
    tools = dict(tools or {})
    extra_feedback = list(extra_feedback or [])
    selected = int(stats.get("sessions_selected") or len(evidence))
    base_meta: dict[str, Any] = {
        "evidence_sessions": selected,
        "evidence_records": int(stats.get("records") or 0),
        "sessions_without_transcript": selected - int(stats.get("sessions_with_transcript") or 0),
    }
    want_prompt = "system_prompt" in components
    want_tools = "tool_descriptions" in components
    chosen = tuple(c for c in ("system_prompt", "tool_descriptions") if c in components) or (
        "system_prompt",)
    tool_calls_seen = int(stats.get("tool_calls_seen") or 0)
    if want_tools:
        base_meta.update(sessions_with_tool_calls=int(stats.get("sessions_with_tool_calls") or 0),
                         tool_calls_seen=tool_calls_seen)
    if not any(ev.get("mean_score") is not None for ev in evidence):
        return _failed(
            "no scored sessions in the selected run — the optimizer needs evaluator scores "
            "to reflect on", want_tools, base_meta,
        )
    # tool component preconditions — decided before spending a model call
    tool_precheck: tuple[str, str] | None = None
    if want_tools and not tools:
        tool_precheck = ("no-tools", "the agent exposes no discoverable tools")
    elif want_tools and tool_calls_seen == 0:
        tool_precheck = (
            "no-tool-calls",
            "no tool calls in the selected run — pick a run whose prompts exercise the "
            "agent's tools",
        )
    if tool_precheck and not want_prompt:
        status, reason = tool_precheck
        return {**_failed(reason, False, base_meta), "tool_descriptions": {},
                "tool_status": status, "tool_error": reason}
    if tool_precheck:  # the prompt still proceeds; the tool side is settled
        want_tools = False
        chosen = ("system_prompt",)

    def fail(reason: str, meta: dict[str, Any]) -> dict[str, Any]:
        out = _failed(reason, want_tools, meta)
        if tool_precheck:  # no model call was ever made for the tool side
            out["tool_status"], out["tool_error"] = tool_precheck
            out["tool_descriptions"] = {}
        return out

    started = time.monotonic()
    usage_total = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
    cap = max(int(max_tokens_cap or MAX_TOKENS_CEILING), int(max_tokens))
    budget = {"max_tokens": int(max_tokens), "truncated": False, "last": int(max_tokens)}

    def ask(system: str, user: str) -> str:
        def call(limit: int) -> str:
            # a retry after a cut-off reply asks for the tighter output explicitly
            suffix = TRUNCATED_SUFFIX if limit != budget["max_tokens"] else ""
            text, usage = converse(model_id, system, [{"role": "user", "text": user + suffix}],
                                   limit)
            usage_total["calls"] += 1
            budget["last"] = limit
            for k in ("input_tokens", "output_tokens"):
                usage_total[k] += int(usage.get(k) or 0)
            budget["truncated"] = str(usage.get("stop_reason") or "") == "max_tokens"
            # a reply cut off by the output limit that still parses is accepted; one that
            # does not is the output-limit failure the budget retry recovers from
            if budget["truncated"] and parse_reflection(text) is None:
                raise model_calls.OutputTruncated(limit, "no parseable JSON in the partial")
            return text

        def on_retry(attempt: int, _before: int, after: int) -> None:
            progress("reflection output hit the token limit — retrying with "
                     f"{after} tokens (attempt {attempt}/{model_calls.MAX_BUDGET_RETRIES + 1})…")

        return model_calls.call_with_budget(call, budget=budget["max_tokens"], cap=cap,
                                            label="reflection", model_id=model_id,
                                            on_retry=on_retry)

    def finish(**extra: Any) -> dict[str, Any]:
        return {**base_meta, **usage_total,
                "latency_ms": int((time.monotonic() - started) * 1000), **extra}

    system = REFLECTION_SYSTEM.format(max_chars=max_chars, components=COMPONENTS_TEXT[chosen])
    user = build_user_message(current_prompt=current_prompt, evidence=evidence, stats=stats,
                              tools=tools, components=chosen, extra_feedback=extra_feedback)
    try:
        progress(f"reflecting on {len(evidence)} sessions with {model_id}…")
        raw = ask(system, user)
        parsed = parse_reflection(raw)
        if parsed is None:
            progress("reflection returned no JSON — retrying once…")
            raw = ask(system, user + RETRY_SUFFIX)
            parsed = parse_reflection(raw)
        if parsed is None:
            return fail("reflection model returned no parseable JSON — "
                        + describe_unparseable(raw), finish())
        prompt = str(parsed.get("revised_prompt") or "").strip()
        if want_prompt and not prompt:
            return fail("reflection model returned an empty revised_prompt", finish())
        if not want_prompt:
            prompt = ""  # frozen — whatever the model echoed is discarded
        if want_prompt and len(prompt) > max_chars:
            progress(f"candidate is {len(prompt)} chars — compressing once…")
            squeezed = parse_reflection(ask(COMPRESS_SYSTEM.format(max_chars=max_chars), prompt))
            prompt = str((squeezed or {}).get("revised_prompt") or "").strip()
            if not prompt or len(prompt) > max_chars:
                return fail(
                    f"candidate exceeds {max_chars} chars after one compression "
                    f"({len(prompt) or 'empty'})", finish(),
                )
    except model_calls.JudgeBudgetExceeded as exc:
        # every attempt was cut off: name the budget tried (N-SCN-bug2) and the knobs
        return fail(
            f"reflection output truncated by the token limit — {exc} "
            f"({usage_total['output_tokens']} output tokens over {usage_total['calls']} calls); "
            "raise prompt_opt_max_tokens / eval_judge_max_tokens_cap", finish(),
        )
    except ClientError as exc:
        err = exc.response.get("Error") or {}
        return fail(f"{err.get('Code') or 'ClientError'}: {err.get('Message') or exc}", finish())
    except Exception as exc:  # noqa: BLE001 — network, SDK response parsing, …
        return fail(f"{type(exc).__name__}: {exc}", finish())

    changes = [str(c) for c in (parsed.get("changes") or []) if str(c).strip()][:12]
    diagnosis = str(parsed.get("diagnosis") or "").strip()
    explanation = diagnosis
    if changes:
        explanation += ("\n" if explanation else "") + "\n".join(f"- {c}" for c in changes)
    result: dict[str, Any] = {
        "status": "COMPLETED" if want_prompt else "FAILED",
        "recommended_prompt": prompt if want_prompt else None,
        "explanation": explanation[:EXPLANATION_MAX] if want_prompt else "",
        "error": None if want_prompt else "instruction frozen — tool descriptions only",
        "meta": finish(changes=changes),
        "tool_descriptions": None, "tool_status": None, "tool_error": None,
        "tool_explanation": "",
    }
    if tool_precheck:
        result["tool_status"], result["tool_error"] = tool_precheck
        result["tool_descriptions"] = {}
    elif want_tools:
        kept, dropped = validate_tool_descriptions(parsed.get("tool_descriptions"), tools)
        tool_changes = [str(c) for c in (parsed.get("tool_changes") or []) if str(c).strip()][:12]
        result["tool_descriptions"] = kept
        result["tool_status"] = "COMPLETED"
        result["tool_explanation"] = "\n".join(f"- {c}" for c in tool_changes)[:EXPLANATION_MAX]
        result["meta"].update(
            tool_descriptions_proposed=len(kept) + sum(dropped.values()),
            tool_descriptions_dropped=dropped,
            tool_changes=tool_changes,
        )
    return result


# ─── model → text completion ─────────────────────────────────────────────────


def model_converse(model: Any) -> ConverseFn:
    """A ``ConverseFn`` over a strands ``Model`` object (the only way a reflection reaches
    Bedrock — lead ruling: judge / reflection models come from ``global_bedrock_model``).
    Only text blocks are returned (reasoning blocks GLM / Kimi stream first are not part
    of the answer, spec C3)."""
    if not isinstance(model, Model):
        raise ValueError(
            "model_converse needs a strands Model object (aws_clients.global_bedrock_model); "
            f"got {type(model).__name__}"
        )

    def converse(
        model_id: str, system: str, messages: list[dict[str, str]], max_tokens: int,
    ) -> tuple[str, dict[str, Any]]:
        try:
            model.update_config(max_tokens=int(max_tokens))
        except Exception:  # noqa: BLE001 — a model without the knob keeps its own
            pass
        agent = Agent(model=model, system_prompt=system, callback_handler=None)
        prompt = "\n\n".join(m["text"] for m in messages if m.get("role") == "user") or " "
        try:
            result = agent(prompt)
        except Exception as exc:  # noqa: BLE001 — classified below
            if not model_calls.is_max_tokens_error(exc):
                raise
            # N-SCN-bug2: strands raises on a max_tokens stop instead of returning it; the
            # partial answer is on the agent's history — hand it back as the Converse shape
            # (stop_reason "max_tokens") so `reflect` can accept a parseable partial or retry
            return partial_text(agent), {"input_tokens": None, "output_tokens": None,
                                         "stop_reason": "max_tokens"}
        usage = getattr(getattr(result, "metrics", None), "accumulated_usage", None) or {}
        return str(result).strip(), {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "stop_reason": getattr(result, "stop_reason", None),
        }

    return converse


def partial_text(agent: Any) -> str:
    """The text blocks of the agent's last assistant message (the partial answer strands
    appended before raising ``MaxTokensReachedException``); reasoning blocks are not text."""
    for message in reversed(list(getattr(agent, "messages", None) or [])):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        parts = [str(block.get("text") or "") for block in message.get("content") or []
                 if isinstance(block, dict) and block.get("text")]
        return "".join(parts).strip()
    return ""


# ─── the job ─────────────────────────────────────────────────────────────────


def run(
    job: Any,  # evaluation.jobs.JobSpec — duck-typed: the library imports nothing from app.*
    sink: Any,
    *,
    judge_factory: Callable[..., Any],
    converse: ConverseFn | None = None,
) -> None:
    """``recommend`` job: one reflection; the result travels in ``done.json.summary``
    (``recommendation``) and as the ``recommendation.json`` artefact."""
    spec = job.spec
    model_id = str(spec.get("model_id") or "")
    max_tokens = int(spec.get("max_tokens") or DEFAULT_MAX_TOKENS)
    # CN additive (N-SCN-bug2): the cap the budget retries stop at
    max_tokens_cap = int(spec.get("max_tokens_cap") or 0) or None
    sink.status("evaluating", done=0, total=1, message="preparing the reflection")
    fn = converse or model_converse(
        judge_factory(model_id, max_tokens=max_tokens,
                      temperature=float(spec.get("temperature") or 0.0))
    )
    result = reflect(
        converse=fn, model_id=model_id,
        current_prompt=str(spec.get("current_prompt") or ""),
        evidence=list(spec.get("evidence") or []), stats=dict(spec.get("stats") or {}),
        tools=dict(spec.get("tools") or {}),
        components=tuple(spec.get("components") or ("system_prompt",)),
        extra_feedback=list(spec.get("extra_feedback") or []),
        max_chars=int(spec.get("max_chars") or DEFAULT_MAX_CHARS), max_tokens=max_tokens,
        progress=lambda message: sink.status("evaluating", done=0, total=1, message=message),
        max_tokens_cap=max_tokens_cap,
    )
    sink.artifact("recommendation.json", result)
    sink.finish("COMPLETED", summary={"provider": PROVIDER_ID, "model_id": model_id,
                                      "recommendation": result})


def make_runner(**dependencies: Any) -> Callable[[Any, Any], None]:
    def runner(job: Any, sink: Any) -> None:
        run(job, sink, **dependencies)

    return runner
