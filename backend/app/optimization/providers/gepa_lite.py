"""``gepa_lite`` — one GEPA-style reflective round on a Bedrock model.

GEPA (gepa-ai/gepa) evolves a text component by showing an LLM the system's
execution traces with per-example feedback, asking it to diagnose the failures
and rewrite the component, then keeping the rewrite when it scores better.
This provider runs exactly ONE such reflection — traces + judge scores +
explanations → one revised system prompt — and leaves the "does it score
better" question to the configuration A/B test the experiment runs next.
No search loop, no candidate pool (that is the `gepa_search` provider, P2).

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

Answer with ONE JSON object and nothing else:
{{"diagnosis": "<what the current instruction gets wrong, 2-5 sentences>",
  "changes": ["<one concrete change>", "..."],
  "revised_prompt": "<the full revised instruction>"}}
"""

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

EXPLANATION_MAX = 1200
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def render_reflective_dataset(evidence: list[SessionEvidence]) -> str:
    """GEPA's ``make_reflective_dataset`` shape: Inputs / Generated Outputs /
    Feedback per example, worst sessions first (the caller's order)."""
    blocks: list[str] = []
    for k, ev in enumerate(evidence, 1):
        score = "n/a" if ev.mean_score is None else f"{ev.mean_score:.2f}"
        users = [t["text"] for t in ev.turns if t["role"] == "user"]
        assistants = [t["text"] for t in ev.turns if t["role"] == "assistant"]
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
        lines.append("Feedback:")
        for r in ev.records:
            if r.error:
                lines.append(f"  - {r.evaluator_id}: evaluator error — {r.error}")
                continue
            val = "n/a" if r.score is None else f"{r.score:g}"
            label = f" ({r.label})" if r.label else ""
            why = f' — "{r.explanation}"' if r.explanation else ""
            lines.append(f"  - {r.evaluator_id} = {val}{label}{why}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_user_message(req: OptimizeRequest) -> str:
    parts = [
        "## Current instruction",
        req.current_prompt or "(empty)",
        "",
        f"## Sessions ({len(req.evidence)} of {req.stats.sessions_scored} scored, "
        "worst first)",
        render_reflective_dataset(req.evidence),
    ]
    if req.extra_feedback:
        parts += ["", "## Failure analysis from the evaluation run"]
        parts += [f"- {line}" for line in req.extra_feedback]
    return "\n".join(parts)


def parse_reflection(text: str) -> dict[str, Any] | None:
    """The first JSON object in ``text`` (fences tolerated), or None."""
    cleaned = _FENCE.sub("", text or "").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class GepaLiteProvider:
    id = PROVIDER_ID
    label = "GEPA-lite · single reflective round (Bedrock)"
    requires_source = True
    supports = ("system_prompt",)

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
        if not any(ev.mean_score is not None for ev in req.evidence):
            return OptimizeResult.failed(
                "no scored sessions in the selected run — the optimizer needs "
                "evaluator scores to reflect on",
                **base_meta,
            )
        call = converse or converse_for(req.workspace)
        started = time.monotonic()
        usage_total = {"input_tokens": 0, "output_tokens": 0, "calls": 0}

        def _ask(system: str, user: str) -> str:
            text, usage = call(req.model_id, system, [{"role": "user", "text": user}],
                               req.max_tokens)
            usage_total["calls"] += 1
            for k in ("input_tokens", "output_tokens"):
                usage_total[k] += int(usage.get(k) or 0)
            return text

        def _finish(**extra: Any) -> dict[str, Any]:
            return {
                **base_meta,
                **usage_total,
                "latency_ms": int((time.monotonic() - started) * 1000),
                **extra,
            }

        system = REFLECTION_SYSTEM.format(max_chars=req.max_chars)
        user = build_user_message(req)
        try:
            progress(f"reflecting on {len(req.evidence)} sessions with {req.model_id}…")
            parsed = parse_reflection(_ask(system, user))
            if parsed is None:
                progress("reflection returned no JSON — retrying once…")
                parsed = parse_reflection(_ask(system, user + RETRY_SUFFIX))
            if parsed is None:
                return OptimizeResult.failed(
                    "reflection model returned no parseable JSON", **_finish()
                )
            prompt = str(parsed.get("revised_prompt") or "").strip()
            if not prompt:
                return OptimizeResult.failed(
                    "reflection model returned an empty revised_prompt", **_finish()
                )
            if len(prompt) > req.max_chars:
                progress(f"candidate is {len(prompt)} chars — compressing once…")
                squeezed = parse_reflection(
                    _ask(COMPRESS_SYSTEM.format(max_chars=req.max_chars), prompt)
                )
                prompt = str((squeezed or {}).get("revised_prompt") or "").strip()
                if not prompt or len(prompt) > req.max_chars:
                    return OptimizeResult.failed(
                        f"candidate exceeds {req.max_chars} chars after one compression "
                        f"({len(prompt) or 'empty'})",
                        **_finish(),
                    )
        except ClientError as exc:
            err = exc.response.get("Error") or {}
            return OptimizeResult.failed(
                f"{err.get('Code') or 'ClientError'}: {err.get('Message') or exc}",
                **_finish(),
            )
        except Exception as exc:  # network, parsing of the SDK response, …
            return OptimizeResult.failed(f"{type(exc).__name__}: {exc}", **_finish())

        changes = [str(c) for c in (parsed.get("changes") or []) if str(c).strip()][:12]
        diagnosis = str(parsed.get("diagnosis") or "").strip()
        explanation = diagnosis
        if changes:
            explanation += ("\n" if explanation else "") + "\n".join(f"- {c}" for c in changes)
        return OptimizeResult(
            status="COMPLETED",
            recommended_prompt=prompt,
            explanation=explanation[:EXPLANATION_MAX],
            meta=_finish(changes=changes),
        )


registry.register_provider(GepaLiteProvider())
