# Real-record accuracy corpus (eval accuracy item 1)

These are redacted, real CloudWatch records of 23 CN sessions (66 turns). Each fixture also
holds the per-turn transcript the evaluation must extract from those records. The gate test
is `tests/evaluation/test_real_record_corpus.py`, and the contract is
`docs/cn/interfaces/evaluation-engine.md` §25.

## Source

research-obs315 read the records read-only from the CN account on 2026-10-23, using
`filter-log-events` on each agent's runtime log group (`rt-<sid>.json`) and on `aws/spans`
(`spans-<sid>.json`). The raw files are in `/tmp/research-obs315/raw`; run `fb8a7e230b11`
and the 10 multi-turn sessions are in `plans/RESEARCH-N17-obs315.md`. `build_corpus.py`
turns them into these fixtures:

    cd backend && uv run python tests/fixtures/eval_corpus/build_corpus.py /tmp/research-obs315/raw \
        <eval-wb cache>/sessions

The two `probe-*` fixtures come from the eval-loop probe corpus. They are eval-wb cache
entries (`backend/.eval-cache/sessions/<id>.json`, fetched read-only on 2026-10-23), named by
the first 24 characters of their session id (`CACHE_SESSIONS`). Span records keep
`resource.attributes.service.name`, which says whose agent wrote the span (N17-obs380).

| fixture | agent | turns | shape | records | size | console deviations |
|---|---|---|---|---|---|---|
| `06d7a610023c` | hr_helpdesk_wb006 | 3 | a tool call in every turn | 42 | 47 KB | 2 |
| `2335a9363850` | travel_expense_assistant | 1 | run fb8a7e230b11: no tool, refusal | 7 | 7 KB | 0 |
| `257f17b16cbb` | travel_expense_assistant | 1 | run fb8a7e230b11: preamble → kb_search + get_employee → answer | 16 | 42 KB | 0 |
| `35f49b824328` | travel_expense_assistant | 2 | tools in both turns | 28 | 59 KB | 0 |
| `3cdd38248181` | paipai_energy_agent | 4 | ok / model read timeout after tools / 2× runtime timeout (timed-out turns) | 49 | 77 KB | 2 |
| `3ebbac0b072e` | store_ops_memory_demo | 2 | long-term memory injected | 14 | 19 KB | 0 |
| `539d13ba067b` | travel_expense_assistant | 1 | run fb8a7e230b11: tools, no text before them | 16 | 42 KB | 0 |
| `7574fd2ea83d` | travel_expense_assistant | 1 | run fb8a7e230b11: 5 tools, no text before them | 34 | 77 KB | 0 |
| `7b394e2feeaa` | travel_expense_assistant | 1 | run fb8a7e230b11: preamble → 2 tools → answer | 16 | 42 KB | 0 |
| `7f557e29febe` | travel_expense_assistant | 1 | run fb8a7e230b11: preamble → kb_search → refusal | 14 | 39 KB | 0 |
| `a696365333a4` | energy_agent | 8 | long, mostly text, refusals | 65 | 213 KB | 0 |
| `b0c5a4e1918f` | hr_helpdesk_wb006 | 8 | tools in 5 turns (3× `shell` with the same `call_0` id in turn 4), refusals | 105 | 200 KB | 4 |
| `cc8cc1baf12e` | hr_helpdesk_wb007 | 5 | text / 2 turns with 2 tools / text | 63 | 73 KB | 0 |
| `d38bac2b014e` | drama_content_copilot | 3 | file attachment, max_tokens continuation, microVM restore (9 REAL span-id collisions across turns) | 72 | 229 KB | 1 |
| `dops12-mem-2` | hr_helpdesk_wb006 | 3 | long-term memory injected | 21 | 21 KB | 0 |
| `ec38059c086c` | memory_console_demo | 2 | long-term memory injected | 14 | 16 KB | 0 |
| `probe-glm-mt3-e12df09adf` | probe_stub_glm | 3 | GLM max_tokens: turn 3 wrote 10 `calculator` toolUse blocks it never executed; agent span ERROR (N17-obs381/obs373) | 42 | 45 KB | 3 |
| `probe-multi-a2a-mt8-9d8c` | probe_multiagent | 9 | calls an A2A peer: the peer's spans share the trace and `session.id` under its own `service.name` (N17-obs380) | 126 | 133 KB | 1 |
| `05440314cc974b4fb5f52f36` | target_skills_sloppy | 1 | run 756fc78ffdd4 (K3): skill loads whose SKILL.md bodies came back as tool results (N17-obs401) | 18 | 25 KB | 0 |
| `230112bf094d4790b152efc5` | target_skills_sloppy | 1 | run 2814a2fc8655 (K3): skill loads and a calculator call (N17-obs401) | 23 | 31 KB | 0 |
| `c9f5f7e5e7d647dea62d6f26` | target_skills_good | 1 | run 1891e12a5efc: AgentSkills' `<available_skills>` catalogue (4 wbtest skills) in the system message, one skill load (N17-obs399) | 21 | — | 0 |
| `d371e407437945f7a28dfa43` | target_skills | 3 | run 772ba33ffb6b: `wbtest-trip-plan` loaded in turn 1 and again in turn 3 (N17-obs402 / obs342) | 42 | — | 0 |
| `f0469605c5f0` | travel_expense_assistant | 2 | run fb8a7e230b11: one scenario replayed twice in ONE session; attempt 1 died on `internalServerException` after its tool call | 23 | 70 KB | 0 |

