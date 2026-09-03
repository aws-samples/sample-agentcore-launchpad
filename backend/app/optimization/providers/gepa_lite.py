"""``gepa_lite`` — one GEPA-style reflective round on a Bedrock model.

GEPA (gepa-ai/gepa) evolves a text component by showing an LLM the system's
execution traces with per-example feedback, asking it to diagnose the failures
and rewrite the component, then keeping the rewrite when it scores better.
This provider runs exactly ONE such reflection — traces + judge scores +
explanations → one revised system prompt and/or revised descriptions for the
agent's OWN tools — and leaves the "does it score better" question to the
configuration A/B test the experiment runs next. No search loop, no candidate
pool (that is the `gepa_search` provider, P2). Tool descriptions are a second
GEPA component in the same candidate: the reflection sees each session's tool
calls (name, arguments, result) and the tool-call judge verdicts, and may only
rewrite tools the platform discovered from the agent's spec/code — gateway /
MCP tools appear as context and are never proposed.

Failure contract (mirrors the AgentCore job, ISSUE-007): anything that stops a
usable prompt from being produced is a FAILED result with a reason and NO
recommended prompt — never a truncated or invented one.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from botocore.exceptions import ClientError

from app.core.config import get_settings
from app.optimization.providers import registry
from app.optimization.providers.base import (
    ConverseFn,
    OptimizeRequest,
    OptimizeResult,
    Progress,
    SessionEvidence,
)
from app.optimization.providers.bedrock_lm import converse_for
from app.optimization.providers.evidence import tool_mentions

PROVIDER_ID = "gepa_lite"

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
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _render_tool_calls(ev: SessionEvidence, own_tools: list[str]) -> list[str]:
    """The session's tool calls with results, plus which owned tools went unused."""
    calls = [t for t in ev.turns if t.get("role") == "tool_call"]
    results = {t.get("id"): t for t in ev.turns if t.get("role") == "tool_result"}
    notes = [t["text"] for t in ev.turns if t.get("role") == "note"]
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
    evidence: list[SessionEvidence],
    own_tools: list[str] | None = None,
    with_tools: bool = False,
    known_tool_names: list[str] | None = None,
) -> str:
    """GEPA's ``make_reflective_dataset`` shape: Inputs / Generated Outputs /
    (Tool calls) / Feedback per example, worst sessions first (the caller's
    order). Tool-call judge records carry no tool id, so each record is tagged
    with the tool names its explanation mentions (best effort)."""
    names = known_tool_names or own_tools or []
    blocks: list[str] = []
    for k, ev in enumerate(evidence, 1):
        score = "n/a" if ev.mean_score is None else f"{ev.mean_score:.2f}"
        users = [t["text"] for t in ev.turns if t.get("role") == "user"]
        assistants = [t["text"] for t in ev.turns if t.get("role") == "assistant"]
        lines = [f"### Example {k}  (mean score {score}, session {ev.session_id[:12]}…)"]
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
            lines.extend(_render_tool_calls(ev, own_tools or []))
        lines.append("Feedback:")
        for r in ev.records:
            if r.error:
                lines.append(f"  - {r.evaluator_id}: evaluator error — {r.error}")
                continue
            val = "n/a" if r.score is None else f"{r.score:g}"
            label = f" ({r.label})" if r.label else ""
            why = f' — "{r.explanation}"' if r.explanation else ""
            tag = ""
            if with_tools and (r.level or "").lower() == "span":
                mentioned = tool_mentions(r.explanation, names)
                tag = f" [tool-call judgement; mentions: {', '.join(mentioned) or 'n/a'}]"
            lines.append(f"  - {r.evaluator_id} = {val}{label}{why}{tag}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_tools_header(req: OptimizeRequest) -> list[str]:
    """The agent's own tools (editable) and other tools seen in traces (context)."""
    lines = ["## Tools the agent owns (current descriptions — the only editable ones)"]
    if req.tools:
        for name, desc in req.tools.items():
            seen = (req.stats.observed_tools.get(name) or {})
            usage = f" · called {seen['calls']}×" if seen.get("calls") else " · never called"
            errs = f", {seen['errors']} errors" if seen.get("errors") else ""
            lines.append(f"- `{name}`{usage}{errs}: {desc or '(no description)'}")
    else:
        lines.append("- (none discovered)")
    others = {
        n: info for n, info in req.stats.observed_tools.items() if n not in req.tools
    }
    if others:
        lines.append("")
        lines.append("## Other tools seen in traces (context only, NOT editable)")
        for name, info in others.items():
            desc = info.get("description_seen") or "(description not recorded)"
            lines.append(f"- `{name}` · called {info.get('calls', 0)}×: {desc}")
    return lines


def build_user_message(req: OptimizeRequest) -> str:
    with_tools = "tool_descriptions" in req.components
    parts = ["## Current instruction", req.current_prompt or "(empty)", ""]
    if with_tools:
        parts += render_tools_header(req) + [""]
    parts += [
        f"## Sessions ({len(req.evidence)} of {req.stats.sessions_scored} scored, "
        "worst first)",
        render_reflective_dataset(
            req.evidence, own_tools=list(req.tools), with_tools=with_tools,
            known_tool_names=list(dict.fromkeys(list(req.tools) + list(req.stats.observed_tools))),
        ),
    ]
    if req.extra_feedback:
        parts += ["", "## Failure analysis from the evaluation run"]
        parts += [f"- {line}" for line in req.extra_feedback]
    return "\n".join(parts)


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
    """The first JSON object in ``text`` (fences tolerated), or None.

    Long revised prompts make models emit raw newlines / tabs inside JSON
    strings, which the strict decoder rejects — ``strict=False`` accepts those
    control characters, so a structurally sound answer is not lost to escaping.
    """
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


class GepaLiteProvider:
    id = PROVIDER_ID
    label = "GEPA-lite · single reflective round (Bedrock)"
    requires_source = True
    supports = ("system_prompt", "tool_descriptions")

    def models(self) -> list[dict[str, str]]:
        return [
            {"model_id": m, "label": registry.label_for_model_id(m)}
            for m in get_settings().prompt_opt_models
        ]

    def default_model_id(self) -> str | None:
        return get_settings().prompt_opt_default_model_id

    def optimize(
        self,
        req: OptimizeRequest,
        progress: Progress,
        converse: ConverseFn | None = None,
    ) -> OptimizeResult:
        base_meta: dict[str, Any] = {
            "evidence_sessions": req.stats.sessions_selected,
            "evidence_records": req.stats.records,
            "sessions_without_transcript": (
                req.stats.sessions_selected - req.stats.sessions_with_transcript
            ),
        }
        want_prompt = "system_prompt" in req.components
        want_tools = "tool_descriptions" in req.components
        components = tuple(
            c for c in ("system_prompt", "tool_descriptions") if c in req.components
        ) or ("system_prompt",)
        if want_tools:
            base_meta.update(
                sessions_with_tool_calls=req.stats.sessions_with_tool_calls,
                tool_calls_seen=req.stats.tool_calls_seen,
            )
        if not any(ev.mean_score is not None for ev in req.evidence):
            return _fail_both(
                "no scored sessions in the selected run — the optimizer needs "
                "evaluator scores to reflect on",
                want_tools, base_meta,
            )
        # tool component preconditions — decided before spending a model call
        tool_precheck: tuple[str, str] | None = None
        if want_tools and not req.tools:
            tool_precheck = ("no-tools", "the agent exposes no discoverable tools")
        elif want_tools and req.stats.tool_calls_seen == 0:
            tool_precheck = (
                "no-tool-calls",
                "no tool calls in the selected run — pick a run whose prompts exercise "
                "the agent's tools",
            )
        if tool_precheck and not want_prompt:
            status, reason = tool_precheck
            return OptimizeResult(
                status="FAILED", error=reason, meta=base_meta,
                tool_descriptions={}, tool_status=status, tool_error=reason,
            )
        if tool_precheck:  # prompt still proceeds; the tool side is settled
            want_tools = False
            components = ("system_prompt",)

        def _fail(reason: str, meta: dict[str, Any]) -> OptimizeResult:
            # one call, one outcome for every component the call covered; a tool
            # side settled by the pre-check keeps its verdict (no model call was
            # ever made for it, so the model failure is not its reason)
            res = _fail_both(reason, want_tools, meta)
            if tool_precheck:
                res.tool_status, res.tool_error = tool_precheck
                res.tool_descriptions = {}
            return res

        call = converse or converse_for(req.workspace)
        started = time.monotonic()
        usage_total = {"input_tokens": 0, "output_tokens": 0, "calls": 0}
        budget = {"max_tokens": req.max_tokens, "truncated": False}

        def _ask(system: str, user: str) -> str:
            text, usage = call(req.model_id, system, [{"role": "user", "text": user}],
                               budget["max_tokens"])
            usage_total["calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage_total[k] += int(usage.get(k) or 0)
            # a reply cut off by the output limit can never parse — remember it so
            # the retry grows the budget instead of just asking for cleaner JSON
            budget["truncated"] = str(usage.get("stop_reason") or "") == "max_tokens"
            if budget["truncated"]:
                budget["max_tokens"] = min(budget["max_tokens"] * 2, MAX_TOKENS_CEILING)
            return text

        def _finish(**extra: Any) -> dict[str, Any]:
            return {
                **base_meta,
                **usage_total,
                "latency_ms": int((time.monotonic() - started) * 1000),
                **extra,
            }

        system = REFLECTION_SYSTEM.format(
            max_chars=req.max_chars, components=COMPONENTS_TEXT[components]
        )
        user = build_user_message(req) if components == tuple(req.components) else (
            build_user_message(
                OptimizeRequest(**{**req.__dict__, "components": components})
            )
        )
        try:
            progress(f"reflecting on {len(req.evidence)} sessions with {req.model_id}…")
            raw = _ask(system, user)
            parsed = parse_reflection(raw)
            if parsed is None:
                if budget["truncated"]:
                    progress(
                        "reflection output hit the token limit — retrying with "
                        f"{budget['max_tokens']} tokens…"
                    )
                    parsed = parse_reflection(_ask(system, user + TRUNCATED_SUFFIX))
                else:
                    progress("reflection returned no JSON — retrying once…")
                    raw = _ask(system, user + RETRY_SUFFIX)
                    parsed = parse_reflection(raw)
            if parsed is None:
                reason = (
                    "reflection output truncated by the token limit "
                    f"({usage_total['output_tokens']} output tokens over "
                    f"{usage_total['calls']} calls) — raise prompt_opt_max_tokens"
                    if budget["truncated"]
                    else "reflection model returned no parseable JSON — "
                    + describe_unparseable(raw)
                )
                return _fail(reason, _finish())
            prompt = str(parsed.get("revised_prompt") or "").strip()
            if want_prompt and not prompt:
                return _fail("reflection model returned an empty revised_prompt", _finish())
            if not want_prompt:
                prompt = ""  # frozen — whatever the model echoed is discarded
            if want_prompt and len(prompt) > req.max_chars:
                progress(f"candidate is {len(prompt)} chars — compressing once…")
                squeezed = parse_reflection(
                    _ask(COMPRESS_SYSTEM.format(max_chars=req.max_chars), prompt)
                )
                prompt = str((squeezed or {}).get("revised_prompt") or "").strip()
                if not prompt or len(prompt) > req.max_chars:
                    return _fail(
                        f"candidate exceeds {req.max_chars} chars after one compression "
                        f"({len(prompt) or 'empty'})",
                        _finish(),
                    )
        except ClientError as exc:
            err = exc.response.get("Error") or {}
            return _fail(
                f"{err.get('Code') or 'ClientError'}: {err.get('Message') or exc}", _finish()
            )
        except Exception as exc:  # network, parsing of the SDK response, …
            return _fail(f"{type(exc).__name__}: {exc}", _finish())

        changes = [str(c) for c in (parsed.get("changes") or []) if str(c).strip()][:12]
        diagnosis = str(parsed.get("diagnosis") or "").strip()
        explanation = diagnosis
        if changes:
            explanation += ("\n" if explanation else "") + "\n".join(f"- {c}" for c in changes)
        result = OptimizeResult(
            status="COMPLETED" if want_prompt else "FAILED",
            recommended_prompt=prompt if want_prompt else None,
            explanation=explanation[:EXPLANATION_MAX] if want_prompt else "",
            error=None if want_prompt else "instruction frozen — tool descriptions only",
            meta=_finish(changes=changes),
        )
        if tool_precheck:
            result.tool_status, result.tool_error = tool_precheck
            result.tool_descriptions = {}
        elif want_tools:
            kept, dropped = validate_tool_descriptions(
                parsed.get("tool_descriptions"), req.tools
            )
            tool_changes = [
                str(c) for c in (parsed.get("tool_changes") or []) if str(c).strip()
            ][:12]
            result.tool_descriptions = kept
            result.tool_status = "COMPLETED"
            result.tool_explanation = "\n".join(f"- {c}" for c in tool_changes)[:EXPLANATION_MAX]
            result.meta.update(
                tool_descriptions_proposed=len(kept) + sum(dropped.values()),
                tool_descriptions_dropped=dropped,
                tool_changes=tool_changes,
            )
        return result


def _fail_both(reason: str, want_tools: bool, meta: dict[str, Any]) -> OptimizeResult:
    """One call, one outcome: a failure fails every requested component."""
    res = OptimizeResult.failed(reason, **meta)
    if want_tools:
        res.tool_status, res.tool_error, res.tool_descriptions = "error", reason[:300], {}
    return res


registry.register_provider(GepaLiteProvider())
