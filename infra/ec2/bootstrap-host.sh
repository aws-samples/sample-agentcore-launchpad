#!/bin/bash
# Launchpad-CN control-plane host bootstrap — the BODY of the EC2 first boot.
#
# N11-r5-bug1: EC2 caps instance user data at 16384 bytes and the template's
# UserData had grown past it (17,680 B raw since 0cd37aaf), so `aws cloudformation
# deploy` refused the Instance before anything ran. The template's UserData is now a
# small stub (infra/ec2/launchpad-cn-ec2.yaml, `Instance.Properties.UserData`) that
# installs the AWS CLI, fetches the deploy key, clones the repo and then
# `exec`s THIS file from the fresh checkout:
#
#   /home/ubuntu/launchpad/infra/ec2/bootstrap-host.sh   (run as root by cloud-init)
#
# Everything the body needs from the CloudFormation parameters / pseudo parameters
# arrives through /etc/launchpad/bootstrap.env (root:root 0600, one KEY=value line
# per LAUNCHPAD_BOOT_* key, written by the stub from `Fn::Sub`). That file carries
# NO secrets: the Global Bedrock API key, the optional SiliconFlow key, the console password and the deploy key
# stay in SSM and are read here (or by the stub) with the instance role — spec C4.
# The contract between stub and body is enforced by
# infra/tests/test_ec2_template.py (every key written == every key required here).
#
# A change to this file is a CODE change: it is picked up by the next cold start /
# instance replacement (the clone happens before it runs). It is NOT re-run by
# scripts/host-update.sh, which updates a live host in place (deploy.md §8).
#
# No `set -x` anywhere in this file: the Global Bedrock API key and the console
# admin password are each read into a shell variable and only ever written to
# /etc/launchpad/env (0600) — neither must ever reach stdout/stderr, which this
# script tees to a log file.
set -Eeuo pipefail

# The stub already redirected its own stdout/stderr into the bootstrap log and
# `exec`ed us with those descriptors inherited; only a by-hand run of this file
# (diagnosis after a failed boot) needs to attach the log itself.
if [ -z "${LAUNCHPAD_BOOTSTRAP_LOG_ATTACHED:-}" ]; then
  exec > >(tee -a /var/log/launchpad-bootstrap.log | logger -t launchpad-bootstrap -s 2>/dev/console) 2>&1
  export LAUNCHPAD_BOOTSTRAP_LOG_ATTACHED=1
fi

BOOTSTRAP_ENV=/etc/launchpad/bootstrap.env
[ -r "$BOOTSTRAP_ENV" ] || { echo "$BOOTSTRAP_ENV missing — the UserData stub writes it" >&2; exit 1; }
# shellcheck source=/dev/null
. "$BOOTSTRAP_ENV"

# The stub ↔ body contract: every key the body reads is declared here (and the
# template test asserts the stub writes exactly this set — no missing, no unused).
: "${LAUNCHPAD_BOOT_REGION:?}"                              # <- AWS::Region
: "${LAUNCHPAD_BOOT_STACK_NAME:?}"                          # <- AWS::StackName
: "${LAUNCHPAD_BOOT_SIGNAL_RESOURCE:?}"                     # the Instance logical id
: "${LAUNCHPAD_BOOT_ENV_NAME?}"                             # <- EnvName (may be empty)
: "${LAUNCHPAD_BOOT_MEMORY_DOMAIN_MODE:=shared}"             # older template compatibility
: "${LAUNCHPAD_BOOT_AUTH_PASSWORD_POLICY:=strict}"
: "${LAUNCHPAD_BOOT_AUTH_PASSWORD_MIN_LENGTH:=12}"
: "${LAUNCHPAD_BOOT_TLS:?}"                                 # <- UseTlsCookieFlag: true|false
: "${LAUNCHPAD_BOOT_CONSOLE_PORT:?}"                        # <- ConsolePort
: "${LAUNCHPAD_BOOT_PIP_INDEX_URL:?}"                       # <- PipIndexUrl (the host mirror)
: "${LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL?}"                  # <- PipExtraIndexUrl (may be empty)
: "${LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL:?}"             # <- CodeBuildPipIndexUrl
: "${LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL:?}"    # <- CodeBuildPipFallbackIndexUrl
: "${LAUNCHPAD_BOOT_NPM_REGISTRY:?}"                        # <- NpmRegistry
: "${LAUNCHPAD_BOOT_BEDROCK_KEY_PARAMETER:?}"               # <- GlobalBedrockApiKeyParameter (an SSM name, not the key)
: "${LAUNCHPAD_BOOT_SILICONFLOW_KEY_PARAMETER?}"            # <- SiliconFlowApiKeyParameter (an SSM name or empty; N-R6)
: "${LAUNCHPAD_BOOT_CONSOLE_PASSWORD_PARAMETER:?}"          # <- ConsolePasswordParameter (an SSM name, not the password)

