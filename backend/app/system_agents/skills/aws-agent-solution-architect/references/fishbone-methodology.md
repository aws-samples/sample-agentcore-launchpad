# Agent-DLC five-dimension fishbone (DEFINE stage)

The fishbone records the **launch barriers the customer confirmed** for one agent
scenario — never the facilitator's architecture ideas. It belongs to the DEFINE stage:
discover problems first, discuss implementation afterwards. Use it

- before or during a Workshop to surface the target team's barriers,
- at the start of round three of the intake, for every project stage (greenfield
  included), before the pain-point list it then seeds, and
- whenever the customer asks for it ("生成鱼骨图", "fishbone", "上线障碍分析",
  "五维鱼骨图", "agent-dlc").

The console renders the result from the structured `fishbone` member of the proposal
block (see "Output" below); you never write files, XML or images.

## Six dimensions

| Key | Label | Core question |
|---|---|---|
| `cognition` | 认知 Cognition | Does it understand? |
| `quality` | 质量 Quality | Is it reliable? |
| `responsibility` | 责任 Responsibility | Who is accountable when it fails? |
| `cost` | 成本 Cost | Is it worth it? |
| `performance` | 性能 Performance | Can it take the load? |
| `other` | 其他 Other | What else would stop it from running? |

`other` is for organisation, process, integration and cross-team dependencies that truly
fit none of the five; when in doubt, use the closest main dimension. One barrier has ONE
primary dimension; cross-dimension impact goes into its `evidence`, never a duplicate note.

## Non-negotiable rules

1. **One question at a time.** This is guided discovery, not a questionnaire. Ask, wait,
   then decide the follow-up from the answer.
2. **Business language.** Do not expose the framework ("the cognition dimension") unless
   the customer asks what the framework is; the labels bias how people describe problems.
3. **Confirm every note.** Rewrite each barrier as one sticky note, read it back, and
   record it only after the customer confirms the wording. The diagram is the customer's
   artefact, not your inference.
4. **Probe, but never invent.** A dimension the customer did not mention is not skipped:
   pick at least two probing directions from the tables below (one in `express` mode),
   phrase them in the customer's own context and ask. If the customer then confirms
   "nothing there", accept the dimension as empty — never fill it from industry
   assumptions. Asking is a duty; inventing is forbidden.
5. **No solutions in discovery.** No AWS services, models, RAG, databases. Solutions bias
   the problem statement.
6. **Solution → barrier reversal.** When the customer offers a solution, ask "without
   that, what concrete error is most likely?" — the solution goes to the parking lot,
   the underlying risk becomes the barrier.
7. **A fallback is not "no barrier".** "We have review / approval / a human backstop" is
   a solution. Ask a neutral factual question — "when did that review last trigger?",
   "which failures does it cover, and which not?" — and record a barrier only when the
   customer confirms one.
8. **Keep the original meaning.** Preserve the customer's numbers, system names and
   examples: "200+ rules" is worth more than "complex business knowledge".
9. **Redact.** No personal data, account ids or credentials in quotes or evidence; use
   `[REDACTED]` or a generic role name.

## Workflow

### 1. Define the scenario (before any barrier)

Ask one at a time, adapting the follow-ups: who uses the agent; if it could only do one
thing well, which task; which systems, documents or data it must consult before it
answers or acts; whether it only answers or also queries / creates / changes / approves /
notifies; which single error would stop the launch immediately. Summarise the scenario in
one sentence and get it confirmed. Record `use_case` (the scenario), `service_target`
(`internal`, `b2b` or `b2c` — it decides how strict accuracy and safety must be) and
`customer` (the organisation name the customer uses; never an account id).

### 2. Discover barriers, dimension by dimension

Rhythm for every dimension: **opening question → listen → probe** (pick a direction from
the table, phrase it in the customer's language) **→ distil** (one sticky note, ≤ 50
characters, one barrier per note, facts not solutions, numbers and system names kept)
**→ confirm → move on** when the dimension is exhausted or has three strong barriers.
The tables give probing *directions*, not scripts; never read them out.

**认知 Cognition** — opening: "What is this agent most likely to get wrong or not
understand? Can you give one real example?"

| Barrier type | Signals in the answer | Probe |
|---|---|---|
| Rule complexity | "many clauses", "many cases" | How many rules? In one place or across systems? |
| Terminology | abbreviations, internal codes | Which terms get confused? Where do internal and external readings differ? |
| Context dependence | "depends", differs by role / region / product | Under which conditions does the same question change answer? How would the agent know which case applies? |
| Knowledge freshness | "changes often", "policy updates" | How fast must the agent know about a change? Who notifies? |
| Technical / system knowledge | code, architecture, APIs, topology | Which technical documents or system relations must it understand? Are they current? |
| Opaque logic | "we cannot explain it ourselves" | If insiders cannot state the logic, will users accept the agent's judgement? |

**质量 Quality** — opening: "Which answer or result would make users stop trusting it
at once?"

| Barrier type | Signals | Probe |
|---|---|---|
| Consistency | "different every time", "contradicts itself" | Same question asked repeatedly — which inconsistency worries you most? |
| Completeness | "missed", "not checked" | Which required steps or checks are most often skipped? |
| Actionability | "gave a plan we could not use" | Can users execute the output directly or must they rework it? |
| Specificity | "boilerplate", "same reply" | Have different questions received the same answer? |
| Source trust | "citation", "basis" | Which answers must carry a source? How do users verify? |
| Artefact quality | agent produces documents / code / reports | Is there a quality standard? Who accepts the output? |

