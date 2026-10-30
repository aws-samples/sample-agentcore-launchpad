"""``--self-preference`` (Round 3 R3-a): does a judge favour answers of its own model family?

The calibration pairs' questions (``tests/fixtures/judge_calibration/pairs.json``: the real
turn's prompt, its earlier turns and the tool results the agent received) are answered once
by each agent model — the same text, the same tool results, no tool calls of its own — and
every answer is judged by every judge model through the production engine path
(``calibration.judge``). Per evaluator, with S[j][a] the mean score judge j gives agent a's
answers:

- ``pref[j] = S[j][own family] − S[j][the other]`` — judge j's preference for its own family;
- ``gap = (pref[glm] + pref[kimi]) / 2`` — the own-family gap: what the two judges agree on
  (one family really answers better) cancels, a bias towards one's own family adds up;
- a 95 % bootstrap interval of ``gap`` over the questions (seeded, stdlib only).

No AWS beyond the model calls themselves (the Global Bedrock key, like ``--judge``).
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

EVALUATORS = ("Builtin.Helpfulness", "Builtin.Correctness", "Builtin.Faithfulness",
              "Builtin.Coherence", "Builtin.Conciseness")
AGENT_SYSTEM_PROMPT = (
    "You are the enterprise assistant of this conversation. Answer the user's latest message "
    "from the conversation and the tool results shown, in the user's language. Do not call "
    "tools: the results you need are already given. Do not invent facts the tool results do "
    "not contain.")
BOOTSTRAP = 2000


def family(model_id: str) -> str:
    return "glm" if "glm" in model_id else "kimi" if "kimi" in model_id else model_id


def agent_prompt(pair: dict[str, Any]) -> str:
    """The question as the agent sees it: earlier turns, the tool results, the request."""
    lines = []
    for turn in pair.get("history") or []:
        lines.append(f"User: {turn['prompt']}")
        lines += [f"Tool {t['name']} returned: {t['result']}" for t in turn["tools"]]
        lines.append(f"Assistant: {turn['answer']}")
    lines.append(f"User: {pair['prompt']}")
    lines += [f"Tool {t['name']}({t.get('arguments') or {}}) returned: {t['result']}"
              for t in pair["tools"]]
    return "\n".join(lines) + "\n\nWrite the assistant's answer to the user's latest message."


def answer_all(pairs: list[dict[str, Any]], agents: list[str], *,
               model_factory: Callable[..., Any] | None = None, concurrency: int = 4,
               ) -> dict[tuple[str, str], str]:
    """``{(pair id, agent model): answer}`` — every question answered by every agent."""
    from strands import Agent

    from app.evaluation.strands_engine import default_judge_factory

    factory = model_factory or default_judge_factory

    def one(job: tuple[dict[str, Any], str]) -> tuple[tuple[str, str], str]:
        pair, model = job
        agent = Agent(model=factory(model, max_tokens=8192), system_prompt=AGENT_SYSTEM_PROMPT,
                      callback_handler=None)
        return (pair["id"], model), str(agent(agent_prompt(pair))).strip()

    jobs = [(pair, model) for pair in pairs for model in agents]
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return dict(pool.map(one, jobs))


def work(pairs: list[dict[str, Any]], answers: dict[tuple[str, str], str],
         evaluators: tuple[str, ...] = EVALUATORS) -> list[dict[str, Any]]:
    """Calibration-shaped items: one per (question, agent model, evaluator); the side is the
    agent model, its answer judged with the pair's real tool results."""
    out = []
    for pair in pairs:
        for (pair_id, agent), text in sorted(answers.items()):
            if pair_id != pair["id"] or not text:
                continue
            side = f"agent:{agent}"
            judged = {**pair, side: {"answer": text, "tools": True}}
            for evaluator_id in evaluators:
                out.append({"pair": pair["id"], "lang": pair["lang"], "agent": agent,
                            "side": side, "variant": "base", "evaluator": evaluator_id,
                            "expected": True, "case": (judged, side, "base")})
    return out


def _mean(cells: dict[tuple[str, str, str], float], j: str, a: str, qs: list[str]) -> float:
    return statistics.fmean(cells[(q, j, a)] for q in qs)


def _gap(cells: dict[tuple[str, str, str], float], qs: list[str]) -> float:
    return ((_mean(cells, "glm", "glm", qs) - _mean(cells, "glm", "kimi", qs))
            + (_mean(cells, "kimi", "kimi", qs) - _mean(cells, "kimi", "glm", qs))) / 2


def summarize(rows: list[dict[str, Any]], *, seed: int = 17) -> list[dict[str, Any]]:
    """Per evaluator: S[judge][agent], each judge's own-family preference and the gap with
    its bootstrap interval (first rep, questions every judge scored for every agent)."""
    score: dict[tuple[str, str, str, str], float] = {}
    for r in rows:
        if r["rep"] == 0 and r["score"] is not None:
            score[(r["evaluator"], r["pair"], family(r["model"]), family(r["agent"]))] = r["score"]
    out = []
    for evaluator_id in sorted({k[0] for k in score}):
        cells = {k[1:]: v for k, v in score.items() if k[0] == evaluator_id}
        judges = sorted({k[1] for k in cells})
        agents = sorted({k[2] for k in cells})
        if judges != ["glm", "kimi"] or agents != ["glm", "kimi"]:
            continue
        questions = sorted(q for q in {k[0] for k in cells}
                           if all((q, j, a) in cells for j in judges for a in agents))
        if not questions:
            continue
        rng = random.Random(seed)
        boot = sorted(_gap(cells, [rng.choice(questions) for _ in questions])
                      for _ in range(BOOTSTRAP))
        out.append({
            "evaluator": evaluator_id, "questions": len(questions),
            "S": {f"{j}->{a}": round(_mean(cells, j, a, questions), 4)
                  for j in judges for a in agents},
            "pref_glm": round(_mean(cells, "glm", "glm", questions)
                              - _mean(cells, "glm", "kimi", questions), 4),
            "pref_kimi": round(_mean(cells, "kimi", "kimi", questions)
                               - _mean(cells, "kimi", "glm", questions), 4),
            "gap": round(_gap(cells, questions), 4),
            "ci95": [round(boot[int(0.025 * BOOTSTRAP)], 4),
                     round(boot[int(0.975 * BOOTSTRAP) - 1], 4)],
        })
    return out


def markdown(result: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    lines = [f"# Judge self-preference — {meta.get('generated_at')}", "",
             f"- code `{meta.get('sha')}` · fingerprint `{meta.get('fingerprint')}` · "
             f"{meta.get('questions')} questions (calibration pairs) · agents "
             f"{', '.join(meta.get('agents') or [])} · judges "
             f"{', '.join(meta.get('models') or [])}",
             "- `S j→a` = mean score judge j gives agent a's answers; `pref j` = what judge j "
             "adds for its own family; `gap` = the mean of both prefs (a real quality "
             "difference cancels, an own-family bias adds up).", "",
             "| evaluator | questions | S glm→glm | S glm→kimi | S kimi→glm | S kimi→kimi "
             "| pref glm | pref kimi | gap | 95 % CI |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for r in result:
        s = r["S"]
        lines.append(f"| {r['evaluator']} | {r['questions']} | {s['glm->glm']} | "
                     f"{s['glm->kimi']} | {s['kimi->glm']} | {s['kimi->kimi']} | "
                     f"{r['pref_glm']:+} | {r['pref_kimi']:+} | {r['gap']:+} | "
                     f"[{r['ci95'][0]:+}, {r['ci95'][1]:+}] |")
    return "\n".join(lines) + "\n"

