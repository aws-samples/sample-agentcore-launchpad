# Round 3 — fixture register (round-16 discovery markers)

Seven-column register (the round-14/15 shape) whose id cells carry the optional discovery markers.

| id | subsystem | severity | symptom | repro | evidence | suspected cause / file |
|---|---|---|---|---|---|---|
| N16-obs1 (r16-a; class: PRODUCT; channel: A; module: E03; repro-hash: 3f9a1c2e) | agents | P2 | **Create matrix red cell** | zip with no handler | matrix row 4 | `deployer/byoc.py` |
| N16-obs2 (explorer t1; channel: B; module: E03; repro-hash: 3f9a1c2e; session: B-E03-1/1.5h) | agents | P2 | **Same red cell seen from the console** (PRODUCT) | upload the 1-byte zip | screenshot | `deployer/byoc.py` |
| N16-obs3 (explorer t1; channel: B; module: E04; session: B-E03-1/1.5h) | chat | P3 (PRODUCT) | **Duplicate delta after reconnect** | two tabs | log | `services/chat.py` |
| N16-obs4 (r16-c; class: TEST; channel: A+B; module: E18) | users | P1 | **Lockout clock not injected** | — | test diff | `tests/test_login_protection.py` |
| N16-obs5 (found-by: user; module: E02) | assistant | High | **Proposal edit drops a key** — token=SHOULD-NOT-APPEAR | edit, approve | user screenshot | `assistant/proposals.py` |
| N16-obs6 (channel: B; module: E11; session: B-E11-1/2h) | evaluation | Low | **Scorecard tooltip truncates** | hover | shot | `v2/scorecard.ts` |
| N16-obs60 (r16-f; kind: process) | quality | — | **Mutation baseline recorded** | `make mutation` | report | — |
| N16-obs4 | users | P1 | state update: fixed in 0123abcd | — | — | fixed |
