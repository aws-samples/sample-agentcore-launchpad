# Distinguish not-yet-provisioned from unhealthy in the readiness panel

Parent: `07-29-workshop-backlog`. ISSUE-001 (P2).

## Problem

On a fresh account with bootstrap complete (`WORKSHOP-READY.txt` present, Transaction
Search `ACTIVE`, `/api/health` → `ok`), the Overview header reads `系统运行正常`
(ALL SYSTEMS GO) while the SERVICE HEALTH panel shows Runtime and Evaluation as
`等待引导初始化` / `AWAITING BOOTSTRAP` with a dead LED. The panel contradicts the
header and reads as a fault, though the state is correct.

Cause: `frontend/src/pages/Overview.tsx:236-247` has one binary state —
`ready = svc === "runtime" ? active > 0 : Boolean(info?.services[svc])`, and everything
false renders the same `overview.health.pending` string. But the seven rows have two
different meanings (`backend/app/routers/overview.py:80-95`):

- **bootstrap-provisioned** — `gateway` / `memory` / `registry` / `policy` come from
  `settings.resources` ids; `observability` is the account's Transaction Search
  destination. False here genuinely means "bootstrap has not run / is incomplete".
- **usage-created** — `runtime` counts active agents (created when the operator deploys
  their first agent) and `evaluation` counts completed eval runs (created on the first
  evaluation). False here is the expected state of a correctly bootstrapped account.

`AWAITING BOOTSTRAP` is simply the wrong sentence for the second group.

## Requirements

1. The panel distinguishes three states per row: ready, **not created yet (expected)**,
   and awaiting bootstrap. The middle state must not use the fault presentation — a
   neutral LED (not the dead `off` grey used for the missing-bootstrap case is
   acceptable to reuse only if the copy carries the difference; prefer a distinct
   neutral tone) and neutral copy.
2. The not-created-yet copy names what creates the resource, so the operator knows it
   is an action they have not taken rather than a broken service. Report's example was
   "name the chapter"; use the **action** instead ("deploy your first agent" / "run
   your first evaluation") — the console is also used outside the workshop, so lab
   chapter numbers do not belong in product copy. Same information, no lab coupling.
3. The classification lives in one place, next to the existing `SERVICES` list, so
   adding a row forces a decision about which kind it is.
4. Bootstrap-missing rows keep the current `AWAITING BOOTSTRAP` wording and LED — that
   case is unchanged.
5. en + zh-CN parity for every new key.

## Acceptance Criteria

- [ ] With bootstrap complete and no agents/eval runs: Runtime and Evaluation render the
      neutral not-created-yet state naming the action; the other five render READY; the
      header still reads ALL SYSTEMS GO and nothing in the panel reads as a fault.
- [ ] With no bootstrap ids in config: gateway/memory/registry/policy still render
      `AWAITING BOOTSTRAP` with the dead LED.
- [ ] With one active agent and one completed eval run: both rows render READY with
      their existing detail (`ACTIVE · n agent(s)`, `n runs`).
- [ ] Browser evidence (fetch-stub over `/api/overview` + `/api/agents`) for the three
      states above.
- [ ] `scripts/i18n_check.py` parity passes; `make verify` passes.
