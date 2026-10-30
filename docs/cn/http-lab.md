# HTTP lab deployment and operations

This is an explicit opt-in for an isolated demonstration environment, with synthetic
data and dedicated passwords. HTTP exposes passwords, session cookies and content
to the network. It does not weaken authorization: `run_mode=prod` and the login gate
remain enabled. There is one account/session/permission/data model, not separate
HTTP and HTTPS users. Default HTTPS deployments are unchanged.

## New environment

Use a release containing this feature on both the operator checkout and
`parameters.GitBranch`; the older `cn-v2026.10.28.2` does not contain it. Copy
`config/deploy-cn.example.json` and add top-level `console_http_lab: true` and
`http_port: 8088`. Keep `console_entry: public_nlb`, `TlsMode: selfsigned` and
`ConsolePort: 8443`. The distribution installer creates the HTTP site after first
boot and persists the mode and primary URL. The NLB adds an HTTP-health-checked
target group and TCP listener. Only the NLB security group can reach EC2's new port.
No direct EC2 HTTP ingress is created. Ports 80, 443 and 8080 are not supported here.

Use the ordinary `deploy_cn.py plan/apply --config ... --state ...` workflow.
Every resume retains configuration/commit/stack ownership checks and obtains fresh
host and HTTP readiness evidence. `outputs.ConsoleUrl` is the only user-facing
entry: `http://<NLB-IP>:8088`. Do not edit an old state/config pair to change mode.
HTTPS on the original port is retained for operations and rollback, not advertised
as an alternative lab entry. A client's HTTPS-first browser policy or network may
still require an HTTP confirmation or block a nonstandard port.

## Existing environment: operator maintenance

Read `../agent-runbook-prod.md`. Freeze the reviewed source commit, inspect actual
running chat/deploy/evaluation/recommendation tasks and wait for their completion.
Back up SQLite with its backup API (including WAL contents), config, secret env,
nginx, current dist and runtime source. Keep backup permissions restricted. A
rollback must retain the current database, including writes made after the backup.

Inventory existing as-user tools before changing the origin. Their return URLs
are baked into runtimes and registered on workload identities. Only affected agents
need a reviewed callback migration/republication; do not redeploy all agents.
Existing allowlist entries must be retained. No as-user agents means no runtime
updates are required. Providers can require HTTPS return URLs: validate the actual
provider before claiming OAuth compatibility. Hiding its HTTPS link cannot repair
a callback, and preserving an old different-host callback alone does not share
the browser's host-only login cookie.

After the exact new source has been installed in `/home/ubuntu/launchpad`, prepare
the host during the maintenance window (replace the documentation IP):

```bash
cd /home/ubuntu/launchpad
sudo bash scripts/configure_http_lab_host.sh http://192.0.2.10:8088
sudo bash scripts/host-update.sh --branch YOUR_FROZEN_RELEASE_REF
```

The first command does not restart the backend. It validates the existing TLS
listener, saves rollback files under `/etc/launchpad/http-lab-config.*`, updates
`/etc/launchpad/env`, renders both nginx sites, tests nginx and reloads it. The
updater owns the backend restart. If code is installed with a custom maintenance
wrapper, it must preserve the same idle, exact-SHA and backup checks.

From the operator checkout, update the **existing** NLB stack:

```bash
bash scripts/ec2-console-nlb.sh update \
  --ec2-stack YOUR_EXISTING_HOST_STACK --stack-name YOUR_EXISTING_NLB_STACK \
  --listener-port 8443 --http-lab --http-port 8088 --no-execute
```

Inspect the change set. It should add only HTTP listener/target-group/security
rules and outputs; the EC2, NLB, EIP, OpenSearch and existing HTTPS resources must
not be replaced. Execute the reviewed change set, or rerun the command without
`--no-execute`. Match all existing non-default stack parameters when invoking this
general helper; do not change an existing access/network policy accidentally.
The new public port is deliberately unrestricted; no company/VPN CIDR is inferred.

Persisted host values are `LAUNCHPAD_CONSOLE_HTTP_LAB=true`,
`LAUNCHPAD_CONSOLE_HTTP_PORT=8088`, `LAUNCHPAD_CONSOLE_ENTRY=public_nlb`,
`LAUNCHPAD_PUBLIC_BASE_URL=http://<NLB-IP>:8088`, and empty
`LAUNCHPAD_OAUTH_RETURN_URL` (derived from the primary URL). `host-update.sh`
refreshes both sites and does not replace the NLB IP from IMDS. Do not add a
conflicting systemd override. `configure_http_lab_host.sh` can be rerun to repair
the HTTP site; a routine update refuses a missing site rather than silently
advertising a dead primary URL.

## Acceptance and rollback

Use the actual public HTTP origin in a normal browser, not localhost or an
insecure-origin browser override. Verify login persistence/logout, registration
policy, uploads, relative API/SSE, policy/audit screens and evaluation preview;
run only scoped synthetic chat/evaluation acceptance. Verify unauthenticated APIs
still return 401 and existing ownership negatives remain denied. Check new
HTTP target health, effective settings, source/dist provenance and unchanged
business data/resources. Confirm nginx overwrites forwarding headers and the API
still binds loopback. No backend/CDK resource rebuild is required merely for the
network entry.

In lab mode nginx does not set HSTS on HTTP; its retained TLS site sends
`max-age=0` to clear an older HSTS policy. The application uses a lab cookie name
to avoid collisions with an existing Secure cookie. Users log in again when
switching into/out of this mode; this is not account/session isolation.

For rollback, first stop advertising HTTP, restore the saved env and nginx files,
disable/remove only `launchpad-http-lab`'s enabled-site link, test/reload nginx and
restart the backend in an idle window. Restore code/dist if required while
retaining current SQLite data. Then update the same NLB stack with `--no-http-lab`
to remove only its conditional HTTP resources. The original HTTPS address and
certificate remain available. Restore original public/OAuth origin and any
callback migration together. Never reuse the old database backup as a routine
code rollback.

## Workshop material

Set `console_http_lab: true` and the same HTTP primary origin in `console` and
`api_base` in the separate workshop config. Use a new workshop state directory
for a new initialization; existing state remains tied to its original digest.
Do not rerun `workshop.py all` simply to rewrite a deployed environment's URL:
prepare an environment-specific handoff page and keep historical manifests,
screenshots and completed results unchanged. A newly rendered manual must name
its own environment and evidence; changing a reference environment's address does
not make its screenshots or acceptance results apply to another environment.
