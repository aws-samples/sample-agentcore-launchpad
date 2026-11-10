# Acceptance standards — Launchpad-CN console modules

One file per video module (E01–E22). Each file follows `plans/acceptance/TEMPLATE.md`: scope and
roles, positive criteria per capability (`AC-E##-NN`), negative / boundary cases sized by the
module's risk score (`NC-E##-NN`), evidence to capture, mapping to existing automation by
`file::test_name` / `spec::case` with GAPs, failure classification, open rulings.

## Index

| id | module (zh / en) | file | risk score | negatives (floor) | positives | open rulings | status | writer |
|---|---|---|---|---|---|---|---|---|
| E01 | 「工作台」 / Workbench | [E01-workbench.md](E01-workbench.md) | 7.5 | 8 (≥ 3) | 7 | 2 | draft | acc-w1 |
| E02 | 「架构助手」 / Architect assistant | [E02-assistant.md](E02-assistant.md) | 12 | 14 (≥ 10) | 8 | 1 | draft | acc-w3 |
| E03 | 「Agent 管理」 / Agent management | [E03-agents.md](E03-agents.md) | 12 | 15 (≥ 10) | 7 | 0 | draft | acc-w3 |
| E04 | 「对话演练场」 / Chat playground | [E04-chat.md](E04-chat.md) | 11.5 | 14 (≥ 10) | 9 | 2 | draft | acc-w1 |
| E05 | 「可观测」 / Observability | [E05-observability.md](E05-observability.md) | 8.5 | 11 (≥ 6) | 7 | 2 | draft | acc-w1 |
| E06 | 「记忆」 / Memory | [E06-memory.md](E06-memory.md) | 9.5 | 10 (≥ 6) | 7 | 2 | draft | acc-w1 |
| E07 | 「治理」 / Governance | [E07-governance.md](E07-governance.md) | 14.5 | 16 (≥ 10) | 8 | 2 | draft | acc-w1 |
| E08 | 「出站凭证」 / Connections | [E08-connections.md](E08-connections.md) | 9.5 | 13 (≥ 10) | 6 | 2 | draft | acc-w1 |
| E09 | 「数据中心」 / Data Center | [E09-datasets.md](E09-datasets.md) | 9.5 | 13 (≥ 6) | 8 | 1 | draft | acc-w2 |
| E10 | 「评估器」 / Evaluators | [E10-evaluators.md](E10-evaluators.md) | 7.5 | 11 (≥ 3) | 7 | 1 | draft | acc-w2 |
| E11 | 「评估任务」 / Evaluation tasks | [E11-eval-tasks.md](E11-eval-tasks.md) | 9.5 | 16 (≥ 6) | 10 | 2 | draft | acc-w2 |
| E12 | 「在线评估」 / Online evaluation | [E12-online-eval.md](E12-online-eval.md) | 9.5 | 16 (≥ 6) | 8 | 2 | draft | acc-w2 |
| E13 | 「金丝雀实验」 / Canary experiments | [E13-canary.md](E13-canary.md) | 9.5 (proposed) | 16 (≥ 6) | 9 | 2 | draft | acc-w2 |
| E14 | 「评估总览」 / Evaluation insights | [E14-insights.md](E14-insights.md) | 7.5 | 7 (≥ 3) | 6 | 1 | draft | acc-w2 |
| E15 | 「技能实验室」 / Skill Lab | [E15-skill-lab.md](E15-skill-lab.md) | 14.5 | 17 (≥ 10) | 8 | 2 | draft | acc-w2 |
| E16 | 「注册中心」 / Registry | [E16-registry.md](E16-registry.md) | 10 | 17 (≥ 6) | 10 | 0 | draft | acc-w3 |
| E17 | 「知识库」 / Knowledge Bases | [E17-knowledge-bases.md](E17-knowledge-bases.md) | 12 | 14 (≥ 10) | 11 | 0 | draft | acc-w3 |
| E18 | 「用户管理」 / User management | [E18-users.md](E18-users.md) | 20 | 17 (≥ 10) | 9 | 0 | draft | acc-w3 |
| E19 | 「工作区」 / Workspaces | [E19-workspaces.md](E19-workspaces.md) | 10 | 16 (≥ 6) | 9 | 0 | draft | acc-w3 |
| E20 | 「公告管理」 / Announcements | [E20-announcements.md](E20-announcements.md) | 4 | 9 (≥ 3) | 7 | 0 | draft | acc-w3 |
| E21 | 「模型提供方」 / Model Providers | [E21-model-providers.md](E21-model-providers.md) | 10 | 15 (≥ 6) | 9 | 0 | draft | acc-w3 |
| E22 | 「视频介绍」 / Video library | [E22-video-library.md](E22-video-library.md) | 5 | 12 (≥ 3) | 10 | 0 | draft | acc-w3 |
| **total** | 22 sheets | | | **297** | 180 | 24 | | acc-w1 / acc-w2 / acc-w3 |