export DEBIAN_FRONTEND=noninteractive
# System preset deployment can take minutes; flush Python stage messages into
# cloud-init/SSM logs so first-boot progress remains visible while AWS converges.
export PYTHONUNBUFFERED=1
# N11 r2 (live failure #2): the pip-installed AWS CLI is v1, which does NOT read
# the region from IMDS the way v2 does — export it before any `aws ` call;
# `runuser -m` below preserves it for the ubuntu subshell too.
export AWS_DEFAULT_REGION="$LAUNCHPAD_BOOT_REGION"
export AWS_REGION="$LAUNCHPAD_BOOT_REGION"
# The stub installed the CLI (pip, PipIndexUrl mirror) before it could clone us.
command -v aws >/dev/null || { echo "aws cli missing" >&2; exit 1; }

# The rest of the host toolchain: the stub only installed what the clone needed.
apt-get update
apt-get install -y ca-certificates curl unzip nginx git build-essential \
  python3 python3-dev python3-pip libssl-dev jq openssl

# N13-obs13: Ubuntu's unattended-upgrades rebooted both console hosts at 02:04Z (10:04 Beijing,
# inside storyline / recording hours) after a kernel update. Keep the automatic security
# upgrades, but pin the reboot to 20:00 UTC = 04:00 Beijing and never while someone is logged
# in. A drop-in sorted after 50unattended-upgrades wins; `cat >` is idempotent (same content
# on every run). Existing hosts get the same file by hand (deploy.md §8.9).
cat >/etc/apt/apt.conf.d/51-launchpad-upgrades <<'APT'
// Launchpad-CN (N13-obs13): reboot for unattended upgrades only in the maintenance window.
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "20:00";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
APT
chmod 0644 /etc/apt/apt.conf.d/51-launchpad-upgrades

IMDS_TOKEN="$(curl --fail --silent --show-error --retry 10 \
  --request PUT --header 'X-aws-ec2-metadata-token-ttl-seconds: 21600' \
  http://169.254.169.254/latest/api/token)"
INSTANCE_ID="$(curl --fail --silent --show-error --retry 10 \
  --header "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
  http://169.254.169.254/latest/meta-data/instance-id)"
# N11-obs14: the console's public origin, for LAUNCHPAD_PUBLIC_BASE_URL below —
# same IMDSv2 token, same public IPv4 the ConsoleUrl output shows (the template
# always associates one; see the NIC). IMDS answers 404 for public-ipv4 on an
# instance without one, which `--fail` + `set -e` would turn into a boot failure
# — fall back to the private IPv4 so a private-only variant still comes up.
PUBLIC_IP="$(curl --fail --silent --show-error --retry 10 \
  --header "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
  http://169.254.169.254/latest/meta-data/public-ipv4 \
  || curl --fail --silent --show-error --retry 10 \
  --header "X-aws-ec2-metadata-token: ${IMDS_TOKEN}" \
  http://169.254.169.254/latest/meta-data/local-ipv4)"
