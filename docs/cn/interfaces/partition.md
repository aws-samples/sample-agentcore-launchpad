# Interface: partition (N1 → all nodes)

Frozen 2026-10-08. Backend code never writes `arn:aws:` or `.amazonaws.com`; `scripts/cn_lint.sh` (in `make verify`) enforces it.

## WorkspaceContext (backend/app/services/workspace.py)

| member | type | value for cn-northwest-1 / 123456789012 |
|---|---|---|
| `partition` | `str` (property) | `"aws-cn"` |
| `dns_suffix` | `str` (property) | `"amazonaws.com.cn"` |
| `arn(service, resource, region=None, account=None)` | `str` | see below |

`region`/`account` default to the context's; pass `""` for an empty segment, `"*"` for a wildcard.

```python
ctx = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
ctx.arn("bedrock-agentcore", "runtime/*")
# arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:runtime/*
ctx.arn("s3", "bucket/prefix/*", region="", account="")   # arn:aws-cn:s3:::bucket/prefix/*
ctx.arn("iam", "role/x", region="")                        # arn:aws-cn:iam::123456789012:role/x
ctx.arn("bedrock", "foundation-model/m", region="*", account="")  # arn:aws-cn:bedrock:*::foundation-model/m
ctx.service_principal("bedrock-agentcore")   # == "bedrock-agentcore.amazonaws.com" in EVERY partition (N11-bug5)
f"https://bedrock-agentcore.{ctx.region}.{ctx.dns_suffix}/…"  # endpoint hostnames
```

## Helpers without a context
- `app.services.aws_clients.partition_for_region(region) -> str` (cached; botocore endpoint data)
- `app.services.aws_clients.dns_suffix_for_partition(partition) -> str`
- `app.services.agent_iam.RoleContext.partition / .dns_suffix / .arn(...)` — same semantics in the per-agent policy builder
- `app.services.workspace_iam._arn(region, account_id, service, resource, *, wildcard_region=False)` / `_global_arn(region, service, resource)`

## CDK (infra/stacks)
`f"arn:{self.partition}:…"` and `f"bedrock-agentcore.{self.url_suffix}"`; `iam.ServicePrincipal` substitutes the suffix itself.

## Frontend
No partition knowledge except `lib/inbound-auth.ts::bearerInvokeUrl` (derives the suffix from the ARN's partition segment). Placeholders use `arn:aws-cn:` / `amazonaws.com.cn`.

## Lint
`bash scripts/cn_lint.sh` scans `backend/app infra/stacks frontend/src scripts` (tests excluded) for `arn:aws:`, `\.amazonaws\.com([^.]|$)` and the escaped-regex form `amazonaws\\.com`; a line containing `cn-lint: allow` is exempt (the mapping table in `aws_clients.py`, partition-tolerant regexes).
