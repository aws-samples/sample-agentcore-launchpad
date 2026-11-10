# deploy-ops ledger (fixture)

- 2026-01-10T08:15:07Z default env DONE: host 0c3e0e2f, backend restarted, health 200
- 2026-01-15T09:12:13Z r10 default DONE+VERIFIED (unit lp-r10-d): host-update done e15390af → 10254862, cdk: no changes
- 2026-01-15T09:16:46Z r10 t1 DONE+VERIFIED (unit lp-r10-t1 Result=success, 09:07:00Z -> 09:16:00Z ~9 min): host-update done e15390af -> **10254862**, restart ok
- 2026-01-16T15:15:43Z GO 6d: create-stack from template, --disable-rollback
- 2026-01-16T16:00:00Z t1 frontend rolled back to dist b990afc8 (pin), backend untouched
- 2026-01-17T16:55:10Z deploy-ops-2 T20 BACKUP-RESTORE DRILL on t1 DONE, fully reverted. Restore: stop 16:52:49.9Z -> healthy 16:52:53.0Z = 3.1 s; password=SHOULD-NOT-APPEAR
- 2026-01-17T17:00:00Z canary control-wins → rollback by the product (not an ops rollback)