if [ "$LAUNCHPAD_BOOT_TLS" = "true" ]; then
  CONSOLE_SCHEME=https
else
  CONSOLE_SCHEME=http
fi

signal_stack() {
  aws cloudformation signal-resource --stack-name "$LAUNCHPAD_BOOT_STACK_NAME" \
    --logical-resource-id "$LAUNCHPAD_BOOT_SIGNAL_RESOURCE" --unique-id "$INSTANCE_ID" \
    --status "$1" --region "$LAUNCHPAD_BOOT_REGION"
}
trap 'signal_stack FAILURE || true' ERR

curl --fail --location --silent --show-error \
  https://deb.nodesource.com/setup_22.x --output /tmp/nodesource-setup.sh
bash /tmp/nodesource-setup.sh
apt-get install -y nodejs

curl --fail --location --silent --show-error \
  https://astral.sh/uv/install.sh --output /tmp/install-uv.sh
UV_INSTALL_DIR=/usr/local/bin sh /tmp/install-uv.sh
npm config set registry "$LAUNCHPAD_BOOT_NPM_REGISTRY"
npm install --global aws-cdk

# root:root 0700 — nothing but root (and systemd's own EnvironmentFile= read,
# which always runs as root/PID 1 regardless of a unit's User=) may ever open
# this file. The bootstrap subshell below reads it by having *root* source it
# and then handing the already-populated environment down via `runuser -m`
# (see below) — never by widening this file's access to the `ubuntu` group,
# which would let every other process running as `ubuntu` (npm/uv installs,
# cloned code) read the raw secrets for the file's entire lifetime.
# (The stub already created the directory for bootstrap.env; idempotent.)
install -d -m 0700 -o root -g root /etc/launchpad
BEDROCK_KEY="$(aws ssm get-parameter --name "$LAUNCHPAD_BOOT_BEDROCK_KEY_PARAMETER" \
  --with-decryption --query Parameter.Value --output text)"
CONSOLE_PASSWORD="$(aws ssm get-parameter --name "$LAUNCHPAD_BOOT_CONSOLE_PASSWORD_PARAMETER" \
  --with-decryption --query Parameter.Value --output text)"
# N-R6: the optional SiliconFlow key (OpenAI-compatible model provider) — same discipline
# as the Bedrock key; an empty parameter name leaves the variable empty and `make
# bootstrap` reports the provider as not configured.
SILICONFLOW_API_KEY=""
if [ -n "$LAUNCHPAD_BOOT_SILICONFLOW_KEY_PARAMETER" ]; then
  SILICONFLOW_API_KEY="$(aws ssm get-parameter --name "$LAUNCHPAD_BOOT_SILICONFLOW_KEY_PARAMETER" \
    --with-decryption --query Parameter.Value --output text)"