Risk scores come from the `VIDEO-SERIES-PLAN.md` module cards (E13 and E22 carry proposed scores; the DoD
in `QUALITY-PACK-PLAN.md` §4.2 names E02/E03/E04/E18/E08 high-risk, so E04 and E08 use the ≥ 10 floor).
`status` moves draft → reviewed → passed(date). Counts are the table rows in each sheet's §2 / §3 and
the bullets of §7 (inline rulings are rows whose text says "ruling").

## Conventions (uniform across the 22 sheets)

- Title `# E## <English name> 「<Chinese UI label>」 (section `<nav key>`, `/v2/<path>`) — acceptance standard`;
  header line `**Risk score:** … → negatives ≥ N · **Recorded build:** to fill · **Status:** draft`
  (fill the build from `GET /api/health` `version` on the recording host).
- Sections: 1 Scope and roles · 2 Capabilities and pass criteria (positive) · 3 Negative / boundary cases ·
  4 Evidence · 5 Automation mapping and GAPs · 6 Failure classification · 7 Open rulings.
- §3 columns `| id | input | expected | source | risk |`. The `risk` cell names the `QUALITY-PACK-PLAN.md`
  §3 risk (R1–R10) or a short tag that justifies the case. Sheets E02/E03/E09–E22 were normalised to this
  layout mechanically: their `risk` cells read `—` until the sheet's writer fills them at review; the
  justification currently lives in the header line and the `source` column.

## How to use

- **Status codes and `error.code` keys come from the routers and services** (`backend/app/routers/*.py`,
  `backend/app/core/route_policy.py`, `backend/app/core/errors.py`). Pydantic rejections are
  `422 validation.invalid_request`; an `AppError` without an explicit status is `400`; mapped
  botocore errors are `aws.not_found 404 / aws.validation 400 / aws.access_denied 403 /
  aws.throttled 429 / aws.conflict 409`. When the live product disagrees with a stated
  expectation, keep the expectation and register a PRODUCT row in `docs/cn/reports/bugs-round-N.md`.
- **Negative floor** (`plans/QUALITY-PACK-PLAN.md` §1): score ≥ 12 → ≥ 10 cases; 8–11 → ≥ 6; < 8 → ≥ 3.
- **Round-13 deletions are already applied** in these standards: classic console retired (V2 only,
  `cn/r13-a`); USD cost estimates and the prices routes removed from Observability, the `episodic`
  memory strategy removed, My connections folded into Connections `?view=mine` (`cn/r13-c`,
  `docs/cn/reports/bugs-round-13.md`). A case that still sees the deleted surface is PRODUCT.
- **Roles on camera:** `demo-admin` (administrator), `hr-intern` (member), anonymous. Member
  `perm:*` keys are default-granted and revocable per user (`route_policy.py`).
- **Evidence** lands in `s3://launchpad-cn-evidence-<account>/<round>/<E##>/<case-id>/` as
  `request.json`, `response.json` (redacted), `shot.png`, `ledger.redacted.json` when a table is
  involved, `cw/<session_id>.jsonl` when a Runtime call is involved. Secrets, SSM values, API keys
  and cookies never appear.
- **Failure classification** follows `tests/e2e/README.md` §Classification: `ENV` / `TEST` /
  `PRODUCT`; each module's §6 lists its own ENV signals.
- **Automation mapping** names hermetic backend tests (`backend/tests/<file>.py::<test>`) and e2e
  cases (`tests/e2e/cn/<spec>.spec.ts::<case>`). A `GAP` row carries the Q-item or ticket that owns it.

Constraints honoured by every file: C8 no AWS calls were made to write them; C2 no partition
literals beyond quoting the contract hosts; identifiers in English, Chinese UI labels quoted.
