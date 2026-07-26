# Execution notes

## What drove this task

A full re-run of `docs/lab/` against the remote prod environment
(`agentops_launchpad`, us-east-1, behind CloudFront) produced ten findings; the
full record lives at `/home/ubuntu/workspace/lab-verify-remote/REPORT.md`
(that path is outside the repo — the durable parts are folded into
`docs/issues/2026-07-26-kb-data-source-lost-on-slow-create.md` and the guide).

All twelve chapters passed on that environment. Timings were the same order of
magnitude; the optimizer independently reproduced the guide's "fabricating
precise numerical data" conclusion from its own traces, which is the strongest
signal that chapter 09's narrative is reproducible rather than lucky.

## Decisions worth remembering

**Evidence was re-captured, not edited.** The two first-deploy event streams in
chapters 02 and 03 still show `claude-sonnet-4-6` because that is what the run on
2026-07-26 actually emitted; each is followed by a real log excerpt from the
republish that moved the agent to sonnet-5. Screenshots and the span waterfall
were regenerated from new sonnet-5 runs. The alternative — swapping the model id
inside recorded output — would have made the guide's "everything here came from a
real run" claim false.

**Chapter 07's dashboard numbers grew a lot** (25 → 118 traces) because the 24H
window now includes the whole day's lab + verification activity across agents. The
table was updated to the new capture and the caption now says the window is
account-wide, rather than pretending the numbers are lab-only.

**sonnet-5 has no published price yet.** litellm's public price file carries no
Bedrock `claude-sonnet-5` entry, so `model_prices` gains an entry that *mirrors*
Sonnet 4.6 ($3/$15) with a comment saying so. Without it the observability page
would show sonnet-5 tokens with a null cost and chapter 07's cost column would
silently blank out. The periodic refresher replaces it once upstream publishes.

**Two model-id occurrences stay on 4-6 on purpose** in `frontend/src/studio/lib/
models.ts` and the two evaluation option lists: those are catalogs of *selectable*
models. This was a default change, not a removal.

**Test assertions now reference `DEFAULT_MODEL_ID`** (harness + strands template
tests) instead of a literal, so the next model bump is a one-line change rather
than a test hunt. `tests/evaluation/test_evaluator_crud.py` keeps a literal
because it asserts the exact payload sent to AWS.

## Findings recorded but deliberately NOT acted on this round

From the same verification run (they are informational, not guide errors):
`meeting-summarizer` only exists in the us-west-2 environment (chapter 03 now
tells the reader to pick any APPROVED skill instead); the evaluation run form
resets its evaluator chips on the page's poll interval, so a JS-synthesised click
does not stick (only affects automation, not humans); the remote eval scored
correctness/goal-success at 0.00 versus the guide's local numbers, which argues
the guide's own table understates how badly an un-grounded agent does.

## Spec-update judgment (Trellis 3.3)

**One spec touched.** `.trellis/spec/launchpad/managed-kb.md` describes the
create → data-source → ingestion topology; ownership of the data-source creation
step moved from the client to the backend, which is a contract change in that
document's scope. The model-default change and the guide revisions are not spec
material (no contract moved — `DEFAULT_MODEL_ID` is a value, and the guide is
product documentation).
