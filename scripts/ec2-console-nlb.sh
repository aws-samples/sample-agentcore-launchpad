#!/usr/bin/env bash
# Console NLB helper (N12-obs52) - thin wrappers around the AWS CLI for the OPTIONAL
# `infra/ec2/launchpad-console-nlb.yaml` stack: a Network Load Balancer with a STATIC
# Elastic IP in front of ONE console host of `launchpad-cn-ec2.yaml`, TCP passthrough on
# 8443 (the host's self-signed TLS stays - no certificate import). User decision
# 2026-10-16: the port is open to any source IP by default (AccessMode=OpenToInternet);
# `--cidr` switches to the allowlist mode. docs/cn/interfaces/deploy.md section 9 is the
# runbook; this script only saves the parameter plumbing. NOT part of `make verify`
# (needs the operator's AWS CLI profile); every AWS-touching subcommand is run by
# deploy-ops (spec C8), never by a sub-session. No secrets are read or written.
#
#   scripts/ec2-console-nlb.sh discover [--ec2-stack <name>]
#   scripts/ec2-console-nlb.sh create|update [--ec2-stack <name>] [--stack-name <name>] \
#       [--listener-port 8443] [--cidr <a.b.c.d/nn> ... up to 4  => AccessMode=AllowedCidrs] \
#       [--no-sg] [--no-execute] [--dry-run]
#       [--http-lab --http-port 8088 | --no-http-lab]
# HTTP lab adds a primary HTTP port and retains the original HTTPS listener.
# Prepare the host with configure_http_lab_host.sh in its maintenance window first.
#   scripts/ec2-console-nlb.sh status   [--stack-name <name>]
#   scripts/ec2-console-nlb.sh delete   [--stack-name <name>]
#
# Environment: AWS_REGION (default cn-northwest-1), AWS_PROFILE (default cn-northwest-1).
# `create` and `update` are the same `aws cloudformation deploy` (idempotent). `--dry-run`
# resolves the ids (read-only describe calls) and prints the deploy command without
# running it; `--no-execute` creates the change set only (review, then execute by hand).
# `--no-sg` is the escape hatch for a region that rejects security groups on an NLB
# (NlbSecurityGroupMode=disabled: the host admits the subnet CIDR instead). `delete` keeps
# the Elastic IP (DeletionPolicy Retain) and prints the release-address command.
set -Eeuo pipefail

REGION="${AWS_REGION:-cn-northwest-1}"
PROFILE="${AWS_PROFILE:-cn-northwest-1}"
AWSCLI=(aws --region "$REGION" --profile "$PROFILE")
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEMPLATE="$ROOT/infra/ec2/launchpad-console-nlb.yaml"

EC2_STACK="launchpad-cn-ec2"
STACK_NAME="launchpad-cn-console-nlb"
LISTENER_PORT=8443
CIDRS=()
NO_SG=false
NO_EXECUTE=false
DRY_RUN=false
HTTP_LAB=
HTTP_PORT=8088