fi
umask 077
{
  printf 'AWS_BEARER_TOKEN_BEDROCK=%s\n' "$BEDROCK_KEY"
  printf 'LAUNCHPAD_GLOBAL_BEDROCK_API_KEY=%s\n' "$BEDROCK_KEY"
  printf 'LAUNCHPAD_SILICONFLOW_API_KEY=%s\n' "$SILICONFLOW_API_KEY"
  printf 'LAUNCHPAD_RUN_MODE=prod\n'
  printf 'LAUNCHPAD_REGION=%s\n' "$LAUNCHPAD_BOOT_REGION"
  printf 'AWS_REGION=%s\n' "$LAUNCHPAD_BOOT_REGION"
  printf 'AWS_DEFAULT_REGION=%s\n' "$LAUNCHPAD_BOOT_REGION"
  # N11: `env_name=<EnvName>` is passed to `cdk deploy` through
  # LAUNCHPAD_CDK_CONTEXT (scripts/bootstrap.py::cdk_context_args() turns it
  # into repeated `--context k=v` flags) so the CDK stacks this host deploys get
  # the same suffix as its own ops role; LAUNCHPAD_ENV_NAME is what the backend
  # and scripts/{bootstrap,teardown}.py read (Settings.env_name) to name/look up
  # launchpad-*-<EnvName> resources. Empty EnvName writes `env_name=` / an empty
  # value, which every layer treats as "no suffix" — the pre-N11 behaviour.
  # D17: no `memory_master_arn=` any more — the shared domain's FGAC master is
  # the stack-owned launchpad-memory-admin-<region> role this host assumes (Sid
  # MemoryAdminAssume in the template), so the same domain serves every
  # environment regardless of which one created it. Not a secret, but written to
  # /etc/launchpad/env so both this boot's `make bootstrap` and any later manual
  # one inherit it.
  # N11-r3-bug7: the build mirrors ride the same variable so the hub CodeBuild
  # project this host deploys (`cdk deploy launchpad-base`) gets the mirrors of
  # CodeBuildPipIndexUrl / CodeBuildPipFallbackIndexUrl — one parameter pair
  # governs the hub project, the spoke projects and the Skill Lab worker
  # buildspec (the LAUNCHPAD_CODEBUILD_* lines).
  printf 'LAUNCHPAD_CDK_CONTEXT=env_name=%s,codebuild_pip_index_url=%s,codebuild_pip_fallback_index_url=%s\n' "$LAUNCHPAD_BOOT_ENV_NAME" "$LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL" "$LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL"
  printf 'LAUNCHPAD_ENV_NAME=%s\n' "$LAUNCHPAD_BOOT_ENV_NAME"
  printf 'LAUNCHPAD_MEMORY_DOMAIN_MODE=%s\n' "$LAUNCHPAD_BOOT_MEMORY_DOMAIN_MODE"
  printf 'LAUNCHPAD_AUTH_PASSWORD_POLICY=%s\n' "$LAUNCHPAD_BOOT_AUTH_PASSWORD_POLICY"
  printf 'LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH=%s\n' "$LAUNCHPAD_BOOT_AUTH_PASSWORD_MIN_LENGTH"
  # The HOST mirror (tuna): this host's own uv sync / pip and the zip resolve.
  # CodeBuild never sees it (N11-r3-bug7, below).
  printf 'PIP_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_PIP_INDEX_URL"
  printf 'UV_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_PIP_INDEX_URL"
  printf 'UV_DEFAULT_INDEX=%s\n' "$LAUNCHPAD_BOOT_PIP_INDEX_URL"
  # N11-r3-bug7: the BUILD mirrors — Settings.codebuild_pip_index_url /
  # codebuild_pip_fallback_index_url: the spoke CodeBuild project env the
  # backend creates and the Skill Lab worker buildspec's build args.
  printf 'LAUNCHPAD_CODEBUILD_PIP_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL"
  printf 'LAUNCHPAD_CODEBUILD_PIP_FALLBACK_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL"
  # N11-obs25: an optional secondary index — pip's extra index, uv's UV_INDEX
  # (additional indexes), and the backend setting that forwards it to
  # CodeBuild / the zip resolve / the Skill Lab worker image.
  if [ -n "$LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL" ]; then
    printf 'PIP_EXTRA_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL"
    printf 'UV_INDEX=%s\n' "$LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL"
    printf 'LAUNCHPAD_PIP_EXTRA_INDEX_URL=%s\n' "$LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL"
  fi
  printf 'NPM_CONFIG_REGISTRY=%s\n' "$LAUNCHPAD_BOOT_NPM_REGISTRY"
  # N11-r5-bug4: uv/npm caches live UNDER the ubuntu-owned checkout (data/ is gitignored
  # and part of host-update.sh's ownership check), never in ~/.cache or ~/.npm — the
  # owner-run block below inherits these through `set -a` + `runuser -m`, the backend
  # service through EnvironmentFile=, and host-update.sh exports the same two values.
  printf 'UV_CACHE_DIR=%s\n' /home/ubuntu/launchpad/data/.uv-cache
  printf 'npm_config_cache=%s\n' /home/ubuntu/launchpad/data/.npm-cache
  printf 'LAUNCHPAD_AUTH_USERNAME=admin\n'
  printf 'LAUNCHPAD_AUTH_PASSWORD=%s\n' "$CONSOLE_PASSWORD"
  # N11-obs14: the backend derives the Identity 3LO (as-user OAuth) return URL
  # as {public_base_url}/auth/return; without this it stays
  # http://localhost:5173 and run_mode=prod warns at start-up. Scheme follows
  # TlsMode, port is ConsolePort, host is this instance's public IPv4 — i.e.
  # exactly the ConsoleUrl output. An operator fronting the console with a
  # hostname (DNS / reverse proxy) must override this line
  # (docs/cn/interfaces/deploy.md §3).
  printf 'LAUNCHPAD_PUBLIC_BASE_URL=%s://%s:%s\n' "$CONSOLE_SCHEME" "$PUBLIC_IP" "$LAUNCHPAD_BOOT_CONSOLE_PORT"
} > /etc/launchpad/env
chmod 0600 /etc/launchpad/env
chown root:root /etc/launchpad/env
unset BEDROCK_KEY SILICONFLOW_API_KEY CONSOLE_PASSWORD