**责任 Responsibility** — opening: "What must it never see, say or do? When must it stop
and hand over to a person?"

| Barrier type | Signals | Probe |
|---|---|---|
| Data permissions | "privacy", "sensitive", "personal data" | Which data may only certain roles see? Where are the permission boundaries? |
| Approval of actions | "approval", "must confirm", "not automatic" | Which actions need human approval? When the boundary is unclear, default to act or not act? |
| Escalation | "if something goes wrong", "human takes over" | What must be escalated to a person? What triggers it? |
| Compliance / standards | "contract", "template", "audit" | Which formats or compliance requirements must the output meet? |
| Traceability | "cannot find out", "accountability" | After an incident, can you reconstruct what it did and on what basis? |

**成本 Cost** — opening: "If usage grew tenfold, which cost would run away first?"

| Barrier type | Signals | Probe |
|---|---|---|
| Invocation cost | "calls", "tokens", "API" | How many calls per typical task? Is there a ceiling? |
| Repetition | "asked again and again", "cache" | What share of questions repeat? Any caching or reuse? |
| Human cost | "hand over to a human", "cannot solve" | How much falls back to people? Could it increase their load? |
| Resolution efficiency | "several rounds", "unresolved" | How many turns to resolve one issue? What happens to the unresolved? |
| Operations cost | "maintenance", "updates" | How much ongoing effort do the agent and its knowledge need? |

**性能 Performance** — opening: "How long will users wait at most? Roughly how many
use it at the same time?"

| Barrier type | Signals | Probe |
|---|---|---|
| Latency | "seconds", "real time" | Which tasks must finish in seconds, which may take minutes? |
| Concurrency | "peak", "queue" | Peak volume? Do users wait or leave when queued? |
| Degradation / timeouts | "system timeout", "stuck" | When an external system is slow or down, how should the agent behave? |
| Long-run stability | "weeks", "7×24", "availability" | Concerns after continuous running? Availability target? |
| Cycle time | tasks measured in days | Are there tasks whose completion is measured in days? What SLA? |

**其他 Other** — opening: "Beyond the agent's own ability, which system, organisation or
process issue would stop it from running?"

| Barrier type | Signals | Probe |
|---|---|---|
| Integration | "connect to", "existing systems" | Which systems must it integrate with? Stability and permission concerns? |
| Engineering / release | "release", "testing", "go-live process" | How are the agent's own changes tested and released? By whom? |
| Feedback loop | "feedback", "complaints" | How do user feedback and failures reach the improvement process? |
| Ownership | "who is responsible", "cross-team" | Who owns long-term operation? How are cross-team dependencies coordinated? |

General probing techniques (any dimension): **make it concrete** ("one real example?"),
**quantify** ("roughly how many / how long?"), **worst case** ("if it happened, what is
the worst consequence?"), **reverse the solution** (rule 6), **probe the boundary**
("are there similar cases?" — one round only).

Depth guarantee: `standard` mode probes at least two directions per dimension before
moving on, even when the first answer is "no problem"; `express` mode uses the opening
question plus one probe, covering responsibility first. A dimension with three strong
barriers may end early. When the customer says a direction does not apply, accept it
and do not press.

Coverage per dimension: `confirmed` (≥ 1 confirmed barrier), `explored_empty` (probed as
required and the customer explicitly confirmed there is nothing) or `unresolved`
(answers stayed vague — the barrier is neither confirmed nor ruled out; it does not enter
the diagram).

### 3. Prioritise and confirm

Each dimension has three slots. With more than three confirmed barriers ask: "if only
three could stay, which three are most likely to block the launch?" Mark those
`selected: true`; the rest stay in the data with `selected: false`. Read every selected
note back, allow rewording, moving and removal, and get an explicit final confirmation
before you emit the block.

## Output — the `fishbone` member of the proposal block

Emit the data inside the proposal JSON (the `fishbone` member described in the
platform protocol), never as a separate file:

```json
"fishbone": {
  "version": 1,
  "customer": "<organisation name as the customer uses it>",
  "date": "YYYY-MM-DD",
  "use_case": "<one-sentence scenario>",
  "service_target": "internal | b2b | b2c",
  "coverage": {"cognition": "confirmed", "quality": "explored_empty",
               "responsibility": "confirmed", "cost": "confirmed",
               "performance": "confirmed", "other": "unresolved"},
  "barriers": {
    "cognition": [{"sticky_text": "≤ 50 chars, one barrier, facts only",
                   "evidence": "example or number the customer gave",
                   "customer_quote": "redacted original words",
                   "confirmed": true, "selected": true}],
    "quality": [], "responsibility": [], "cost": [], "performance": [], "other": []
  },
  "parking_lot": [{"original": "solution the customer proposed",
                   "converted_to": "cognition[0]"}]
}
```

Only barriers the customer stated **and confirmed in this conversation** may carry
`confirmed: true`; only confirmed barriers may be `selected`; at most three selected per
dimension. Coverage `confirmed` requires at least one confirmed barrier in that
dimension. If the intake never reached the fishbone, omit the member entirely rather
than emitting an empty or invented one. The fishbone describes barriers; the design and
the golden tests that follow must trace back to them, but the fishbone itself contains
no solution.