usage() { sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
die() { echo "ec2-console-nlb: $*" >&2; exit 1; }

# stack_resource <stack> <logical-id> -> physical id
stack_resource() {
  "${AWSCLI[@]}" cloudformation describe-stack-resource --stack-name "$1" \
    --logical-resource-id "$2" --query 'StackResourceDetail.PhysicalResourceId' --output text
}
# stack_param <stack> <parameter-key> -> value
stack_param() {
  "${AWSCLI[@]}" cloudformation describe-stacks --stack-name "$1" \
    --query "Stacks[0].Parameters[?ParameterKey=='$2'].ParameterValue" --output text
}

cmd_discover() {
  echo "ec2 stack:               $EC2_STACK ($REGION, profile $PROFILE)"
  echo "VpcId:                   $(stack_resource "$EC2_STACK" Vpc)"
  echo "PublicSubnetId:          $(stack_resource "$EC2_STACK" PublicSubnet)"
  echo "InstanceId:              $(stack_resource "$EC2_STACK" Instance)"
  echo "InstanceSecurityGroupId: $(stack_resource "$EC2_STACK" ConsoleSecurityGroup)"
  echo "InstancePort:            $(stack_param "$EC2_STACK" ConsolePort)"
  echo "TlsMode:                 $(stack_param "$EC2_STACK" TlsMode)   (selfsigned expected: the NLB passes TLS through)"
  echo "host AllowedCidr:        $(stack_param "$EC2_STACK" AllowedCidr)   (stays; the direct Elastic-IP path is untouched)"
}

cmd_deploy() {
  if [ "$HTTP_LAB" = true ]; then
    [[ "$HTTP_PORT" =~ ^[0-9]+$ ]] &&
      (( HTTP_PORT >= 1024 && HTTP_PORT <= 65535 && HTTP_PORT != 8080 )) ||
      die "HTTP port must be 1024-65535 and not 8080"
    [[ "$HTTP_PORT" != "$LISTENER_PORT" ]] || die "HTTP and HTTPS listener ports must differ"
    [[ "$NO_SG" = false && "${#CIDRS[@]}" = 0 ]] ||
      die "HTTP lab requires NLB security groups and OpenToInternet"
  fi
  [ "${#CIDRS[@]}" -le 4 ] || die "create/update: at most four --cidr values"
  for c in "${CIDRS[@]}"; do
    case "$c" in */0) die "refusing a /0 in the allowlist ($c): omit --cidr to open the port deliberately (AccessMode=OpenToInternet)" ;; esac
  done
  case "$LISTENER_PORT" in 80|443|8080) die "listener port $LISTENER_PORT is ICP-gated in the China partition (N11-bug12); use 8443" ;; esac
  if [ "$NO_SG" = true ] && [ "${#CIDRS[@]}" -gt 0 ]; then
    die "--no-sg cannot enforce an allowlist (no security group on the NLB); drop --cidr or --no-sg"
  fi
  local vpc subnet instance sg port tls
  vpc="$(stack_resource "$EC2_STACK" Vpc)"
  subnet="$(stack_resource "$EC2_STACK" PublicSubnet)"
  instance="$(stack_resource "$EC2_STACK" Instance)"
  sg="$(stack_resource "$EC2_STACK" ConsoleSecurityGroup)"
  port="$(stack_param "$EC2_STACK" ConsolePort)"
  tls="$(stack_param "$EC2_STACK" TlsMode)"
  if [ "$tls" != "selfsigned" ]; then
    echo "note: $EC2_STACK has TlsMode=$tls - the health check is HTTPS; a TlsMode=none host will stay unhealthy" >&2
  fi
  local params=(
    "VpcId=$vpc" "PublicSubnetId=$subnet" "InstanceId=$instance"
    "InstanceSecurityGroupId=$sg" "InstancePort=$port" "ListenerPort=$LISTENER_PORT"
  )
  if [ -n "$HTTP_LAB" ]; then params+=("ConsoleHttpLab=$HTTP_LAB" "HttpPort=$HTTP_PORT"); fi
  if [ "${#CIDRS[@]}" -gt 0 ]; then
    local joined; joined="$(IFS=,; echo "${CIDRS[*]}")"
    params+=("AccessMode=AllowedCidrs" "AllowedCidrs=$joined")
  else
    params+=("AccessMode=OpenToInternet")
  fi
  if [ "$NO_SG" = true ]; then params+=("NlbSecurityGroupMode=disabled"); fi
  local cmd=(
    "${AWSCLI[@]}" cloudformation deploy
    --template-file "$TEMPLATE" --stack-name "$STACK_NAME"
    --no-fail-on-empty-changeset
    --parameter-overrides "${params[@]}"
  )
  if [ "$NO_EXECUTE" = true ]; then cmd+=(--no-execute-changeset); fi
  printf '%q ' "${cmd[@]}"; echo
  if [ "$DRY_RUN" = true ]; then echo "(dry run - not executed)"; return 0; fi
  "${cmd[@]}"
  if [ "$NO_EXECUTE" = false ]; then cmd_status; fi
}

cmd_status() {
  echo "stack: $STACK_NAME ($REGION)"
  "${AWSCLI[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
    --query 'Stacks[0].[StackStatus, Outputs[].[OutputKey, OutputValue]]' --output text
  local tg
  tg="$("${AWSCLI[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='TargetGroupArn'].OutputValue" --output text)"
  if [ -n "$tg" ] && [ "$tg" != "None" ]; then
    echo "target health:"
    "${AWSCLI[@]}" elbv2 describe-target-health --target-group-arn "$tg" \
      --query 'TargetHealthDescriptions[].[Target.Id, Target.Port, TargetHealth.State, TargetHealth.Reason]' --output text
  fi
}

cmd_delete() {
  local alloc
  alloc="$("${AWSCLI[@]}" cloudformation describe-stacks --stack-name "$STACK_NAME" \
      --query "Stacks[0].Outputs[?OutputKey=='EipAllocationId'].OutputValue" --output text 2>/dev/null || true)"
  echo "deleting $STACK_NAME (the NLB, its security group and the NLB rule on the host's security group). The host stack is untouched."
  echo "the fixed IP SURVIVES stack deletion (DeletionPolicy Retain); release it manually when truly done:"
  echo "  aws ec2 release-address --allocation-id ${alloc:-<EipAllocationId>} --region $REGION --profile $PROFILE"
  "${AWSCLI[@]}" cloudformation delete-stack --stack-name "$STACK_NAME"
  "${AWSCLI[@]}" cloudformation wait stack-delete-complete --stack-name "$STACK_NAME"
  echo "deleted (Elastic IP ${alloc:-?} retained)"
}

[ $# -ge 1 ] || { usage; exit 1; }
SUBCMD="$1"; shift
while [ $# -gt 0 ]; do
  case "$1" in
    --ec2-stack) EC2_STACK="$2"; shift 2 ;;
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --listener-port) LISTENER_PORT="$2"; shift 2 ;;
    --http-lab) HTTP_LAB=true; shift ;;
    --no-http-lab) HTTP_LAB=false; shift ;;
    --http-port) HTTP_PORT="$2"; shift 2 ;;
    --cidr) CIDRS+=("$2"); shift 2 ;;
    --no-sg) NO_SG=true; shift ;;
    --no-execute) NO_EXECUTE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1 (see --help)" ;;
  esac
done

case "$SUBCMD" in
  discover) cmd_discover ;;
  create|update) cmd_deploy ;;
  status) cmd_status ;;
  delete) cmd_delete ;;
  -h|--help|help) usage ;;
  *) die "unknown subcommand: $SUBCMD (see --help)" ;;
esac