# N9 follow-up: `umask 077` (set above for the secrets — every secret file was
# created at its own explicit, tighter mode regardless) must not leak into the
# rest of boot. nginx runs as www-data and serves
# /home/ubuntu/launchpad/frontend/dist, so every directory/file `npm run build`
# creates from here on needs to be at least world-readable+traversable, not the
# secrets' own 0600/0700 — reset to a normal 022 now that every secret write
# above is done. (The stub performed the clone itself under umask 022.)
umask 022
# Ubuntu 24.04's default /home/ubuntu is 0750 (group/other cannot even traverse
# it), which would 403 nginx regardless of how permissive the files underneath
# are. 0711 (execute-only, no listing) is the minimum traversal grant — it does
# not expose a directory listing of /home/ubuntu itself.
chmod 0711 /home/ubuntu
# N13-obs11: the demo-video library's self-hosted media directory — nginx serves it as
# /media/ (infra/ec2/nginx-site.sh). It lives OUTSIDE the checkout so neither a fast-forward
# nor host-update.sh's ownership check / rebuilds ever touch it; files are staged by hand
# (deploy.md §8.8) as ubuntu (0644 under umask 022) and read by www-data through the 0711
# home above. Idempotent: an existing directory keeps its contents.
install -d -m 0755 -o ubuntu -g ubuntu /home/ubuntu/launchpad-media

