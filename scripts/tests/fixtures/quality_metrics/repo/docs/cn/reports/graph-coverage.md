# Lifecycle graph coverage

| graph | module | nodes | edges | edges covered | prime paths | prime paths covered | edge pairs | pairs covered | gaps |
|---|---|---|---|---|---|---|---|---|---|
| `agent` | E03 | 7 | 28 | 28 | 44 | 44 | 131 | 131 | 4 |
| `chat_session` | E04 | 5 | 22 | 21 | 23 | 22 | 126 | 117 | 2 |
| **total** | — | 12 | 50 | 49 | 67 | 66 | 257 | 248 | 6 |

## Loop engine (round 17, r17-b — testing.md §6.8)

| graph | walks | walk findings | neighbourhood paths | neighbourhood newly covered | cross-graph walks | cross findings |
|---|---|---|---|---|---|---|
| `agent` | 8 | 0 | 0 | 0 | 0 | 0 |
| `chat_session` | 8 | 1 (N17-obs10) | 168 | 53 | 12 (agent_x_chat 6, dataset_x_run 6) | 2 (N17-obs12, N17-obs13) |
| **total** | 16 | 1 | 168 | 53 | 12 | 2 |

## Loop rounds (user rule 2026-10-19: stop after three consecutive zero-yield rounds)

- round 1 · random walks + neighbourhood + cross-graph + soak · findings 6 (N17-obs10, N17-obs11, N17-obs12, N17-obs13, N17-obs14, N17-obs15) · streak 0
- round 2 · fault-injected walks · findings 0 · streak 1