`edges.json` holds the edge shapes that real data lacks. Each is built from a real fixture by
a transform in `tests/evaluation/corpus_support.py`, and each carries its own expected turns:

- `257f17b16cbb-ends_after_tool`: the turn dies after its tool calls. The user saw the
  preamble and never the HR record.
- `257f17b16cbb-empty_final`: a reasoning-only end. The answer is `""`.
- `35f49b824328-collide`: every turn-2 span id repeats a turn-1 id, as after a snapshot
  restore.

Unified telemetry is converted at test time from every fixture (`corpus_support.to_unified`):
the body messages ride inline as `gen_ai.*` events on the span records, and there are no
runtime-group records.

## Fixture shape

```
{session_id, log_group, agent, shape, source, trimmed{…}, colliding_span_ids,
 expected: {turns: [{trace_id, user_prompt, answer, tools}], console_deviations: [...]},
 records: [{group: "runtime" | "aws/spans", timestamp: <event ms>, record: {...}}]}
```

The test's Logs double (`corpus_support.GroupLogs`) answers queries as follows:

- **Content query:** the runtime records, plus `aws/spans` when that group is queried, in
  event-time order (`sort @timestamp asc`).
- **Hierarchy query:** the span records.
- **`filter_log_events`:** the runtime records only. This is the console's reader.

## Expected turns: the derivation

`corpus_support.expected_turns` derives the expected turns. It reads only the RUNTIME
content records, the records the console transcript reads
(`observability.eval_turns_from_content_logs` / `eval_tool_turns_from_content_logs`), and
never the production mapper. Each trace that carries a user prompt is one turn, in time
order.

- `user_prompt`: the user message in the trace's first record, i.e. the prompt that
  opened the turn.
- `answer`: the trace's last `end_turn` own answer (content keyed `message`, without an
  `id`). An empty one stays `""`. If there is none, the last non-empty own answer of any
  finish (the text the user saw). If there is none of those either, `""`. Replayed history
  (`{"content": …}`) and tool results (`{"message", "id"}`) are never the answer.
- `tools`: the `toolUse` names of the trace's own `finish_reason`-tagged choices that RAN
  (an `execute_tool` record `{"message", "id"}` of that id exists in the trace), in time
  order, i.e. the calls the model made and executed in this turn. A call written into a
  max_tokens stop and never run is no call (N17-obs381). The builder asserts that they equal the
  trace's `execute_tool` span names, by start time, on every turn (48/48).

`console_deviations` records each place where this differs from what the console shows.
`test_corpus_expectations_are_the_console_transcript` re-derives these deviations against
the console's own extractors. There are three kinds:

- `user_prompt` (`d38b…` turn 2): the console shows the trace's LAST user message. In a
  max_tokens continuation that is the platform's own follow-up prompt ("Your previous answer
  was cut off…").
- `answer` (`3cdd…` turns 3-4): these turns timed out without any `gen_ai.choice`. The
  console's fallback shows the replayed history, which is turn 2's text (N17-obs315 MT1).
  The expected answer is `""`.
- `tools` (`06d7…`, `b0c5…`): the console's TOOL_CALL list dedupes `toolUseId` across the
  whole session. Models that number calls per response (`call_0`) repeat ids, so the console
  drops later turns' calls and repeated calls.

The console's 4000-char cut is not a deviation.

## Redaction

`build_corpus.py` handles redaction, and it was grep-checked on every file:

- **Allowlisted fields only.**
  - Span records keep the ids, name, kind, times, scope, status, and the gen_ai / session
    attributes; `resource` and every `aws.*` / `rpc.*` / access-key attribute are dropped.
  - Content records keep the ids, times, scope, `event.name` / `session.id`, and the body.
- **Masked:**
  - account id → `000000000000`
  - ARNs → `arn:aws-cn:REDACTED`
  - AKIA/ASIA keys
  - IPv4 addresses (except 127.0.0.1)
  - email addresses
  - CN mobile numbers
  - 18-digit ID numbers
- **Join ids are untouched.** A builder assertion checks that no trace, span or session id
  changed.
- **Names are kept.** Employee names and ids in tool results (Maya Chen / EMP-1024, Wei
  Zhang / EMP-4096) are the HR demo database's seed data, the same records the repo README
  shows.

## Trimming

Trimming keeps the corpus at about 1.4 MB, down from 6.3 MB raw. None of the trimmed content
is read by any extractor or assertion.

- **Dropped:**
  - runtime records without a body (application log lines);
  - span records that are not `invoke_agent` / `chat` / `execute_event_loop_cycle` /
    `execute_tool` (S3, SQS, MCP, HTTP);
  - the exception events on spans (stack traces).
  The exception is any span whose id repeats across traces, which is kept so the real
  collision survives.
- **Cut, with ` …[trimmed]` appended:**
  - system prompts → 300 chars;
  - replayed-history text blocks → 200 (their `toolUse` blocks are kept);
  - tool-result text → 1500;
  - reasoning text → 100.