# Forward-compatibility: once the N2 model-credential stage lands,
# `cdk bootstrap`/`make bootstrap` may need AWS_BEARER_TOKEN_BEDROCK /
# LAUNCHPAD_GLOBAL_BEDROCK_API_KEY. Source the env file *here*, as root, then
# hand the resulting environment down to the `ubuntu` subshell via `runuser -m`
# (--preserve-environment) — never as `env VAR=value`/`-- VAR=value` on the
# command line, which `ps`/`/proc/<pid>/cmdline` would expose to every user on
# the host, and never by widening the env file's own access instead (see the
# `install -d`/`chown` comments above). `-m` means `runuser` does NOT reset
# HOME/PATH/etc. to the target user's defaults, so both are exported explicitly
# first. The whole thing runs in a `( ... )` subshell so HOME/PATH and every var
# sourced from the env file (not just the Bedrock ones) are scoped to this one
# call and gone the moment it returns — they do not leak into the rest of this
# script (systemd/nginx setup, the health-check loop, signal_stack).
# `set -Eeuo pipefail` is already active in the parent, so a non-zero exit from
# the subshell still aborts the script and fires the ERR trap exactly as a
# non-subshelled failure would.
# N12-bug10: everything that writes under the checkout (uv sync, npm install,
# cdk bootstrap, make bootstrap, npm run build) runs as `ubuntu` here, never as
# root — the backend unit runs as `ubuntu` and rewrites data/ and config/ on
# every repair/redeploy; scripts/host-update.sh keeps the same rule.
# N11-obs46: the build is stamped with the commit it was built from
# (frontend/dist/.launchpad-build, the sha + newline host-update.sh writes and
# reads with `tr -d '[:space:]'`), so the first host-update keeps dist/ instead
# of rebuilding it for "no .launchpad-build stamp".
(
  export HOME=/home/ubuntu
  export PATH=/usr/local/bin:/usr/bin:/usr/sbin:/bin:/sbin
  set -a
  # shellcheck source=/dev/null
  . /etc/launchpad/env
  set +a
  # shellcheck disable=SC2016  # the single-quoted script is expanded by the ubuntu bash, not here
  runuser -u ubuntu -m -- bash -c '
      set -Eeuo pipefail
      cd /home/ubuntu/launchpad
      (cd backend && uv sync)
      (cd frontend && npm install)
      (cd infra && uv sync)
      ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
      printf -v CDK_ENV "aws://%s/%s" "$ACCOUNT_ID" "$AWS_REGION"
      cdk bootstrap "$CDK_ENV"
      make bootstrap
      cd frontend && npm run build
      git -C /home/ubuntu/launchpad rev-parse HEAD > /home/ubuntu/launchpad/frontend/dist/.launchpad-build
    '
)

cat >/etc/systemd/system/launchpad-backend.service <<UNIT
[Unit]
After=network-online.target
[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/launchpad/backend
EnvironmentFile=/etc/launchpad/env
ExecStart=/usr/local/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT

if [ "$LAUNCHPAD_BOOT_TLS" = "true" ]; then
  # N12-obs60 (scan T-4): the self-signed cert is generated by infra/ec2/tls-cert.sh (next to this
  # file) with a SAN — DNS:launchpad.internal plus this host's IPv4 from IMDS. The EIP is associated
  # by a separate CloudFormation resource and the console NLB's address is never known at boot, so
  # an operator re-runs it with the deploy-time addresses and reloads nginx (deploy.md §11):
  #   sudo infra/ec2/tls-cert.sh --force --san IP:<eip>,IP:<nlb-ip> && sudo nginx -t && sudo systemctl reload nginx
  bash "$(dirname "${BASH_SOURCE[0]}")/tls-cert.sh" --out-dir /etc/nginx/launchpad-tls \
    --san "IP:${PUBLIC_IP}"
fi

# WB-003: the site is rendered by infra/ec2/nginx-site.sh (next to this file in the
# checkout the stub cloned) — the SAME function scripts/host-update.sh re-renders on every
# update and installs + reloads only when it differs, so a rule change there reaches running
# hosts without a rebuild. Never inline nginx config here again.
# shellcheck source=/dev/null
. "$(dirname "${BASH_SOURCE[0]}")/nginx-site.sh"
render_nginx_site "$LAUNCHPAD_BOOT_TLS" "$LAUNCHPAD_BOOT_CONSOLE_PORT" \
  /home/ubuntu/launchpad/frontend/dist 8000 >/etc/nginx/sites-available/launchpad
rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/launchpad /etc/nginx/sites-enabled/launchpad
nginx -t
systemctl daemon-reload
systemctl enable --now launchpad-backend
systemctl restart nginx

UP=no
for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/api/health >/dev/null 2>&1; then UP=yes; break; fi
  sleep 5
done
if [ "$UP" != yes ]; then
  journalctl -u launchpad-backend --no-pager -n 60 >&2 || true
  trap - ERR
  signal_stack FAILURE
  exit 1
fi

trap - ERR
signal_stack SUCCESS
