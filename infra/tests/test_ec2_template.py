"""Static checks on the CN control-plane EC2 CloudFormation template and the host
bootstrap it hands off to.

Not CDK — this is plain CloudFormation YAML, so these tests use a small
multi-constructor YAML loader (the standard cfn-flip/cfn-lint trick: every `!Tag`
becomes `{"Fn::Tag": ...}` or `{"Ref": ...}`) instead of `aws_cdk.assertions`.

N11-r5-bug1: EC2 caps user data at 16384 bytes, so the first boot is split in two —
the template's UserData is a small STUB (`_user_data_text()`: awscli, bootstrap.env,
deploy key, sparse clone, `exec`) and `infra/ec2/bootstrap-host.sh` is the BODY
(`_script_text()`), run from the fresh checkout and fed by /etc/launchpad/bootstrap.env.
Assertions about the stub read the Fn::Sub string; assertions about the body read the
script; the two are tied together by the bootstrap.env key-parity test below.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "ec2" / "launchpad-cn-ec2.yaml"
SCRIPT_PATH = Path(__file__).resolve().parents[1] / "ec2" / "bootstrap-host.sh"
BOOTSTRAP_ENV = "/etc/launchpad/bootstrap.env"
BODY_ON_HOST = "/home/ubuntu/launchpad/infra/ec2/bootstrap-host.sh"
EC2_USER_DATA_LIMIT = 16384
USER_DATA_MARGIN = 512
# every substitution the inline UserData used before the split — the stub must still
# consume each one (directly, or by writing it into bootstrap.env for the body)
EXPECTED_STUB_TOKENS = {
    "AWS::Region", "AWS::StackName", "UseTlsCookieFlag",
    "PipIndexUrl", "PipExtraIndexUrl", "CodeBuildPipIndexUrl", "CodeBuildPipFallbackIndexUrl",
    "NpmRegistry", "ConsolePort", "EnvName", "MemoryDomainMode",
    "AuthPasswordPolicy", "AuthPasswordMinLength",
    "GlobalBedrockApiKeyParameter", "SiliconFlowApiKeyParameter", "ConsolePasswordParameter",
    "GitDeployKeyParameter",
    "GitRepoUrl", "GitBranch", "GitCloneTimeout",
}


def _cfn_multi_constructor(loader: yaml.SafeLoader, tag_suffix: str, node):
    tag = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node)
    else:
        value = loader.construct_mapping(node)
    return {tag: value}


class _CfnLoader(yaml.SafeLoader):
    pass


_CfnLoader.add_multi_constructor("!", _cfn_multi_constructor)


def _load() -> dict:
    return yaml.load(TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CfnLoader)


def _resources_of_type(template: dict, type_: str) -> list[dict]:
    return [r for r in template["Resources"].values() if r["Type"] == type_]


def _user_data_sub() -> tuple[str, dict]:
    """The Instance's `UserData: Fn::Base64: Fn::Sub: [text, {Var: ...}]` pair."""
    template = _load()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    text, variables = instance["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    return text, variables


def _user_data_text() -> str:
    """The stub: the bash the instance actually boots with, before substitution."""
    return _user_data_sub()[0]


def _script_text() -> str:
    """The body: infra/ec2/bootstrap-host.sh, exec'd by the stub from the checkout."""
    return SCRIPT_PATH.read_text(encoding="utf-8")


def _both_texts() -> str:
    return _user_data_text() + "\n" + _script_text()


def _sub_tokens(text: str) -> set[str]:
    return set(re.findall(r"\$\{([^}!][^}]*)\}", text))


def _worst_case_value(name: str, template: dict, variables: dict) -> str:
    """The longest value a token can legally take: MaxLength / `{0,N}` of the
    AllowedPattern / the Number's MaxValue, else 128 characters (unconstrained)."""
    if name == "AWS::Region":
        return "x" * 128  # region names are short, but nothing in the template bounds them
    if name == "AWS::StackName":
        return "x" * 128  # CloudFormation's own stack-name limit
    if name in variables:
        return "false"  # UseTlsCookieFlag: "true" | "false"
    param = template["Parameters"][name]
    if param.get("Type") == "Number":
        return str(param.get("MaxValue", 10**8))
    if "MaxLength" in param:
        return "x" * int(param["MaxLength"])
    bounded = re.search(r"\{0,(\d+)\}\$$", param.get("AllowedPattern", ""))
    if bounded:
        return "x" * int(bounded.group(1))
    return "x" * 128


def _render_user_data(worst_case: bool) -> str:
    """Evaluate the Fn::Sub by hand: every `${Token}` -> its worst-case (or default)
    value, `${!Literal}` -> `${Literal}` (the Fn::Sub escape)."""
    template = _load()
    text, variables = _user_data_sub()
    for token in _sub_tokens(text):
        if worst_case:
            value = _worst_case_value(token, template, variables)
        elif token == "AWS::Region":
            value = "cn-northwest-1"
        elif token == "AWS::StackName":
            value = "launchpad-cn-ec2"
        elif token in variables:
            value = "false"
        else:
            value = str(template["Parameters"][token].get("Default", "x" * 32))
        text = text.replace(f"${{{token}}}", value)
    return text.replace("${!", "${")


def _bootstrap_env_block(stub: str) -> list[str]:
    """The lines between `{` and `} > /etc/launchpad/bootstrap.env` in the stub."""
    lines = stub.splitlines()
    close_idx = next(i for i, ln in enumerate(lines) if ln.strip() == f"}} > {BOOTSTRAP_ENV}")
    open_idx = max(i for i, ln in enumerate(lines) if ln.strip() == "{" and i < close_idx)
    return lines[open_idx + 1 : close_idx]


def _bootstrap_env_keys_written() -> set[str]:
    keys = set()
    for line in _bootstrap_env_block(_user_data_text()):
        m = re.match(r"\s*printf '(LAUNCHPAD_BOOT_[A-Z0-9_]+)=", line)
        assert m, f"unexpected line in the bootstrap.env block: {line!r}"
        keys.add(m.group(1))
    return keys


def _bootstrap_env_keys_declared() -> set[str]:
    """The body's contract block: one `: "${LAUNCHPAD_BOOT_X:?}"` (or `?`) per key."""
    return set(re.findall(r'^: "\$\{(LAUNCHPAD_BOOT_[A-Z0-9_]+):?[?=]', _script_text(), re.M))


def test_template_parses():
    template = _load()
    assert template["Resources"]
    assert set(template["Parameters"]) >= {
        "AllowedCidr", "InstanceType", "GitRepoUrl", "GitBranch",
        "PipIndexUrl", "NpmRegistry", "GlobalBedrockApiKeyParameter",
        "ConsolePasswordParameter", "TlsMode", "GitDeployKeyParameter", "EnvName",
        "AmiId",
    }


def test_allowed_cidr_has_no_default():
    template = _load()
    assert "Default" not in template["Parameters"]["AllowedCidr"]


def test_allowed_cidr_pattern_rejects_slash_zero_and_overlong_prefixes():
    # F4 (Palisade-adjacent): the pre-fix pattern's `[0-9]{1,2}` prefix group
    # accepted `/0` (whole IPv4 space) for any IP that wasn't literally
    # 0.0.0.0, and accepted nonsense like `/99`. The prefix group must now be
    # restricted to 1-32.
    template = _load()
    pattern = re.compile(template["Parameters"]["AllowedCidr"]["AllowedPattern"])
    assert not pattern.match("1.2.3.4/0")
    assert not pattern.match("0.0.0.0/0")
    assert not pattern.match("10.20.30.40/99")
    assert pattern.match("10.0.0.0/8")
    assert pattern.match("203.0.113.7/32")


def test_ssm_parameter_names_must_be_flat_no_leading_slash():
    # F2: the IAM statement below builds its resource ARN as the literal
    # string "parameter/" immediately followed by the parameter's name. Per
    # AWS's own SSMParameterReadPolicy vs. SSMParameterWithSlashPrefixReadPolicy
    # SAM templates, that form is only correct for a name with no leading
    # slash — a hierarchical name would double the slash and not match the
    # real parameter ARN. Both SSM-name parameters must reject one.
    template = _load()
    for name in ("GlobalBedrockApiKeyParameter", "ConsolePasswordParameter",
                 "SiliconFlowApiKeyParameter"):
        pattern = re.compile(template["Parameters"][name]["AllowedPattern"])
        assert not pattern.match("/launchpad/leading-slash-name"), name
        assert pattern.match("launchpad-global-bedrock-api-key"), name
        assert pattern.match("launchpad-siliconflow-api-key"), name


def test_no_cloudfront_resource():
    template = _load()
    types = {r["Type"] for r in template["Resources"].values()}
    assert not any(t.startswith("AWS::CloudFront::") for t in types)


def test_security_group_ingress_only_from_allowed_cidr():
    template = _load()
    sg = template["Resources"]["ConsoleSecurityGroup"]
    ingress = sg["Properties"]["SecurityGroupIngress"]
    assert len(ingress) >= 1
    for rule in ingress:
        assert rule["CidrIp"] == {"Ref": "AllowedCidr"}
        assert rule["FromPort"] == {"Ref": "ConsolePort"}
        assert rule["ToPort"] == {"Ref": "ConsolePort"}


POLICY_NETWORK = (
    "PolicyPrivateSubnetA", "PolicyPrivateSubnetB", "PolicyPrivateRouteTable",
    "PolicyPrivateSubnetARouteTableAssociation", "PolicyPrivateSubnetBRouteTableAssociation",
    "PolicyS3GatewayEndpoint", "PolicyInterceptorSecurityGroup",
    "PolicyInterceptorToEngineEgress", "PolicyEngineFromInterceptorIngress",
)


def test_policy_interceptor_network_is_opt_in_and_private():
    """Stream B4 (design §13.6): everything behind PolicyInterceptorNetwork (default
    "false", so an update with the defaults adds nothing); private subnets with no route
    but local + the S3 gateway endpoint; no NAT, no interface endpoint; the engine ports
    open to the interceptor security group only."""
    template = _load()
    res = template["Resources"]
    assert template["Parameters"]["PolicyInterceptorNetwork"]["Default"] == "false"
    for name in POLICY_NETWORK:
        assert res[name].get("Condition") == "UsePolicyInterceptorNetwork", name
    for name in ("PolicySubnetIds", "PolicyInterceptorSecurityGroupId"):
        assert template["Outputs"][name]["Condition"] == "UsePolicyInterceptorNetwork"
    for name in ("PolicyPrivateSubnetA", "PolicyPrivateSubnetB"):
        assert res[name]["Properties"]["MapPublicIpOnLaunch"] is False
    routes = [r for r in res.values() if r["Type"] == "AWS::EC2::Route"]
    assert all(r["Properties"]["RouteTableId"] != {"Ref": "PolicyPrivateRouteTable"}
               for r in routes)
    assert not _resources_of_type(template, "AWS::EC2::NatGateway")
    [endpoint] = _resources_of_type(template, "AWS::EC2::VPCEndpoint")
    assert endpoint["Properties"]["VpcEndpointType"] == "Gateway"
    [statement] = endpoint["Properties"]["PolicyDocument"]["Statement"]
    assert statement["Action"] == "s3:GetObject"
    assert "/policy/gateway/*" in json.dumps(statement["Resource"])
    sg = res["PolicyInterceptorSecurityGroup"]["Properties"]
    assert "SecurityGroupIngress" not in sg
    ingress = res["PolicyEngineFromInterceptorIngress"]["Properties"]
    assert ingress["SourceSecurityGroupId"] == {"Ref": "PolicyInterceptorSecurityGroup"}
    assert (ingress["FromPort"], ingress["ToPort"]) == (7443, 7444)
    assert "CidrIp" not in ingress


def test_policy_principal_key_is_opt_in_retained_and_signs_for_the_hub_role_only():
    """Stream A1: the principal-token signing key behind PolicyPrincipalSigningKey (default
    "false" — an update with the defaults adds nothing); P-256 SIGN_VERIFY, retained on
    delete / replacement, 30-day window; only the ops role may kms:Sign (the account root
    administers the key without Sign); the alias carries the EnvName suffix."""
    template = _load()
    res = template["Resources"]
    assert template["Parameters"]["PolicyPrincipalSigningKey"]["Default"] == "false"
    key, alias = res["PolicyPrincipalKey"], res["PolicyPrincipalKeyAlias"]
    for item in (key, alias, template["Outputs"]["PolicyPrincipalKeyArn"]):
        assert item["Condition"] == "UsePolicyPrincipalKey"
    assert key["DeletionPolicy"] == key["UpdateReplacePolicy"] == "Retain"
    props = key["Properties"]
    assert (props["KeySpec"], props["KeyUsage"], props["PendingWindowInDays"]) == (
        "ECC_NIST_P256", "SIGN_VERIFY", 30)
    statements = {s["Sid"]: s for s in props["KeyPolicy"]["Statement"]}
    assert set(statements) == {"AccountAdministersTheKey", "HubSignsPrincipalTokens"}
    signers = [s for s in statements.values()
               if any(a in ("kms:*", "kms:Sign", "kms:Sign*") for a in s["Action"])]
    assert signers == [statements["HubSignsPrincipalTokens"]]
    admin = statements["AccountAdministersTheKey"]["Action"]
    assert not [a for a in admin if "*" in a]  # explicit actions only (re-review nit c)
    assert "kms:CreateGrant" not in admin and "kms:Sign" not in admin
    assert "kms:PutKeyPolicy" in admin  # the key stays manageable
    hub = statements["HubSignsPrincipalTokens"]
    assert hub["Principal"] == {"AWS": {"Fn::GetAtt": "InstanceRole.Arn"}}
    assert sorted(hub["Action"]) == ["kms:DescribeKey", "kms:GetPublicKey", "kms:Sign"]
    assert all(s["Effect"] == "Allow" for s in statements.values())
    assert alias["Properties"]["TargetKeyId"] == {"Ref": "PolicyPrincipalKey"}
    assert "alias/launchpad-principal" in json.dumps(alias["Properties"]["AliasName"])
    assert template["Outputs"]["PolicyPrincipalKeyArn"]["Value"] == {
        "Fn::GetAtt": "PolicyPrincipalKey.Arn"}


def test_policy_interceptor_lambda_grant_is_name_and_region_scoped():
    statements = {
        s["Sid"]: s for s in _load()["Resources"]["InstanceRole"]["Properties"]["Policies"][0][
            "PolicyDocument"]["Statement"]}
    grant = statements["LambdaForPolicyInterceptor"]
    assert "lambda:InvokeFunction" not in grant["Action"]
    assert "lambda:DeleteFunction" not in grant["Action"]
    assert "function:launchpad-policy-interceptor*" in json.dumps(grant["Resource"])
    assert "${AWS::Region}" in json.dumps(grant["Resource"])


def test_instance_has_an_egress_path_to_the_internet():
    # UserData needs outbound internet (apt/npm/pip mirrors, GitHub, SSM,
    # cfn-signal) to ever complete. This template has no NAT gateway (cost), so
    # the instance NIC must associate a public IP for outbound reachability —
    # or a future revision adds a NAT gateway / VPC endpoints instead. Inbound
    # exposure stays gated by the security group regardless (see the ingress
    # test above), so a public IP here does not by itself violate C6.
    template = _load()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    [nic] = instance["Properties"]["NetworkInterfaces"]
    has_public_ip = nic.get("AssociatePublicIpAddress") is True
    has_nat = bool(_resources_of_type(template, "AWS::EC2::NatGateway"))
    has_endpoints = bool(_resources_of_type(template, "AWS::EC2::VPCEndpoint"))
    assert has_public_ip or has_nat or has_endpoints


def test_no_hardcoded_arn_or_amazonaws_com_literal():
    # Mirrors scripts/cn_lint.sh's own patterns so this test matches its name:
    # a bare `arn:aws:` literal, or `.amazonaws.com` not immediately followed
    # by another `.` (i.e. not part of a longer, partition-tolerant hostname).
    amazonaws_com = re.compile(r"\.amazonaws\.com([^.]|$)")
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "cn-lint: allow" in line:
            continue
        assert "arn:aws:" not in line, f"literal partition ARN: {line!r}"
        assert not amazonaws_com.search(line), f"literal amazonaws.com hostname: {line!r}"


def test_user_data_writes_the_environment_file():
    # the body writes /etc/launchpad/env (secrets) under umask 077 and resets to 022 after
    text = _script_text()
    assert "/etc/launchpad/env" in text
    assert "EnvironmentFile=/etc/launchpad/env" in text
    assert "chmod 0600 /etc/launchpad/env" in text or "chmod 600 /etc/launchpad/env" in text
    lines = [ln.strip() for ln in text.splitlines()]
    env_close = lines.index("} > /etc/launchpad/env")
    assert lines.index("umask 077") < env_close < lines.index("umask 022")


def test_environment_file_and_its_directory_stay_root_owned():
    # The Bedrock key must stay readable only by root (and by systemd's own
    # EnvironmentFile= read, which always happens as PID 1/root regardless of
    # a unit's User=) — never widened to the `ubuntu` group/user, which would
    # let every other process running as `ubuntu` (npm/uv installs, cloned
    # code) read the raw key for the file's entire lifetime.
    text = _both_texts()
    install_lines = [
        line for line in text.splitlines() if "install -d" in line and "/etc/launchpad" in line
    ]
    assert len(install_lines) == 2, "one `install -d … /etc/launchpad` in the stub, one in the body"
    for line in install_lines:
        assert "-o root" in line
        assert "-g root" in line
        assert "ubuntu" not in line
    assert "chown root:root /etc/launchpad/env" in text
    assert "chown ubuntu" not in text
    # the stub's bootstrap.env (parameter values, no secrets) is root 0600 as well
    stub = _user_data_text()
    assert f"chmod 0600 {BOOTSTRAP_ENV}" in stub
    lines = [ln.strip() for ln in stub.splitlines()]
    assert lines.index("umask 077") < lines.index(f"}} > {BOOTSTRAP_ENV}")


def test_bedrock_key_is_never_passed_as_a_command_line_argument():
    # The key may only ever be: read from SSM into $BEDROCK_KEY, printf'd into
    # the env file, or unset — never appear as an `env VAR=value` or `--
    # VAR=value`-style command-line argument (visible to any user who can
    # list processes via `ps`/`/proc/<pid>/cmdline`), and never read back out
    # via a bare `env`/`runuser -- env` invocation either.
    text = _both_texts()
    secret_vars = (
        "AWS_BEARER_TOKEN_BEDROCK",
        "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY",
        "BEDROCK_KEY",
        "LAUNCHPAD_SILICONFLOW_API_KEY",  # N-R6: the optional provider key, same rules
        "SILICONFLOW_API_KEY",
        "LAUNCHPAD_AUTH_PASSWORD",
        "CONSOLE_PASSWORD",
        "DEPLOY_KEY",
    )
    # `: "${LAUNCHPAD_BOOT_*_PARAMETER:?}"` is the body's contract check on the SSM
    # parameter *names* (a bash builtin no-op; the names are not secrets).
    allowed_prefixes = ("printf ", "unset ", "#", ': "${LAUNCHPAD_BOOT_') + tuple(
        f"{v}=" for v in secret_vars
    )
    for line in text.splitlines():
        if not any(v in line for v in secret_vars):
            continue
        stripped = line.strip()
        assert stripped.startswith(allowed_prefixes), (
            f"secret variable referenced outside an allowed context: {line!r}"
        )
    # And no single line both invokes `env ...` (e.g. `runuser -- env ...`)
    # and mentions BEDROCK — i.e. no `env`-prefixed command line ever carries
    # a secret-looking assignment.
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        if re.search(r"\benv\b", line):
            assert "BEDROCK" not in line, f"env invocation mentions BEDROCK: {line!r}"


def test_bedrock_env_sourcing_is_scoped_to_a_subshell():
    # Round-3 fix: HOME/PATH and every var sourced from /etc/launchpad/env
    # (not just the two Bedrock ones) must not leak into the rest of
    # the bootstrap body (systemd/nginx setup, the health-check loop, signal_stack).
    # The `export HOME=...` through `runuser -u ubuntu -m -- bash -c '...'`
    # block must run inside a `( ... )` subshell, which scopes every export
    # and the `set -a` sourcing to just that one call.
    text = _script_text()
    lines = text.splitlines()
    source_idx = next(i for i, line in enumerate(lines) if line.strip() == ". /etc/launchpad/env")
    runuser_idx = next(i for i, line in enumerate(lines) if "runuser -u ubuntu -m --" in line)
    backend_unit_idx = next(
        i for i, line in enumerate(lines) if "launchpad-backend.service" in line
    )
    assert source_idx < runuser_idx < backend_unit_idx

    open_paren_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "(" and i < source_idx
    )
    close_paren_idx = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == ")" and runuser_idx < i < backend_unit_idx
    )
    assert open_paren_idx < source_idx
    assert runuser_idx < close_paren_idx < backend_unit_idx


def test_instance_role_has_no_administrator_access_by_default():
    # N17-obs112: AdministratorAccess exists in the template only behind the
    # GrantHubAdmin condition, whose parameter defaults to "false".
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    managed = role["Properties"].get("ManagedPolicyArns", [])
    admin = [m for m in managed if "AdministratorAccess" in str(m)]
    assert len(admin) == 1
    [gate] = admin
    assert list(gate) == ["Fn::If"]
    condition, when_true, when_false = gate["Fn::If"]
    assert condition == "GrantHubAdmin"
    assert "AdministratorAccess" in str(when_true)
    assert when_false == {"Ref": "AWS::NoValue"}
    assert template["Conditions"]["GrantHubAdmin"] == {
        "Fn::Equals": [{"Ref": "HubAdminAccess"}, "true"]
    }
    param = template["Parameters"]["HubAdminAccess"]
    assert param["Default"] == "false"
    assert param["AllowedValues"] == ["true", "false"]
    unconditional = [m for m in managed if "AdministratorAccess" not in str(m)]
    assert not any("AdministratorAccess" in str(m) for m in unconditional)


def test_ami_resolves_via_the_public_canonical_ssm_parameter():
    # N11-obs48b: the lookup is now the UseLatestAmi branch of ImageId (create-time
    # only); the pinned branch is `!Ref AmiId` — see the obs48b section below.
    template = _load()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    image_id = _latest_ami_branch(instance["Properties"]["ImageId"])
    assert image_id.startswith("{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/")
    assert "canonical/ubuntu/server/24.04" in image_id
    assert "arm64" in image_id
    assert image_id.endswith("/ami-id}}")


def test_outputs_include_private_ip_console_url_and_ssm_command():
    template = _load()
    assert {"PrivateIp", "ConsoleUrl", "SessionManagerCommand"} <= set(template["Outputs"])


def test_console_password_is_required_and_read_from_ssm_not_a_literal():
    # F1 (Critical): UserData sets LAUNCHPAD_RUN_MODE=prod, and
    # app.main._assert_production_is_authenticated refuses to build the app
    # at all when run_mode=="prod" and no password is configured — a crash
    # loop, not an open console. The console password must be read from SSM
    # at boot (same discipline as the Bedrock key), never baked into the
    # template as a literal or NoEcho CFN parameter (Fn::Sub still bakes a
    # NoEcho value into the UserData text, which `describe-instance-
    # attribute --attribute userData` can read back).
    template = _load()
    assert "Default" not in template["Parameters"]["ConsolePasswordParameter"]
    assert "NoEcho" not in template["Parameters"]["ConsolePasswordParameter"]
    # the stub hands the body the SSM *name*; the body reads the value with the role
    stub = _user_data_text()
    assert (
        "printf 'LAUNCHPAD_BOOT_CONSOLE_PASSWORD_PARAMETER=%q\\n' \"${ConsolePasswordParameter}\""
    ) in stub
    text = _script_text()
    assert (
        'aws ssm get-parameter --name "$LAUNCHPAD_BOOT_CONSOLE_PASSWORD_PARAMETER" \\' in text
    )
    assert "printf 'LAUNCHPAD_AUTH_USERNAME=admin\\n'" in text
    assert 'printf \'LAUNCHPAD_AUTH_PASSWORD=%s\\n\' "$CONSOLE_PASSWORD"' in text


def test_iam_statement_covers_both_secret_parameters():
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [ssm_statement] = [s for s in statements if s["Action"] == "ssm:GetParameter"]
    resources = ssm_statement["Resource"]
    joined = str(resources)
    assert "GlobalBedrockApiKeyParameter" in joined
    assert "ConsolePasswordParameter" in joined


def test_env_file_sets_uv_default_index_alongside_uv_index_url():
    # F5: uv 0.9.10 deprecates UV_INDEX_URL in favor of UV_DEFAULT_INDEX —
    # base_stack.py and workspace_bootstrap.py both already emit both; the
    # EC2 template's own env file had been missed in that fix.
    text = _script_text()
    assert "printf 'UV_INDEX_URL=%s\\n' \"$LAUNCHPAD_BOOT_PIP_INDEX_URL\"" in text
    assert "printf 'UV_DEFAULT_INDEX=%s\\n' \"$LAUNCHPAD_BOOT_PIP_INDEX_URL\"" in text


def test_env_file_pins_the_uv_and_npm_caches_under_the_checkout():
    # N11-r5-bug4: the owner-run first boot, the backend service (EnvironmentFile=) and
    # scripts/host-update.sh must share ONE owner-owned cache location that the ownership
    # check covers — data/ under the ubuntu-owned checkout — never ~/.cache/uv or ~/.npm,
    # which a root-run update polluted on t1 (1,625 root-owned entries).
    text = _script_text()
    lines = [ln.strip() for ln in text.splitlines()]
    uv = "printf 'UV_CACHE_DIR=%s\\n' /home/ubuntu/launchpad/data/.uv-cache"
    npm = "printf 'npm_config_cache=%s\\n' /home/ubuntu/launchpad/data/.npm-cache"
    assert uv in lines and npm in lines
    # inside the single /etc/launchpad/env block, so `set -a` + `runuser -m` carry them
    # into the owner-run block and systemd into the service
    first_env_line = lines.index("printf 'AWS_BEARER_TOKEN_BEDROCK=%s\\n' \"$BEDROCK_KEY\"")
    env_close = lines.index("} > /etc/launchpad/env")
    assert first_env_line < lines.index(uv) < env_close
    assert first_env_line < lines.index(npm) < env_close
    runuser_idx = next(i for i, ln in enumerate(lines) if ln.startswith("runuser -u ubuntu -m --"))
    assert env_close < lines.index(". /etc/launchpad/env") < runuser_idx
    host_update = (Path(__file__).resolve().parents[2] / "scripts" / "host-update.sh").read_text(
        encoding="utf-8"
    )
    assert 'export UV_CACHE_DIR="$ROOT/data/.uv-cache"' in host_update
    assert 'export npm_config_cache="$ROOT/data/.npm-cache"' in host_update


def test_ops_role_may_assume_cdk_bootstrap_roles():
    # N8 fix: `cdk deploy` under CDK v2's modern bootstrap first tries to
    # assume the bootstrap's cdk-<qualifier>-deploy-role-... (and the
    # file-publishing/lookup roles) so CloudFormation executes under
    # cdk-<qualifier>-cfn-exec-role-...; without sts:AssumeRole on those
    # roles the CLI silently falls back to this role's own credentials and
    # CloudFormation then needs every target-resource permission directly,
    # which this least-privilege role deliberately does not grant.
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [assume_statement] = [s for s in statements if s.get("Sid") == "CdkAssumeBootstrapRoles"]
    assert assume_statement["Action"] == "sts:AssumeRole"
    resources = assume_statement["Resource"]
    joined = str(resources)
    assert "role/cdk-*" in joined
    assert "${AWS::Partition}" in joined or "AWS::Partition" in joined


def test_ops_role_may_send_to_the_memory_extraction_queue():
    # N8 fix: once the N6 Memory stack merges, this host (running as this
    # role) is the SQS producer for `launchpad-memory-extract-<region>` —
    # it sends `knowledge_selfhosted.start_sync` (KB ingest) and
    # `memory_selfhosted.create_turn_event` messages itself. The ops role's
    # inline policy had no `sqs:*` action at all, which would AccessDeny
    # both flows at runtime. Least privilege: sqs:SendMessage only, scoped
    # to the queue-name prefix (not the DLQ, not `*`).
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [queue_statement] = [s for s in statements if s.get("Sid") == "MemoryQueueSend"]
    assert queue_statement["Action"] == "sqs:SendMessage"
    resources = queue_statement["Resource"]
    joined = str(resources)
    assert "launchpad-memory-extract-*" in joined
    assert "${AWS::Partition}" in joined or "AWS::Partition" in joined
    assert "${AWS::Region}" in joined or "AWS::Region" in joined
    assert "${AWS::AccountId}" in joined or "AWS::AccountId" in joined
    assert resources != "*"


def test_env_file_sets_launchpad_cdk_context_without_a_master_arn():
    # D17: the OpenSearch domain is shared (CDK stack launchpad-memory-domain) and
    # its FGAC master is that stack's own admin role, which this host *assumes*
    # (Sid MemoryAdminAssume) — the pre-D17 `memory_master_arn=<ops role arn>`
    # context, which bound the domain's master to whichever environment created
    # it, must be gone from the env file. `env_name=<EnvName>` still rides the
    # variable (scripts/bootstrap.py splits on commas).
    text = _script_text()
    assert ("printf 'LAUNCHPAD_CDK_CONTEXT=env_name=%s,codebuild_pip_index_url=%s,"
            "codebuild_pip_fallback_index_url=%s\\n' \"$LAUNCHPAD_BOOT_ENV_NAME\" "
            "\"$LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL\" "
            "\"$LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL\"") in text
    both = _both_texts() + TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "memory_master_arn" not in both.replace("`memory_master_arn=", "")  # only the comment

    user_data_text, substitutions = _user_data_sub()
    # the ops-role ARN is no longer threaded into UserData at all
    assert "${OpsRoleArn}" not in user_data_text
    assert "OpsRoleArn" not in substitutions

    for line in both.splitlines():
        if "cn-lint: allow" in line:
            continue
        if "LAUNCHPAD_CDK_CONTEXT" in line:
            assert "arn:aws:" not in line


def test_ops_role_may_assume_the_shared_memory_admin_role_only():
    # D17: every OpenSearch call of this host's backend runs as the shared
    # domain's FGAC master role launchpad-memory-admin-<region> (assumed via
    # STS), so the ops role needs sts:AssumeRole on exactly that role-name
    # prefix — and nothing wider (not role/launchpad-*, not role/*).
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [stmt] = [s for s in statements if s.get("Sid") == "MemoryAdminAssume"]
    assert stmt["Action"] == "sts:AssumeRole"
    resources = stmt["Resource"]
    joined = str(resources)
    assert "role/launchpad-memory-admin-*" in joined
    assert "${AWS::Partition}" in joined or "AWS::Partition" in joined
    assert "${AWS::AccountId}" in joined or "AWS::AccountId" in joined
    # the CDK-produced admin role name matches the pattern; a per-agent role does not
    import fnmatch

    assert fnmatch.fnmatchcase(
        "role/launchpad-memory-admin-cn-northwest-1", "role/launchpad-memory-admin-*"
    )
    assert not fnmatch.fnmatchcase(
        "role/launchpad-agent-x-12345678", "role/launchpad-memory-admin-*"
    )
    # every sts:AssumeRole grant on this role is a scoped prefix, never "*"
    for s in statements:
        actions = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
        if "sts:AssumeRole" in actions:
            assert s["Resource"] != "*"
            assert all("role/" in str(r) for r in _as_list(s["Resource"]))


def test_env_name_kb_is_refused_by_a_template_rule():
    # D17: the three layers share the AllowedPattern literal, which cannot express a
    # negative match, so the reserved name is refused by a Rule assertion instead.
    template = _load()
    rule = template["Rules"]["EnvNameIsNotReserved"]
    [assertion] = rule["Assertions"]
    assert assertion["Assert"] == {"Fn::Not": [{"Fn::Equals": [{"Ref": "EnvName"}, "kb"]}]}


def test_no_dead_launchpad_auth_cookie_secure_env_var():
    # F3: routers/auth.py::cookie_secure() is `auth_cookie_secure OR
    # run_mode=="prod"`, and this template always sets run_mode=prod, so a
    # LAUNCHPAD_AUTH_COOKIE_SECURE env line here would have zero effect on
    # actual behavior — removed rather than left as misleading dead config.
    text = _both_texts() + TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "LAUNCHPAD_AUTH_COOKIE_SECURE" not in text
    # UseTlsCookieFlag itself survives — the stub writes it to bootstrap.env as
    # LAUNCHPAD_BOOT_TLS and the body still gates the nginx self-signed cert /
    # listen-port branch and the public-origin scheme on it, which is not dead.
    assert "printf 'LAUNCHPAD_BOOT_TLS=%q\\n' \"${UseTlsCookieFlag}\"" in _user_data_text()
    assert _script_text().count('if [ "$LAUNCHPAD_BOOT_TLS" = "true" ]; then') == 2


def test_git_deploy_key_parameter_and_condition_exist():
    # N9: a private repo clone needs a credential — a read-only SSH deploy
    # key stored as its own flat SSM SecureString name, empty by default
    # (anonymous https clone unchanged).
    template = _load()
    param = template["Parameters"]["GitDeployKeyParameter"]
    assert param.get("Default") == ""
    pattern = re.compile(param["AllowedPattern"])
    assert pattern.match("")
    assert pattern.match("launchpad-git-deploy-key")
    assert not pattern.match("/launchpad/leading-slash-name")
    assert not pattern.match("foo;rm -rf")
    assert template["Conditions"]["UseGitDeployKey"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "GitDeployKeyParameter"}, ""]}]
    }


def test_git_repo_url_allowed_pattern_accepts_ssh_forms_and_rejects_shell_metacharacters():
    template = _load()
    pattern = re.compile(template["Parameters"]["GitRepoUrl"]["AllowedPattern"])
    assert pattern.match("https://github.com/aws-samples/sample-agentcore-launchpad.git")
    assert pattern.match("git@github.com:example-org/launchpad-cn.git")
    assert pattern.match("ssh://git@github.com/example-org/launchpad-cn.git")
    assert not pattern.match("foo;rm -rf")
    assert not pattern.match("git@github.com:example-org/launchpad-cn.git; rm -rf /")


def test_iam_statement_conditionally_grants_the_git_deploy_key_parameter():
    # The grant must only exist when UseGitDeployKey holds — Fn::If with
    # AWS::NoValue on the false branch so the empty default adds nothing to
    # the Resource list at all (not an empty-string ARN that matches
    # nothing but still widens the statement's shape).
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [ssm_statement] = [s for s in statements if s["Action"] == "ssm:GetParameter"]
    resources = ssm_statement["Resource"]
    conditionals = [r for r in resources if isinstance(r, dict) and "Fn::If" in r]
    # N-R6: the optional SiliconFlow key parameter follows the very same pattern
    by_condition = {c["Fn::If"][0]: c["Fn::If"] for c in conditionals}
    assert set(by_condition) == {"UseGitDeployKey", "UseSiliconFlowKey"}
    for condition_name, parameter in (("UseGitDeployKey", "GitDeployKeyParameter"),
                                      ("UseSiliconFlowKey", "SiliconFlowApiKeyParameter")):
        _name, true_branch, false_branch = by_condition[condition_name]
        assert parameter in str(true_branch)
        assert false_branch == {"Ref": "AWS::NoValue"}
    assert template["Conditions"]["UseSiliconFlowKey"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "SiliconFlowApiKeyParameter"}, ""]}]}
    assert template["Parameters"]["SiliconFlowApiKeyParameter"]["Default"] == ""


def test_user_data_writes_the_deploy_key_with_mode_0600_never_via_echo():
    text = _user_data_text()
    assert 'DEPLOY_KEY="$(aws ssm get-parameter --name "${GitDeployKeyParameter}" \\' in text
    assert "install -m 0600 -o ubuntu -g ubuntu /dev/null /home/ubuntu/.ssh/id_ed25519" in text
    assert "printf '%s\\n' \"$DEPLOY_KEY\" > /home/ubuntu/.ssh/id_ed25519" in text
    for line in text.splitlines():
        if "id_ed25519" in line or "DEPLOY_KEY" in line:
            assert "echo" not in line, f"deploy key touched by echo: {line!r}"


def test_known_hosts_pinned_with_githubs_published_host_keys():
    # Pinned literal keys from GitHub's own published host-key list — never
    # `ssh-keyscan`'d at boot (which would trust whatever answers on the wire
    # for that one boot with no verification at all).
    text = _user_data_text()
    assert "known_hosts" in text
    # The script itself never invokes ssh-keyscan; a comment is allowed to
    # name it when explaining why not.
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        assert "ssh-keyscan" not in line, f"ssh-keyscan command found: {line!r}"
    assert (
        "github.com ssh-ed25519 "
        "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl" in text
    )
    assert "github.com ecdsa-sha2-nistp256 " in text
    assert "github.com ssh-rsa " in text


def test_git_ssh_command_is_exported_and_passed_via_runuser_whitelist():
    # The task is explicit: GIT_SSH_COMMAND must be exported into the
    # runuser environment, never appended to the git command line itself
    # (which `ps`/`/proc/<pid>/cmdline` would expose to every user on the
    # host).
    text = _user_data_text()
    assert 'export GIT_SSH_COMMAND="ssh -i /home/ubuntu/.ssh/id_ed25519' in text
    assert "-o IdentitiesOnly=yes" in text
    assert "-o UserKnownHostsFile=/home/ubuntu/.ssh/known_hosts" in text
    assert "-o StrictHostKeyChecking=yes" in text
    assert "--whitelist-environment=GIT_SSH_COMMAND" in text
    for line in text.splitlines():
        if "git clone" in line:
            assert "GIT_SSH_COMMAND=" not in line, f"GIT_SSH_COMMAND on the command line: {line!r}"


def test_deploy_key_env_var_is_unset_before_the_clone_runs():
    text = _user_data_text()
    lines = text.splitlines()
    deploy_key_unset_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "unset DEPLOY_KEY"
    )
    clone_idx = next(i for i, line in enumerate(lines) if "git clone --depth 1" in line)
    assert deploy_key_unset_idx < clone_idx
    # And GIT_SSH_COMMAND itself is unset again once the clone is done, so it
    # does not leak into the body it execs (systemd/nginx setup).
    git_ssh_unset_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "unset GIT_SSH_COMMAND"
    )
    exec_idx = next(i for i, line in enumerate(lines) if line.strip().startswith("exec bash "))
    assert clone_idx < git_ssh_unset_idx < exec_idx


def test_anonymous_clone_path_is_unaffected_when_deploy_key_parameter_is_empty():
    # The `if [ -n "${GitDeployKeyParameter}" ]` guard must wrap every new
    # SSH-credential step so an empty parameter (the default) skips all of
    # it — the runuser clone call itself is unconditional and identical to
    # before this feature existed, modulo the always-present (and harmless
    # when unset) --whitelist-environment flag.
    text = _user_data_text()
    lines = text.splitlines()
    guard_line = 'if [ -n "${GitDeployKeyParameter}" ]; then'
    guard_idx = next(i for i, line in enumerate(lines) if line.strip() == guard_line)
    fi_idx = next(i for i, line in enumerate(lines) if i > guard_idx and line.strip() == "fi")
    clone_idx = next(i for i, line in enumerate(lines) if "git clone --depth 1" in line)
    assert guard_idx < fi_idx < clone_idx

    guarded_markers = (
        "install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh",
        'DEPLOY_KEY="$(aws ssm get-parameter --name "${GitDeployKeyParameter}" \\',
        "install -m 0600 -o ubuntu -g ubuntu /dev/null /home/ubuntu/.ssh/id_ed25519",
        "printf '%s\\n' \"$DEPLOY_KEY\" > /home/ubuntu/.ssh/id_ed25519",
        "unset DEPLOY_KEY",
        "install -m 0644 -o ubuntu -g ubuntu /dev/null /home/ubuntu/.ssh/known_hosts",
        "cat > /home/ubuntu/.ssh/known_hosts",
        'export GIT_SSH_COMMAND="ssh -i /home/ubuntu/.ssh/id_ed25519',
    )
    for marker in guarded_markers:
        [idx] = [i for i, line in enumerate(lines) if marker in line]
        assert guard_idx < idx < fi_idx, f"{marker!r} must sit inside the if-guard"

    # The heredoc terminator itself (an exact line, not a substring match —
    # "KNOWN_HOSTS" also appears inside the `<<'KNOWN_HOSTS'` opening line).
    [known_hosts_terminator_idx] = [
        i for i, line in enumerate(lines) if line.strip() == "KNOWN_HOSTS"
    ]
    assert guard_idx < known_hosts_terminator_idx < fi_idx

    # Nothing SSH-credential-related leaks out before the guard or after fi —
    # anywhere else in the stub, nor anywhere in the body (the Parameters
    # section legitimately documents /home/ubuntu/.ssh/id_ed25519 in prose,
    # but that is not script text).
    for i, line in enumerate(lines):
        if guard_idx <= i <= fi_idx:
            continue
        assert "/home/ubuntu/.ssh/id_ed25519" not in line, (
            f"id_ed25519 outside the if-guard: {line!r}"
        )
        assert "/home/ubuntu/.ssh/known_hosts" not in line, (
            f"known_hosts outside the if-guard: {line!r}"
        )
    assert "id_ed25519" not in _script_text() and ".ssh" not in _script_text()


def test_umask_is_restored_to_022_after_the_secrets_block_before_the_clone():
    # N9 follow-up (nginx 403 fix): `umask 077` (set before the Bedrock/
    # console-password secrets) must not leak into the rest of boot — nginx
    # (www-data) serves /home/ubuntu/launchpad/frontend/dist, so the clone
    # and `npm run build` need normal, world-readable+traversable
    # permissions, not the secrets' own tight modes. The reset must land
    # after every secret write — including the N9 deploy-key/.ssh block,
    # which must run entirely under the original 077 — and before the git
    # clone. (Stub side: bootstrap.env and the deploy key; the body has its
    # own 077 -> /etc/launchpad/env -> 022 sequence, checked above.)
    text = _user_data_text()
    lines = text.splitlines()
    first_umask_idx = next(i for i, line in enumerate(lines) if line.strip() == "umask 077")
    bootstrap_env_idx = next(
        i for i, line in enumerate(lines) if line.strip() == f"}} > {BOOTSTRAP_ENV}"
    )
    assert first_umask_idx < bootstrap_env_idx, "bootstrap.env must be created under umask 077"
    ssh_dir_idx = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == "install -d -m 0700 -o ubuntu -g ubuntu /home/ubuntu/.ssh"
    )
    known_hosts_install_line = (
        "install -m 0644 -o ubuntu -g ubuntu /dev/null /home/ubuntu/.ssh/known_hosts"
    )
    known_hosts_install_idx = next(
        i for i, line in enumerate(lines) if line.strip() == known_hosts_install_line
    )
    reset_umask_idx = next(i for i, line in enumerate(lines) if line.strip() == "umask 022")
    clone_idx = next(i for i, line in enumerate(lines) if "git clone --depth 1" in line)

    assert first_umask_idx < ssh_dir_idx, "the deploy-key .ssh writes must happen under umask 077"
    assert ssh_dir_idx < known_hosts_install_idx < reset_umask_idx, (
        "every .ssh write must precede the umask 022 reset"
    )
    assert reset_umask_idx < clone_idx, "umask must be restored before the git clone"


def test_launchpad_checkout_dir_and_home_are_traversable_for_nginx():
    # nginx runs as www-data; Ubuntu 24.04's default /home/ubuntu (0750) and
    # a umask-077-created checkout dir would both 403 it regardless of file
    # permissions underneath. The stub creates the checkout dir (it clones);
    # the body widens /home/ubuntu right before it configures nginx.
    stub = _user_data_text()
    assert "REPO=/home/ubuntu/launchpad" in stub
    assert 'install -d -m 0755 -o ubuntu -g ubuntu "$REPO"' in stub
    assert "chmod 0711 /home/ubuntu" in _script_text()


def test_nginx_root_serves_the_cloned_launchpad_checkout():
    # WB-003: the site is rendered by infra/ec2/nginx-site.sh (tests/test_nginx_site.py);
    # the body hands it the cloned checkout's dist as the root
    text = _script_text()
    assert "/home/ubuntu/launchpad/frontend/dist 8000 >/etc/nginx/sites-available/launchpad" in text
    assert "root /home/ubuntu/launchpad/frontend/dist;" not in text  # no inline copy of the site


def test_python3_pip_is_in_the_apt_install_list():
    # N11 r1: Ubuntu 24.04 (noble) dropped the `awscli` apt package entirely
    # (confirmed on packages.ubuntu.com — jammy still ships 1.22.x in
    # universe, noble has no candidate at all), so the AWS CLI is installed
    # via pip instead; python3-pip must be in the stub's one apt transaction
    # (the stub installs only what the clone + CLI need; the body installs the
    # rest of the toolchain once the checkout exists).
    text = _user_data_text()
    lines = text.splitlines()
    apt_idx = next(
        i for i, line in enumerate(lines) if line.strip().startswith("apt-get install -y")
    )
    # The install list spans a line continuation (`\`); gather it and the
    # immediately following continuation line(s).
    install_block = [lines[apt_idx]]
    i = apt_idx
    while install_block[-1].rstrip().endswith("\\"):
        i += 1
        install_block.append(lines[i])
    joined = " ".join(install_block)
    assert "python3-pip" in joined
    for pkg in ("git", "curl", "ca-certificates", "python3"):
        assert f" {pkg}" in joined, pkg
    # the body picks up the rest (nginx/build-essential/openssl/jq) itself
    body_apt = next(ln for ln in _script_text().splitlines() if ln.startswith("apt-get install -y"))
    assert "nginx" in body_apt and "build-essential" in body_apt


def test_awscli_never_downloaded_from_the_global_endpoint():
    # Explicit project constraint: Global egress (including
    # awscli.amazonaws.com) is unreliable from China — the AWS CLI must come
    # from a China-reachable mirror (here: pip against PipIndexUrl), never
    # the official installer/zip endpoint.
    assert "awscli.amazonaws.com" not in _both_texts()
    assert "awscli.amazonaws.com" not in TEMPLATE_PATH.read_text(encoding="utf-8")


def test_awscli_installed_before_signal_stack_definition_and_first_aws_call():
    # N11 r1 (live deployment failure): "aws: command not found" on both the
    # secrets block and signal_stack during the first real cn-northwest-1
    # deploy — the AMI ships no AWS CLI. The pip-based install (and its
    # command -v guard) must land before signal_stack is defined/trapped and
    # before every other `aws ` invocation in the script, since the EXIT trap
    # itself depends on `aws cloudformation signal-resource` working.
    text = _user_data_text()
    lines = text.splitlines()

    install_idx = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == 'python3 -m pip install --break-system-packages --no-cache-dir \\'
    )
    guard_idx = next(
        i
        for i, line in enumerate(lines)
        if line.strip() == 'command -v aws >/dev/null || { echo "aws cli missing" >&2; exit 1; }'
    )
    signal_stack_def_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "signal_stack() {"
    )
    trap_idx = next(i for i, line in enumerate(lines) if line.strip() == "trap on_exit EXIT")
    first_aws_call_idx = next(
        i
        for i, line in enumerate(lines)
        if re.search(r"\baws\s", line.strip()) and i > guard_idx
    )

    assert install_idx < guard_idx
    assert guard_idx < signal_stack_def_idx
    assert guard_idx < trap_idx
    assert guard_idx < first_aws_call_idx

    # the body inherits the CLI but re-guards, and its ERR trap follows its own
    # signal_stack definition exactly as the inline script's did
    body = [ln.strip() for ln in _script_text().splitlines()]
    body_guard = body.index('command -v aws >/dev/null || { echo "aws cli missing" >&2; exit 1; }')
    body_def = body.index("signal_stack() {")
    body_trap = body.index("trap 'signal_stack FAILURE || true' ERR")
    assert body_guard < body_def < body_trap


def test_awscli_pip_install_uses_the_china_mirror_not_a_literal_pypi_url():
    assert '--index-url "${PipIndexUrl}" awscli' in _user_data_text()
    assert "pip install" not in _script_text()  # the body never installs the CLI again


def test_command_v_aws_guard_fails_fast_if_pip_install_did_not_produce_a_binary():
    guard = 'command -v aws >/dev/null || { echo "aws cli missing" >&2; exit 1; }'
    assert guard in _user_data_text()
    assert guard in _script_text()


def test_region_is_exported_before_the_first_aws_cli_call():
    # N11 r2 (live deployment failure #2): awscli v1 (what pip installs) does
    # not derive the region from IMDS, so the first `aws ssm get-parameter`
    # failed with "You must specify a region". The script must export
    # AWS_DEFAULT_REGION from the pseudo parameter before any `aws ` call —
    # and the body, which may also be run by hand, re-exports it from
    # bootstrap.env before its own first call.
    def first_aws_call(lines: list[str]) -> int:
        return next(
            i
            for i, line in enumerate(lines)
            if re.search(r"\baws\s", line.strip()) and not line.strip().startswith("#")
        )

    stub_lines = _user_data_text().splitlines()
    export_idx = next(
        i
        for i, line in enumerate(stub_lines)
        if line.strip() == 'export AWS_DEFAULT_REGION="${AWS::Region}"'
    )
    assert export_idx < first_aws_call(stub_lines)

    body_lines = _script_text().splitlines()
    body_export_idx = next(
        i
        for i, line in enumerate(body_lines)
        if line.strip() == 'export AWS_DEFAULT_REGION="$LAUNCHPAD_BOOT_REGION"'
    )
    assert body_export_idx < first_aws_call(body_lines)
    # the region export must come from the pseudo parameter, not a literal
    assert not re.search(r'export AWS_DEFAULT_REGION="cn-', _both_texts())


def test_ops_role_may_create_identity_provider_secrets_only_under_the_reserved_prefix():
    # N11-bug7 (live): CreateApiKeyCredentialProvider -> AccessDenied
    # secretsmanager:CreateSecret. The grant must stay scoped to AgentCore
    # Identity's reserved secret prefix, never to secret:* .
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    statements = [s for p in policies for s in p["PolicyDocument"]["Statement"]]
    sm = [s for s in statements if s.get("Sid") == "IdentityCredentialProviderSecrets"]
    assert len(sm) == 1
    assert "secretsmanager:CreateSecret" in sm[0]["Action"]
    assert "secretsmanager:GetSecretValue" not in sm[0]["Action"]
    resources = sm[0]["Resource"]
    assert all("secret:bedrock-agentcore-identity!*" in str(r) for r in resources)
    for s in statements:
        if s.get("Sid") != "IdentityCredentialProviderSecrets":
            assert "secretsmanager" not in str(s.get("Action"))


def test_ops_role_may_read_api_gateway_api_keys_only():
    # N11-bug8 (live): gateway stage GetApiKey(includeValue) -> AccessDenied
    # apigateway:GET on /apikeys/<id>. Read-only, keys resource only.
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    statements = [s for p in policies for s in p["PolicyDocument"]["Statement"]]
    apigw = [s for s in statements if s.get("Sid") == "ApiGatewayReadOfficeFactsApiKey"]
    assert len(apigw) == 1
    assert apigw[0]["Action"] == "apigateway:GET"
    assert all("::/apikeys" in str(r) for r in apigw[0]["Resource"])
    for s in statements:
        if s.get("Sid") != "ApiGatewayReadOfficeFactsApiKey":
            assert "apigateway" not in str(s.get("Action"))


def test_ops_role_may_enable_transaction_search_including_application_signals_discovery():
    # N11-bug9 (live): UpdateTraceSegmentDestination -> AccessDenied
    # application-signals:StartDiscovery (plus the Application Signals SLR).
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    statements = [s for p in policies for s in p["PolicyDocument"]["Statement"]]
    obs = next(s for s in statements if s.get("Sid") == "ObservabilityForBootstrap")
    for action in (
        "xray:UpdateTraceSegmentDestination",
        "xray:UpdateIndexingRule",
        "application-signals:StartDiscovery",
    ):
        assert action in obs["Action"]
    slr = next(s for s in statements if s.get("Sid") == "ApplicationSignalsServiceLinkedRole")
    assert slr["Action"] == "iam:CreateServiceLinkedRole"
    assert all(
        "aws-service-role/application-signals.cloudwatch.amazonaws.com/" in str(r)
        for r in slr["Resource"]
    )
    ct = next(s for s in statements if s.get("Sid") == "ApplicationSignalsCloudTrailChannel")
    assert "cloudtrail:CreateServiceLinkedChannel" in ct["Action"]
    assert all("channel/aws-service-channel/" in str(r) for r in ct["Resource"])
    for s in statements:
        if s.get("Sid") != "ApplicationSignalsCloudTrailChannel":
            assert "cloudtrail" not in str(s.get("Action"))


def test_console_port_is_a_high_port_and_80_443_are_rejected():
    # N11-bug12 (live): China-partition public IPs do not accept inbound 80/443
    # without an ICP filing; the console must live on a high port.
    template = _load()
    port = template["Parameters"]["ConsolePort"]
    assert port["Default"] == 8443
    assert port["MinValue"] >= 1024
    rule = template["Rules"]["ConsolePortIsNotAnIcpGatedPort"]["Assertions"][0]
    assert "80" in str(rule["Assert"]) and "443" in str(rule["Assert"])
    assert "printf 'LAUNCHPAD_BOOT_CONSOLE_PORT=%q\\n' \"${ConsolePort}\"" in _user_data_text()
    text = _script_text()
    # WB-003: the port reaches nginx through the shared renderer's second argument
    assert 'render_nginx_site "$LAUNCHPAD_BOOT_TLS" "$LAUNCHPAD_BOOT_CONSOLE_PORT"' in text
    site = (SCRIPT_PATH.parent / "nginx-site.sh").read_text(encoding="utf-8")
    assert 'listen_block="listen ${port} ssl default_server;' in site
    assert 'listen_block="listen ${port} default_server;"' in site
    assert "listen 443" not in site + text and "listen 80" not in site + text
    url = template["Outputs"]["ConsoleUrl"]["Value"]
    assert ":${ConsolePort}" in str(url)


def test_git_clone_is_bounded_retried_and_sparse():
    # N11-bug6 / obs10 (live): an unbounded full clone of a 184 MB tree over a
    # stalling GitHub link consumed the whole signal window. N11-r5-bug2 (live): a
    # fixed 600 s per command killed the blob fetch of the sparse set at 10 MB on a
    # ~36 KB/s link, and the retry reset the directory and started from zero.
    text = _user_data_text()
    lines = [ln.strip() for ln in text.splitlines()]
    clone = next(line for line in lines if "git clone --depth 1" in line)
    # every git network command runs as ubuntu under the REMAINING GitCloneTimeout budget
    assert clone.startswith("as_ubuntu git clone --depth 1 --filter=blob:none --sparse")
    assert "DEADLINE=$((SECONDS + ${GitCloneTimeout}))" in lines
    assert (
        'as_ubuntu() { runuser -u ubuntu --whitelist-environment=GIT_SSH_COMMAND -- '
        'timeout "$(remaining)" "$@"; }'
    ) in lines
    assert "timeout 600" not in text
    assert "for attempt in 1 2 3; do" in lines
    assert any(ln.startswith('[ "$CLONE_OK" = yes ] ||') and "exit 1" in ln for ln in lines)
    assert "-o ServerAliveInterval=15" in text and "-o ConnectTimeout=20" in text
    # every git network command (not the log messages naming it) goes through as_ubuntu
    git_cmds = [
        ln for ln in lines
        if re.search(r"\bgit (clone|-C|fetch|checkout|sparse)", ln) and 'echo "' not in ln
    ]
    prefixes = ("as_ubuntu ", "if ! as_ubuntu ", "if as_ubuntu ", "&& as_ubuntu ")
    assert git_cmds and all(ln.startswith(prefixes) for ln in git_cmds), git_cmds


def test_git_clone_timeout_parameter_bounds_the_whole_fetch_phase():
    # N11-r5-bug2: Number parameter, default 1800, 300..3000, consumed by the stub
    # only (the body never fetches) — and the sparse set / clone / retries all share
    # the one budget so the fetch phase can never exceed it whatever the attempt
    # count (the old "3 attempts × timeout" arithmetic against PT60M is gone).
    template = _load()
    param = template["Parameters"]["GitCloneTimeout"]
    assert param["Type"] == "Number"
    assert param["Default"] == 1800
    assert param["MinValue"] == 300
    assert param["MaxValue"] == 3000
    assert "PT60M" in param["Description"]
    signal = template["Resources"]["Instance"]["CreationPolicy"]["ResourceSignal"]
    assert signal["Timeout"] == "PT60M"
    stub = _user_data_text()
    assert stub.count("${GitCloneTimeout}") == 2  # the deadline + the failure message
    assert "GitCloneTimeout" not in _script_text()
    assert "GitCloneTimeout" not in "\n".join(_bootstrap_env_block(stub))
    lines = [ln.strip() for ln in stub.splitlines()]
    remaining = next(ln for ln in lines if ln.startswith("remaining()"))
    assert "DEADLINE - SECONDS" in remaining and "echo 1" in remaining  # never `timeout 0`
    assert lines.index("DEADLINE=$((SECONDS + ${GitCloneTimeout}))") < lines.index(
        "for attempt in 1 2 3; do"
    )


def test_sparse_set_is_what_the_host_runs_and_excludes_docs_tests_design():
    # N11-r5-bug2: docs/ (840 blobs, 194 MB of evidence PNGs), tests/ (Playwright e2e,
    # never run on the host) and design/ are excluded; .superpowers/.worktrees are
    # gitignored local trees named for safety. backend, frontend, infra, scripts,
    # samples, config, vendor and the root files stay — nothing on the host reads
    # docs/ or tests/ at runtime (test_docs_tracked_links skips without docs/).
    stub = _user_data_text()
    base = next(ln.strip() for ln in stub.splitlines() if "sparse-checkout set --no-cone" in ln)
    for excluded in ("'!/docs'", "'!/tests'", "'!/design'", "'!/.superpowers'", "'!/.worktrees'"):
        assert excluded in base, excluded
    assert base.startswith("if as_ubuntu git -C \"$REPO\" sparse-checkout set --no-cone '/*' ")
    assert "workshop" not in stub  # the old partial exclusion is gone
    # staged materialisation: the three big trees are excluded from the base set and
    # re-included one `sparse-checkout add` at a time, so a killed stage keeps the
    # completed ones (git discards a killed pack; smaller batches are the remedy)
    for tree in ("vendor", "frontend", "backend"):
        assert f"'!/{tree}'" in base, tree
        assert f"as_ubuntu git -C \"$REPO\" sparse-checkout add '/{tree}'" in stub, tree
    lines = [ln.strip() for ln in stub.splitlines()]
    adds = [i for i, ln in enumerate(lines) if "sparse-checkout add" in ln]
    base_idx = next(i for i, ln in enumerate(lines) if "sparse-checkout set --no-cone" in ln)
    assert len(adds) == 3 and base_idx < adds[0] < adds[1] < adds[2]
    assert lines[adds[2]].endswith("sparse-checkout add '/backend'")  # the biggest last
    # the included trees are the ones the body/host actually uses
    body = _script_text()
    for needed in ("/home/ubuntu/launchpad/backend", "/home/ubuntu/launchpad/frontend/dist",
                   "cd infra", "make bootstrap"):
        assert needed in body, needed


def test_clone_retry_is_resumable_and_only_a_missing_head_resets_the_checkout():
    # N11-r5-bug2: the checkout is reset (rm -rf) ONLY when `git rev-parse HEAD`
    # fails as ubuntu (no clone / a half-made clone); a successful clone whose
    # sparse-checkout stage was killed keeps its packs and the retry resumes.
    stub = _user_data_text()
    lines = [ln.strip() for ln in stub.splitlines()]
    loop = lines.index("for attempt in 1 2 3; do")
    done = next(i for i, ln in enumerate(lines) if ln == "done" and i > loop)
    body = lines[loop + 1 : done]
    guard = next(
        i for i, ln in enumerate(body) if ln.startswith("if ! as_ubuntu git -C \"$REPO\" rev-parse")
    )
    fi = next(i for i, ln in enumerate(body) if ln == "fi" and i > guard)
    resets = [i for i, ln in enumerate(body) if ln.startswith("rm -rf")]
    assert resets == [guard + 1], "the only rm -rf sits right inside the missing-HEAD guard"
    assert body[guard] == (
        'if ! as_ubuntu git -C "$REPO" rev-parse --verify -q HEAD >/dev/null 2>&1; then'
    )
    assert guard < resets[0] < fi
    assert "install -d -m 0755 -o ubuntu -g ubuntu \"$REPO\"" in body[guard:fi]
    clone = next(ln for ln in body[guard:fi] if "git clone --depth 1" in ln)
    assert clone.endswith('"$REPO" \\')
    # a failed clone retries the loop (continue) — it never falls through to the stages
    assert any(ln.startswith("|| {") and "continue;" in ln for ln in body[guard:fi])
    # the stages come after the guard and never reset anything
    stages = [i for i, ln in enumerate(body) if "sparse-checkout" in ln]
    assert stages and stages[0] > fi
    assert not any("rm -rf" in body[i] for i in range(fi, len(body)))
    assert any("resuming in 20s" in ln for ln in body)
    assert lines.index("REPO=/home/ubuntu/launchpad") < loop


def test_creation_policy_allows_a_cold_environment():
    # N11-obs15: a cold EnvName deployment (base + OpenSearch memory stack from
    # scratch) needs ~40 min; PT30M only fits the pre-provisioned path.
    template = _load()
    signal = template["Resources"]["Instance"]["CreationPolicy"]["ResourceSignal"]
    assert signal["Timeout"] == "PT60M"
# ── N11: EnvName suffix for parallel environments ─────────────────────────────


def _resolve(value, env_name: str, region: str = "cn-northwest-1") -> str:
    """Evaluate the `!Sub [tmpl, {Var: !If [HasEnvName, !Sub "-${EnvName}", ""]}]`
    shape this template uses for every suffixed name, for one EnvName — a tiny
    two-branch evaluator, not a CloudFormation emulator."""
    if isinstance(value, str):
        return value.replace("${AWS::Region}", region)
    template, variables = value["Fn::Sub"]
    out = template.replace("${AWS::Region}", region)
    for key, expr in variables.items():
        cond, when_true, when_false = expr["Fn::If"]
        assert cond == "HasEnvName"
        chosen = when_true if env_name else when_false
        if isinstance(chosen, dict):  # !Sub "-${EnvName}" / " (${EnvName})"
            chosen = chosen["Fn::Sub"].replace("${EnvName}", env_name)
        out = out.replace(f"${{{key}}}", chosen)
    return out


def test_env_name_parameter_defaults_to_empty_and_shares_the_three_layer_pattern():
    from stacks.naming import ENV_NAME_PATTERN

    template = _load()
    param = template["Parameters"]["EnvName"]
    assert param["Default"] == ""
    assert param["AllowedPattern"] == ENV_NAME_PATTERN == "^[a-z0-9]{0,3}$"
    pattern = re.compile(param["AllowedPattern"])
    for ok in ("", "t1", "abc", "999"):
        assert pattern.fullmatch(ok), ok
    for bad in ("T1", "t-1", "t_1", "abcd", "t1 ", "prod"):
        assert not pattern.fullmatch(bad), bad
    assert template["Conditions"]["HasEnvName"] == {
        "Fn::Not": [{"Fn::Equals": [{"Ref": "EnvName"}, ""]}]
    }


def test_ops_role_name_is_suffixed_at_the_end_only_when_env_name_is_set():
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    role_name = role["Properties"]["RoleName"]
    # default: byte-identical to the pre-N11 name
    assert _resolve(role_name, "") == "launchpad-cn-ops-cn-northwest-1"
    assert _resolve(role_name, "t1") == "launchpad-cn-ops-cn-northwest-1-t1"
    assert len(_resolve(role_name, "zzz")) <= 64  # IAM role-name limit


def test_every_fixed_name_tag_is_suffixed_the_same_way():
    template = _load()
    tagged = {
        "Vpc": "launchpad-cn-ops",
        "PublicSubnet": "launchpad-cn-ops-public",
        "Instance": "launchpad-cn-ops",
        "PolicyPrivateSubnetA": "launchpad-cn-policy-a",
        "PolicyPrivateSubnetB": "launchpad-cn-policy-b",
    }
    for logical_id, base in tagged.items():
        tags = template["Resources"][logical_id]["Properties"]["Tags"]
        [name_tag] = [t for t in tags if t["Key"] == "Name"]
        assert _resolve(name_tag["Value"], "") == base, logical_id
        assert _resolve(name_tag["Value"], "t1") == f"{base}-t1", logical_id
    sg = template["Resources"]["ConsoleSecurityGroup"]
    desc = sg["Properties"]["GroupDescription"]
    assert _resolve(desc, "") == "Launchpad console - inbound ConsolePort from AllowedCidr only."
    assert "(t1)" in _resolve(desc, "t1")


def test_ops_role_wildcards_still_match_the_suffixed_resource_names():
    """The ops role scopes IAM/ECR/CodeBuild/SQS to launchpad-* prefixes. Every name the
    CDK app produces for env_name=t1 (infra/tests/test_naming.py) must still fall under
    them — otherwise `make bootstrap` on a suffixed host would be AccessDenied."""
    import fnmatch

    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}

    def patterns(sid: str) -> list[str]:
        res = statements[sid]["Resource"]
        res = res if isinstance(res, list) else [res]
        out = []
        for r in res:
            text = r["Fn::Sub"] if isinstance(r, dict) else r
            out.append(text.rsplit(":", 1)[-1])  # the resource segment of the ARN
        return out

    def matches(sid: str, resource: str) -> bool:
        return any(fnmatch.fnmatchcase(resource, p) for p in patterns(sid))

    assert matches("IamForLaunchpadRoles", "role/launchpad-agent-execution-role-cn-northwest-1-t1")
    assert matches("IamForLaunchpadRoles", "role/launchpad-gateway-role-cn-northwest-1-t1")
    assert matches("IamForLaunchpadRoles", "role/launchpad-memory-extractor-cn-northwest-1-t1")
    assert matches("IamForLaunchpadRoles", "role/launchpad-agent-my-agent-0123abcd-t1")
    assert matches("EcrForLaunchpadRepos", "repository/launchpad-agents-t1")
    assert matches("CodeBuildForLaunchpadProjects", "project/launchpad-agent-builder-t1")
    assert matches("MemoryQueueSend", "launchpad-memory-extract-cn-northwest-1-t1")
    # ...and nothing outside the launchpad-* namespace is admitted by the suffix
    assert not matches("IamForLaunchpadRoles", "role/someone-else-t1")
    assert not matches("EcrForLaunchpadRepos", "repository/other-t1")


def test_user_data_writes_env_name_into_the_env_file_and_the_cdk_context():
    assert "printf 'LAUNCHPAD_BOOT_ENV_NAME=%q\\n' \"${EnvName}\"" in _user_data_text()
    assert "printf 'LAUNCHPAD_ENV_NAME=%s\\n' \"$LAUNCHPAD_BOOT_ENV_NAME\"" in _script_text()
    # the only `${AWS::StackName}` uses are the cfn-signal (stub) and handing the
    # body the stack name for ITS cfn-signal; nothing derives a resource name
    # from the EC2 stack's own name
    text = TEMPLATE_PATH.read_text(encoding="utf-8")
    stack_name_lines = [ln for ln in text.splitlines() if "${AWS::StackName}" in ln]
    assert stack_name_lines and all(
        "signal-resource" in ln or "LAUNCHPAD_BOOT_STACK_NAME=" in ln for ln in stack_name_lines
    )
    assert "LAUNCHPAD_BOOT_STACK_NAME" in next(
        ln for ln in _script_text().splitlines() if "signal-resource" in ln
    )


def test_outputs_echo_env_name_and_ops_role_name():
    template = _load()
    assert template["Outputs"]["EnvName"]["Value"] == {"Ref": "EnvName"}
    assert template["Outputs"]["OpsRoleName"]["Value"] == {"Ref": "InstanceRole"}


def _ops_role_statements() -> list[dict]:
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    return [s for p in policies for s in p["PolicyDocument"]["Statement"]]


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def test_ops_role_may_create_the_cdk_bootstrap_ecr_repo_with_lifecycle_actions_only():
    # N11-bug3-r1 (live): cold `cdk bootstrap` -> ecr:CreateRepository denied on
    # repository/cdk-hnb659fds-container-assets-…; the launchpad-* grant never
    # matched the CDKToolkit's own repo. The new grant must be the bootstrap
    # template's lifecycle actions only, on cdk-* repositories only.
    statements = _ops_role_statements()
    [ecr] = [s for s in statements if s.get("Sid") == "EcrForCdkBootstrapRepo"]
    assert sorted(ecr["Action"]) == sorted(
        [
            "ecr:CreateRepository",
            "ecr:DeleteRepository",
            "ecr:DescribeRepositories",
            "ecr:SetRepositoryPolicy",
            "ecr:DeleteRepositoryPolicy",
            "ecr:PutLifecyclePolicy",
            "ecr:DeleteLifecyclePolicy",
            "ecr:PutImageTagMutability",
            "ecr:PutImageScanningConfiguration",
            "ecr:TagResource",
        ]
    )
    assert "ecr:*" not in ecr["Action"]
    resources = _as_list(ecr["Resource"])
    assert resources and all(":repository/cdk-*" in str(r) for r in resources), resources
    assert all("${AWS::Partition}" in str(r) for r in resources)
    # ecr:* stays confined to the launchpad-* repositories; the only ecr grant on "*"
    # remains GetAuthorizationToken (not resource-scopable).
    for s in statements:
        actions = _as_list(s.get("Action"))
        if "ecr:*" in actions:
            assert s["Sid"] == "EcrForLaunchpadRepos"
            assert all(":repository/launchpad-*" in str(r) for r in _as_list(s["Resource"]))
        if any(a.startswith("ecr:") for a in actions) and s.get("Resource") == "*":
            assert actions == ["ecr:GetAuthorizationToken"], s


def test_ops_role_may_write_the_cdk_bootstrap_version_parameter_only():
    # N11-bug3-r1 (live): CdkBootstrapVersion -> ssm:PutParameter denied, then the
    # rollback stuck in ROLLBACK_FAILED on ssm:DeleteParameter. Scoped to the
    # /cdk-bootstrap/ hierarchy; the three secret parameters stay GetParameter-only.
    statements = _ops_role_statements()
    [ssm] = [s for s in statements if s.get("Sid") == "SsmForCdkBootstrapVersion"]
    assert sorted(ssm["Action"]) == sorted(
        [
            "ssm:PutParameter",
            "ssm:GetParameter",
            "ssm:GetParameters",
            "ssm:DeleteParameter",
            "ssm:AddTagsToResource",
        ]
    )
    resources = _as_list(ssm["Resource"])
    assert resources and all(":parameter/cdk-bootstrap/*" in str(r) for r in resources)
    assert all("${AWS::Partition}" in str(r) for r in resources)
    # no other statement may write SSM, and no ssm grant is ever on "*" — except N17-obs203's
    # `SsmWriteLaunchpadSecretParameters` (Put / Delete / Tag on the `launchpad-*-api-key`
    # family only, pinned by its own test below); the boot-path parameters stay read-only
    for s in statements:
        actions = _as_list(s.get("Action"))
        ssm_actions = [a for a in actions if a.startswith("ssm:")]
        if not ssm_actions:
            continue
        assert s.get("Resource") != "*", s
        if s["Sid"] == "SsmWriteLaunchpadSecretParameters":
            assert all(":parameter/launchpad-*-api-key" in str(r)
                       for r in _as_list(s["Resource"])), s
            continue
        if s["Sid"] != "SsmForCdkBootstrapVersion":
            assert ssm_actions == ["ssm:GetParameter"], s
            assert s["Sid"] == "ReadLaunchpadSecretParameters"


def test_user_data_writes_the_public_base_url_from_imdsv2_and_tls_mode():
    # N11-obs14 (live): run_mode=prod warned that the as-user (3LO) OAuth return URL
    # was http://localhost:5173/auth/return because UserData never wrote
    # LAUNCHPAD_PUBLIC_BASE_URL. It must be <scheme>://<public-ipv4>:<ConsolePort>,
    # public IP via IMDSv2 (token header), scheme from TlsMode's UseTls condition.
    text = _script_text()
    lines = text.splitlines()

    def idx(needle: str) -> int:
        matches = [i for i, ln in enumerate(lines) if needle in ln]
        assert len(matches) == 1, (needle, matches)
        return matches[0]

    printf_idx = idx(
        "printf 'LAUNCHPAD_PUBLIC_BASE_URL=%s://%s:%s\\n' "
        '"$CONSOLE_SCHEME" "$PUBLIC_IP" "$LAUNCHPAD_BOOT_CONSOLE_PORT"'
    )
    # inside the single env-file heredoc block
    first_env_line = idx("printf 'AWS_BEARER_TOKEN_BEDROCK=%s\\n' \"$BEDROCK_KEY\"")
    assert first_env_line < printf_idx < idx("} > /etc/launchpad/env")
    # public IPv4 from IMDSv2, with the session token header, before the block
    public_ip_idx = idx('PUBLIC_IP="$(curl --fail --silent --show-error --retry 10 \\')
    assert public_ip_idx < printf_idx
    ip_block = "\n".join(lines[public_ip_idx : public_ip_idx + 6])
    assert ip_block.count('X-aws-ec2-metadata-token: ${IMDS_TOKEN}') == 2
    assert "latest/meta-data/public-ipv4" in ip_block
    # no public IPv4 (IMDS 404) must not abort boot under set -e: private IPv4 fallback
    assert "|| curl --fail" in ip_block and "latest/meta-data/local-ipv4" in ip_block
    assert idx("IMDS_TOKEN=\"$(curl") < public_ip_idx
    # scheme follows TlsMode via the existing UseTls -> UseTlsCookieFlag substitution
    https_idx = idx("CONSOLE_SCHEME=https")
    assert https_idx < printf_idx
    assert lines[https_idx - 1].strip() == 'if [ "$LAUNCHPAD_BOOT_TLS" = "true" ]; then'
    assert lines[https_idx + 1].strip() == "else"
    assert lines[https_idx + 2].strip() == "CONSOLE_SCHEME=http"
    template = _load()
    assert template["Conditions"]["UseTls"] == {"Fn::Equals": [{"Ref": "TlsMode"}, "selfsigned"]}
    # never a literal origin
    assert "LAUNCHPAD_PUBLIC_BASE_URL=http" not in text


def test_ops_role_may_list_role_policies_for_agent_role_teardown():
    # N11 r2-B: agent delete lists the per-agent role's policies before deleting it.
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    statements = [s for p in policies for s in p["PolicyDocument"]["Statement"]]
    iam_stmt = next(s for s in statements if s.get("Sid") == "IamForLaunchpadRoles")
    assert "iam:ListRolePolicies" in iam_stmt["Action"]
    assert "iam:ListAttachedRolePolicies" in iam_stmt["Action"]
    assert all("launchpad-" in str(r) or "cdk-" in str(r) for r in iam_stmt["Resource"])


def _adopt_statements() -> tuple[dict, dict, dict]:
    statements = _ops_role_statements()
    broad = next(s for s in statements if s.get("Sid") == "IamForLaunchpadRoles")
    agent = next(s for s in statements if s.get("Sid") == "IamAdoptAgentRoles")
    workspace = next(s for s in statements if s.get("Sid") == "IamAdoptWorkspaceRoles")
    return broad, agent, workspace


def _patterns(statement: dict) -> list[str]:
    resources = _as_list(statement["Resource"])
    assert all(isinstance(r, dict) and r["Fn::Sub"].startswith(
        "arn:${AWS::Partition}:iam::${AWS::AccountId}:role/launchpad-") for r in resources)
    return [r["Fn::Sub"].rsplit(":", 1)[-1] for r in resources]


def test_ops_role_may_refresh_the_trust_policy_of_the_roles_it_adopts_only():
    """N11 r3-B (N10v2-bug1): a resumed deploy adopts the agent's existing execution
    role and refreshes its trust policy only when it differs; the ops role must be
    allowed that write — on the per-agent `launchpad-agent-*` roles (`IamAdoptAgentRoles`)
    and the four roles the workspace bootstrap makes and re-adopts with the same refresh
    (`IamAdoptWorkspaceRoles`, N17-obs201: the Skill Lab worker, the regional execution role,
    and, N17-obs131, the gateway and CodeBuild roles), and nowhere else (the broad
    IamForLaunchpadRoles statement stays without it; the ops / memory-admin / CDK roles stay
    out of reach)."""
    import fnmatch

    broad, agent, workspace = _adopt_statements()
    assert "iam:UpdateAssumeRolePolicy" not in broad["Action"]
    for statement in (agent, workspace):
        assert _as_list(statement["Action"]) == ["iam:UpdateAssumeRolePolicy"]
        assert statement["Effect"] == "Allow"
    assert _patterns(agent) == ["role/launchpad-agent-*"]
    patterns = _patterns(workspace)
    assert patterns == [
        "role/launchpad-agent-execution-role-*",
        "role/launchpad-skill-lab-worker-*",
        "role/launchpad-gateway-role-*",
        "role/launchpad-codebuild-role-*",
    ]

    def matches(resource: str) -> bool:
        return any(fnmatch.fnmatchcase(resource, p) for p in patterns)

    agent_glob = "role/launchpad-agent-*"
    assert fnmatch.fnmatchcase("role/launchpad-agent-my-agent-0123abcd-t1", agent_glob)
    assert matches("role/launchpad-skill-lab-worker-cn-northwest-1-t1")  # bug5
    assert matches("role/launchpad-agent-execution-role-cn-north-1-t1")  # named explicitly now
    # N17-obs131: the workspace bootstrap's gateway / CodeBuild roles, with and without EnvName
    for region in ("cn-northwest-1", "cn-north-1"):
        assert matches(f"role/launchpad-gateway-role-{region}")
        assert matches(f"role/launchpad-gateway-role-{region}-t1")
        assert matches(f"role/launchpad-codebuild-role-{region}")
        assert matches(f"role/launchpad-codebuild-role-{region}-t1")
    for other in ("role/launchpad-cn-ops-cn-northwest-1",
                  "role/launchpad-memory-admin-cn-northwest-1",
                  "role/launchpad-workspace-role",  # the spoke role of a cross-account ws
                  "role/cdk-hnb659fds-cfn-exec-role-111122223333-cn-northwest-1"):
        assert not matches(other) and not fnmatch.fnmatchcase(other, agent_glob)
    # every other UpdateAssumeRolePolicy grant in the role is one of these two
    granting = [s["Sid"] for s in _ops_role_statements()
                if "iam:UpdateAssumeRolePolicy" in _as_list(s.get("Action"))]
    assert granting == ["IamAdoptAgentRoles", "IamAdoptWorkspaceRoles"]


def test_N17_obs201_the_trust_refresh_statements_carry_the_resource_tag_conditions():
    """N17-obs201: the name patterns also match the hub's CDK-made twins
    (`launchpad-gateway-role-<region>`, `launchpad-agent-execution-role-<region>` — tagged
    `launchpad:managed` only, infra/stacks/base_stack.py); `workspace_iam.ensure_role` refuses an
    untagged role in code, and the statements now make IAM enforce the same tag: the per-agent
    statement requires `launchpad:agent-id` (what `agent_iam.ensure_role` tags in its CreateRole
    call), the workspace one `launchpad:workspace` (what `workspace_iam.ensure_role` tags in its
    CreateRole call — before any trust write). `StringLike "*"` = the key must exist, any value.
    The allowed / denied evaluation (tagged bootstrap roles allowed, CDK twins and bare roles
    denied) is `backend/tests/consistency/test_workspace_permissions.py`."""
    _broad, agent, workspace = _adopt_statements()
    assert agent["Condition"] == {"StringLike": {"iam:ResourceTag/launchpad:agent-id": "*"}}
    assert workspace["Condition"] == {"StringLike": {"iam:ResourceTag/launchpad:workspace": "*"}}
    # the Condition narrows the trust WRITE only — the adopt path's reads / tag / inline-policy
    # writes stay on the unconditional broad statement
    statements = _ops_role_statements()
    broad = next(s for s in statements if s.get("Sid") == "IamForLaunchpadRoles")
    assert "Condition" not in broad
    for action in ("iam:GetRole", "iam:TagRole", "iam:ListRoleTags", "iam:PutRolePolicy",
                   "iam:CreateRole"):
        assert action in _as_list(broad["Action"]), action


def test_ops_role_may_list_role_tags_for_role_adoption():
    # N11 r3-B (bug5): workspace_iam.ensure_role verifies the launchpad:workspace tag of a
    # role it adopts; the listing fallback must not be denied on the hub.
    template = _load()
    policies = template["Resources"]["InstanceRole"]["Properties"]["Policies"]
    statements = [s for p in policies for s in p["PolicyDocument"]["Statement"]]
    iam_stmt = next(s for s in statements if s.get("Sid") == "IamForLaunchpadRoles")
    assert "iam:ListRoleTags" in iam_stmt["Action"]
    assert "iam:TagRole" in iam_stmt["Action"]


def test_pip_extra_index_url_is_optional_and_reaches_the_host_env(monkeypatch):
    """N11-obs25 / N13-bug1: an optional secondary PyPI index (NJU is the uv-complete
    fallback) — empty by default, written to /etc/launchpad/env as PIP_EXTRA_INDEX_URL
    + UV_INDEX + LAUNCHPAD_PIP_EXTRA_INDEX_URL only when set; the primary stays tuna."""
    template = _load()
    param = template["Parameters"]["PipExtraIndexUrl"]
    assert param["Default"] == ""
    assert template["Parameters"]["PipIndexUrl"]["Default"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    stub = _user_data_text()
    assert "printf 'LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL=%q\\n' \"${PipExtraIndexUrl}\"" in stub
    text = _script_text()
    extra = "$LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL"
    assert f'if [ -n "{extra}" ]; then' in text
    assert f"printf 'PIP_EXTRA_INDEX_URL=%s\\n' \"{extra}\"" in text
    assert f"printf 'UV_INDEX=%s\\n' \"{extra}\"" in text
    assert f"printf 'LAUNCHPAD_PIP_EXTRA_INDEX_URL=%s\\n' \"{extra}\"" in text
    # an empty extra index must still be *declared* (`?`, not `:?`) so the body boots
    assert ': "${LAUNCHPAD_BOOT_PIP_EXTRA_INDEX_URL?}"' in text
    assert ': "${LAUNCHPAD_BOOT_ENV_NAME?}"' in text


def test_codebuild_pip_mirror_parameters_reach_the_backend_env_and_the_cdk_context():
    """N11-r3-bug7: the BUILD mirror is a separate parameter pair from the host mirror
    (`PipIndexUrl` stays tuna for the host's own uv/pip): `CodeBuildPipIndexUrl` (NJU,
    uv-complete) + `CodeBuildPipFallbackIndexUrl` (Tencent) are written to
    /etc/launchpad/env as the backend settings (spoke CodeBuild projects + the Skill Lab
    worker buildspec read them) AND ride LAUNCHPAD_CDK_CONTEXT so the hub project the host
    deploys with `cdk deploy` gets the same mirrors (scripts/bootstrap.py and
    scripts/host-update.sh both split the variable on commas)."""
    template = _load()
    params = template["Parameters"]
    assert params["CodeBuildPipIndexUrl"]["Default"] == "https://mirror.nju.edu.cn/pypi/web/simple"
    assert params["CodeBuildPipFallbackIndexUrl"]["Default"] == (
        "https://mirrors.cloud.tencent.com/pypi/simple"
    )
    assert params["PipIndexUrl"]["Default"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    stub = _user_data_text()
    assert ("printf 'LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL=%q\\n' "
            "\"${CodeBuildPipIndexUrl}\"") in stub
    assert ("printf 'LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL=%q\\n' "
            "\"${CodeBuildPipFallbackIndexUrl}\"") in stub
    text = _script_text()
    assert ("printf 'LAUNCHPAD_CODEBUILD_PIP_INDEX_URL=%s\\n' "
            "\"$LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL\"") in text
    assert ("printf 'LAUNCHPAD_CODEBUILD_PIP_FALLBACK_INDEX_URL=%s\\n' "
            "\"$LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL\"") in text
    # the host's own installs keep the host mirror
    assert "printf 'PIP_INDEX_URL=%s\\n' \"$LAUNCHPAD_BOOT_PIP_INDEX_URL\"" in text
    assert '--index-url "${PipIndexUrl}" awscli' in stub
    ctx_line = next(line for line in text.splitlines() if "LAUNCHPAD_CDK_CONTEXT=" in line)
    assert "codebuild_pip_index_url=%s" in ctx_line
    assert "codebuild_pip_fallback_index_url=%s" in ctx_line
    assert "$LAUNCHPAD_BOOT_CODEBUILD_PIP_INDEX_URL" in ctx_line
    assert "$LAUNCHPAD_BOOT_CODEBUILD_PIP_FALLBACK_INDEX_URL" in ctx_line


def test_user_data_runs_everything_that_writes_under_the_checkout_as_ubuntu():
    # N12-bug10: the backend unit runs as `ubuntu` and rewrites data/ and config/ on every
    # repair/redeploy, so whatever the first boot writes there must be ubuntu-owned —
    # `uv sync`, `npm install`, `cdk bootstrap`, `make bootstrap` and `npm run build` all
    # run inside the `runuser -u ubuntu -m` block, never as root (host-update.sh keeps the
    # same rule for every later update through `as_owner_env`).
    text = _script_text()
    start = text.index("runuser -u ubuntu -m -- bash -c '")
    end = text.index("\n)", start)
    block = text[start:end]
    for cmd in ("uv sync", "npm install", "cdk bootstrap", "make bootstrap", "npm run build"):
        assert cmd in block, cmd
    # N11-obs46: the build is stamped (as ubuntu, inside the same block) with the commit it
    # was built from, in exactly the form scripts/host-update.sh writes and reads
    # (`git rev-parse HEAD` = sha + newline; read back with `tr -d '[:space:]'`), so the
    # first host-update keeps dist/ instead of rebuilding for "no .launchpad-build stamp"
    stamp = (
        "git -C /home/ubuntu/launchpad rev-parse HEAD > "
        "/home/ubuntu/launchpad/frontend/dist/.launchpad-build"
    )
    assert stamp in block
    assert block.index("npm run build") < block.index(stamp)
    host_update = (Path(__file__).resolve().parents[2] / "scripts" / "host-update.sh").read_text(
        encoding="utf-8"
    )
    assert 'STAMP="frontend/dist/.launchpad-build"' in host_update
    assert "tr -d '[:space:]' <\"$STAMP\"" in host_update
    # ...and no root-run command after that block writes under the checkout (comments and
    # the unit's ExecStart line, which systemd runs as User=ubuntu, are not commands here)
    after = text[end:text.index("systemctl enable --now launchpad-backend")]
    commands = [
        line.strip() for line in after.splitlines()
        if line.strip() and not line.strip().startswith("#") and "ExecStart=" not in line
    ]
    writers = ("make bootstrap", "npm ", "uv sync", "uv run", "cdk ", "chown -R")
    assert [c for c in commands if c.startswith(writers)] == []


# ── N11-r5-bug1: UserData stub + infra/ec2/bootstrap-host.sh body ─────────────


def test_user_data_fits_the_ec2_limit_with_worst_case_parameter_values():
    # N11-r5-bug1 (live): `aws cloudformation deploy` -> "User data is limited to
    # 16384 bytes" — the inline bootstrap had reached 17,680 B raw. EC2 measures the
    # RAW (pre-base64) bytes, after CloudFormation's Fn::Sub. Render the stub with
    # every token at its longest legal value and keep a 512 B margin; and keep the
    # stub a stub — under half the limit — so the body cannot creep back in.
    rendered = _render_user_data(worst_case=True).encode("utf-8")
    assert len(rendered) < EC2_USER_DATA_LIMIT - USER_DATA_MARGIN, len(rendered)
    assert len(rendered) < EC2_USER_DATA_LIMIT // 2, (
        f"{len(rendered)} B: the stub is growing — move logic into bootstrap-host.sh"
    )
    # and the un-substituted template text itself, for the record
    assert len(_user_data_text().encode("utf-8")) < EC2_USER_DATA_LIMIT // 2


def test_user_data_stub_consumes_every_token_the_inline_bootstrap_did():
    # nothing a parameter used to feed may silently stop reaching the host
    text, variables = _user_data_sub()
    assert _sub_tokens(text) == EXPECTED_STUB_TOKENS
    assert set(variables) == {"UseTlsCookieFlag"}
    template = _load()
    for token in EXPECTED_STUB_TOKENS - {"AWS::Region", "AWS::StackName", "UseTlsCookieFlag"}:
        assert token in template["Parameters"], token


def test_user_data_stub_execs_the_repo_bootstrap_body_as_its_last_step():
    stub = _user_data_text()
    lines = [ln.strip() for ln in stub.splitlines() if ln.strip()]
    assert f"BODY={BODY_ON_HOST}" in lines
    assert lines[-1] == 'exec bash "$BODY"'
    # a checkout without the body (wrong GitBranch) fails the stack at once
    body_guard = next(ln for ln in lines if '[ -r "$BODY" ]' in ln)
    assert body_guard.startswith('[ -r "$BODY" ] ||') and "exit 1" in body_guard
    # the stub does none of the body's work
    for body_only in ("make bootstrap", "cdk bootstrap", "npm install", "uv sync", "nginx",
                      "launchpad-backend.service", "/etc/launchpad/env\n", "signal_stack SUCCESS"):
        assert body_only not in stub, body_only


def test_user_data_stub_signals_failure_on_any_exit_before_the_exec():
    # The inline script's ERR trap never fired for `[ ok ] || { ...; exit 1; }` paths
    # (the clone-failed exit), leaving CloudFormation to wait out PT60M. The stub's
    # EXIT trap covers set -e failures AND explicit exits; `exec` replaces the process
    # so a successful hand-off never fires it (the body signals from then on).
    stub = _user_data_text()
    lines = [ln.strip() for ln in stub.splitlines()]
    on_exit = 'on_exit() { rc=$?; [ "$rc" -eq 0 ] || signal_stack FAILURE || true; }'
    assert on_exit in lines
    assert lines.index("signal_stack() {") < lines.index(on_exit) < lines.index("trap on_exit EXIT")
    assert lines.index("trap on_exit EXIT") < lines.index("umask 077")  # armed before any real work
    assert "trap - EXIT" not in stub  # never disarmed: an exec failure must still signal
    assert "trap 'signal_stack FAILURE || true' ERR" not in stub  # the body keeps the ERR form
    # signal-resource targets this template's own Instance with the pseudo parameters
    signal = stub[stub.index("signal_stack() {"):stub.index("on_exit()")]
    assert '--stack-name "${AWS::StackName}"' in signal
    assert "--logical-resource-id Instance" in signal
    assert '--region "${AWS::Region}"' in signal


def test_stub_writes_exactly_the_bootstrap_env_keys_the_body_declares():
    # The contract: every LAUNCHPAD_BOOT_* the body reads is written by the stub and
    # nothing is written that the body never reads (no missing, no unused).
    written = _bootstrap_env_keys_written()
    declared = _bootstrap_env_keys_declared()
    assert written == declared, {
        "missing (body reads, stub never writes)": sorted(declared - written),
        "unused (stub writes, body never declares)": sorted(written - declared),
    }
    assert len(written) >= 13
    # ...and every LAUNCHPAD_BOOT_* reference anywhere in the body is a declared key
    referenced = set(re.findall(r"\$\{?(LAUNCHPAD_BOOT_[A-Z0-9_]+)", _script_text()))
    assert referenced <= declared, sorted(referenced - declared)
    # the body actually uses each declared key (a declaration alone is dead contract)
    used = set(re.findall(r"\$\{?(LAUNCHPAD_BOOT_[A-Z0-9_]+)\b(?!:?\?)", _script_text()))
    assert used == declared, sorted(declared ^ used)
    # the body refuses to run without the file and sources it before the contract block
    body = [ln.strip() for ln in _script_text().splitlines()]
    assert f"BOOTSTRAP_ENV={BOOTSTRAP_ENV}" in body
    assert body.index('. "$BOOTSTRAP_ENV"') < min(
        i for i, ln in enumerate(body) if ln.startswith(': "${LAUNCHPAD_BOOT_')
    )


def test_bootstrap_env_carries_parameter_values_only_never_a_secret():
    # spec C4: bootstrap.env holds the SSM parameter *names* and the plain template
    # values — every line is a printf of a literal or of `${Token}`s; no command
    # substitution (nothing read from SSM/IMDS lands there), and the three secret
    # parameters appear only by name.
    block = _bootstrap_env_block(_user_data_text())
    assert block
    template = _load()
    for line in block:
        assert "$(" not in line, f"command substitution in bootstrap.env: {line!r}"
        assert "aws " not in line, line
        _key, _, value = line.strip().partition("=")
        tokens = _sub_tokens(value)
        bare = re.sub(r"\$\{[^}]*\}", "", value)
        assert "$" not in bare, f"a shell variable, not a template token: {line!r}"
        for token in tokens:
            assert token.startswith("AWS::") or token == "UseTlsCookieFlag" or (
                token in template["Parameters"]
            ), token
    joined = "\n".join(block)
    for name_param in ("GlobalBedrockApiKeyParameter", "ConsolePasswordParameter",
                       "SiliconFlowApiKeyParameter"):
        assert f"${{{name_param}}}" in joined  # the NAME travels...
    assert "--with-decryption" not in joined  # ...the value never does
    # the deploy key is the stub's own business: fetched after bootstrap.env is closed
    # and written only to the 0600 key file, never into the env file
    assert "GitDeployKeyParameter" not in joined
    assert "LAUNCHPAD_BOOT_SIGNAL_RESOURCE=Instance" in joined  # the Instance logical id


def test_bootstrap_host_script_has_no_template_substitution_tokens():
    # The body is plain bash from the checkout — CloudFormation never sees it, so a
    # leftover `${Param}` / `${AWS::X}` / `${!Escape}` would be a silent empty string.
    text = _script_text()
    template = _load()
    forbidden = set(template["Parameters"]) | {"UseTlsCookieFlag"}
    for token in _sub_tokens(text):
        assert not token.startswith("AWS::"), token
        assert token not in forbidden, token
    assert "${!" not in text
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert "Fn::" not in code and "!Ref" not in code and "!Sub" not in code


def test_bootstrap_host_script_is_executable_bash_with_strict_mode_and_logging():
    assert SCRIPT_PATH.stat().st_mode & 0o111, "bootstrap-host.sh must be executable"
    text = _script_text()
    assert text.startswith("#!/bin/bash\n")
    assert "set -Eeuo pipefail" in text
    assert "set -x" not in text.replace("No `set -x`", "")
    # logs where the operator looks, appending to the stub's log when run by hand;
    # the stub marks the descriptors as already attached so nothing is logged twice
    assert "tee -a /var/log/launchpad-bootstrap.log" in text
    assert 'if [ -z "${LAUNCHPAD_BOOTSTRAP_LOG_ATTACHED:-}" ]; then' in text
    assert "export LAUNCHPAD_BOOTSTRAP_LOG_ATTACHED=1" in _user_data_text()
    assert "tee /var/log/launchpad-bootstrap.log" in _user_data_text()
    # the body ends exactly as the inline script did
    tail = [ln.strip() for ln in text.strip().splitlines()][-2:]
    assert tail == ["trap - ERR", "signal_stack SUCCESS"]


def test_bootstrap_host_script_and_rendered_stub_are_shellcheck_clean(tmp_path):
    import pytest

    if shutil.which("shellcheck") is None:
        pytest.skip(
            "shellcheck is not installed — install it (apt install shellcheck) to lint "
            "infra/ec2/bootstrap-host.sh and the rendered UserData stub"
        )
    body = subprocess.run(
        ["shellcheck", str(SCRIPT_PATH)], capture_output=True, text=True, check=False
    )
    assert body.returncode == 0, body.stdout + body.stderr
    stub_file = tmp_path / "user-data.sh"
    stub_file.write_text(_render_user_data(worst_case=False), encoding="utf-8")
    stub = subprocess.run(
        ["shellcheck", str(stub_file)], capture_output=True, text=True, check=False
    )
    assert stub.returncode == 0, stub.stdout + stub.stderr


def test_host_update_script_does_not_depend_on_the_moved_bootstrap_body():
    # scripts/host-update.sh is the in-place update path; it must keep working on
    # hosts that booted from the inline UserData (no bootstrap.env, no body file).
    host_update = (Path(__file__).resolve().parents[2] / "scripts" / "host-update.sh").read_text(
        encoding="utf-8"
    )
    code = "\n".join(
        ln.split("#", 1)[0] for ln in host_update.splitlines() if not ln.lstrip().startswith("#")
    )
    assert "bootstrap.env" not in code
    assert "bootstrap-host.sh" not in code  # prose may name it; no code path reads it
    assert "LAUNCHPAD_BOOT_" not in code
    assert os.path.isfile(SCRIPT_PATH)


# ─── N11-obs47: Elastic IP ─────────────────────────────────────────────────────────────
# A stop/start (and the in-place UserData update path, deploy.md §8.2) re-assigned the
# auto-assigned public IPv4 of launchpad-cn-ec2-t1 (203.0.113.50 -> 203.0.113.40) and the
# console address plus /etc/launchpad/env's LAUNCHPAD_PUBLIC_BASE_URL went stale. User
# ruling 2026-10-14 (#2): both hosts get an Elastic IP.


def _eip_resources() -> tuple[dict, dict, dict]:
    template = _load()
    [eip] = _resources_of_type(template, "AWS::EC2::EIP")
    [assoc] = _resources_of_type(template, "AWS::EC2::EIPAssociation")
    return template, eip, assoc


def test_allocate_elastic_ip_parameter_defaults_on_and_drives_one_condition():
    template = _load()
    param = template["Parameters"]["AllocateElasticIp"]
    assert param["Type"] == "String"
    assert param["Default"] == "true", "new stacks get a fixed address by default (ruling #2)"
    assert param["AllowedValues"] == ["true", "false"]
    assert template["Conditions"]["UseElasticIp"] == {
        "Fn::Equals": [{"Ref": "AllocateElasticIp"}, "true"]
    }


def test_elastic_ip_and_its_association_exist_only_under_the_condition():
    _template, eip, assoc = _eip_resources()
    assert eip["Condition"] == "UseElasticIp"
    assert assoc["Condition"] == "UseElasticIp"
    assert eip["Properties"]["Domain"] == "vpc"
    # a VPC EIP must not be allocated before the VPC has its internet gateway
    assert eip["DependsOn"] == "InternetGatewayAttachment"
    allocation = json.dumps(assoc["Properties"]["AllocationId"])
    assert "ElasticIp" in allocation and "AllocationId" in allocation
    assert assoc["Properties"]["InstanceId"] == {"Ref": "Instance"}
    # the EIP is named like every other fixed resource (EnvName-suffixed)
    assert eip["Properties"]["Tags"][0]["Key"] == "Name"
    assert _resolve(eip["Properties"]["Tags"][0]["Value"], "t1") == "launchpad-cn-ops-t1"
    assert _resolve(eip["Properties"]["Tags"][0]["Value"], "") == "launchpad-cn-ops"


def test_enabling_the_elastic_ip_on_an_existing_stack_never_touches_the_instance():
    # The association is its own resource, so an UPDATE that flips AllocateElasticIp adds or
    # removes ElasticIp + ElasticIpAssociation and nothing else — the AWS::EC2::Instance
    # must neither reference the EIP nor be referenced from it (EIP.InstanceId would make
    # the EIP resource depend on the instance's lifecycle and is the documented
    # replacement-prone shape).
    template, eip, _assoc = _eip_resources()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    assert "ElasticIp" not in json.dumps(instance)
    assert "InstanceId" not in eip["Properties"]
    # the NIC still auto-assigns a public IPv4: the first boot needs egress (apt, GitHub,
    # cfn-signal) before the association exists, and the EIP simply supersedes it
    assert instance["Properties"]["NetworkInterfaces"][0]["AssociatePublicIpAddress"] is True


def test_public_ip_and_console_url_outputs_prefer_the_elastic_ip():
    template = _load()
    outputs = template["Outputs"]
    public_ip = outputs["PublicIp"]["Value"]["Fn::If"]
    assert public_ip[0] == "UseElasticIp"
    assert public_ip[1] == {"Ref": "ElasticIp"}
    assert "Instance" in json.dumps(public_ip[2]) and "PublicIp" in json.dumps(public_ip[2])
    # ConsoleUrl: scheme from UseTls, host from UseElasticIp — both branches
    console = outputs["ConsoleUrl"]["Value"]["Fn::If"]
    assert console[0] == "UseTls"
    for branch, scheme in ((console[1], "https"), (console[2], "http")):
        text, variables = branch["Fn::Sub"]
        assert text == f"{scheme}://${{Host}}:${{ConsolePort}}"
        assert variables["Host"]["Fn::If"][0] == "UseElasticIp"
        assert variables["Host"]["Fn::If"][1] == {"Ref": "ElasticIp"}
    # the allocation id is exported only when there is one
    assert outputs["ElasticIpAllocationId"]["Condition"] == "UseElasticIp"
    assert "ElasticIp" in json.dumps(outputs["ElasticIpAllocationId"]["Value"])
    # never a hard-coded address anywhere in the outputs
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", json.dumps(outputs))


def test_allocate_elastic_ip_is_not_a_user_data_concern():
    # The host learns its address from IMDSv2 (bootstrap-host.sh on first boot,
    # host-update.sh step 12b afterwards) — the parameter never reaches the stub, so the
    # 16 KB budget and the bootstrap.env key parity are unaffected.
    assert "AllocateElasticIp" not in _sub_tokens(_user_data_text())
    assert "AllocateElasticIp" not in _both_texts()


# ─── N11-obs48b: pin the AMI ───────────────────────────────────────────────────────────
# Live (deploy-ops, t1 change set 2026-10-14): ImageId was the dynamic reference
# `{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/
# ebs-gp3/ami-id}}`,
# which CloudFormation re-resolves on EVERY stack update — so every change set marked the
# Instance "Replacement: Conditional", and the first newer Canonical AMI would have turned
# any unrelated update (the Elastic IP add, an IAM statement) into an instance REPLACEMENT:
# new host, local SQLite ledger and /etc/launchpad gone. Fix: `AmiId` parameter (empty =
# resolve the latest at create time only) + `UseLatestAmi` condition; every update passes
# the running AMI (deploy.md §3 "AMI pin", §8.2). The lead filed this as N11-obs48; that id
# was already the Task-6 closure row, hence obs48b.

LATEST_AMI_LOOKUP = (
    "{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current"
    "/arm64/hvm/ebs-gp3/ami-id}}"
)


def _latest_ami_branch(image_id) -> str:
    """The create-time (`UseLatestAmi` true) branch of the Instance's ImageId."""
    cond, when_latest, _when_pinned = image_id["Fn::If"]
    assert cond == "UseLatestAmi"
    return when_latest


def _render_image_id(ami_id: str) -> str:
    """Evaluate `ImageId: !If [UseLatestAmi, <lookup>, !Ref AmiId]` for one AmiId value,
    honouring the condition's own definition — a two-branch evaluator, not an emulator."""
    template = _load()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    cond_name, when_true, when_false = instance["Properties"]["ImageId"]["Fn::If"]
    lhs, rhs = template["Conditions"][cond_name]["Fn::Equals"]
    assert lhs == {"Ref": "AmiId"}
    chosen = when_true if ami_id == rhs else when_false
    if isinstance(chosen, dict):
        assert chosen == {"Ref": "AmiId"}
        chosen = ami_id
    return chosen


def _strings_containing(value, needle: str, path: tuple = ()) -> list[tuple]:
    """Every (path, string) in a parsed template whose string contains `needle`."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            found += _strings_containing(child, needle, path + (key,))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            found += _strings_containing(child, needle, path + (idx,))
    elif isinstance(value, str) and needle in value:
        found.append((path, value))
    return found


def test_ami_id_parameter_is_optional_and_accepts_only_an_ami_id_or_empty():
    template = _load()
    param = template["Parameters"]["AmiId"]
    assert param["Type"] == "String"  # AWS::EC2::Image::Id cannot default to ""
    assert param["Default"] == ""
    pattern = re.compile(param["AllowedPattern"])
    for ok in ("", "ami-12345678", "ami-0123456789abcdef0", "ami-0fedcba9876543210"):
        assert pattern.fullmatch(ok), ok
    for bad in (
        "latest", "ami-", "ami-1234567", "ami-" + "0" * 18, "ami-0123456789ABCDEF0",
        "ami-0g3b2733686736f80", " ami-12345678", "ami-12345678 ", "AMI-12345678",
        LATEST_AMI_LOOKUP,
    ):
        assert not pattern.fullmatch(bad), bad
    # the hazard is spelled out where the operator reads the parameter
    description = " ".join(param["Description"].split())
    assert "empty = resolve the latest Canonical Ubuntu 24.04 arm64 AMI at create time only" in (
        description
    )
    assert "ALWAYS pass the running AMI on updates" in description
    assert "ConstraintDescription" in param


def test_use_latest_ami_condition_is_exactly_ami_id_empty():
    template = _load()
    assert template["Conditions"]["UseLatestAmi"] == {"Fn::Equals": [{"Ref": "AmiId"}, ""]}


def test_image_id_pins_the_parameter_and_only_falls_back_to_the_canonical_lookup():
    template = _load()
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    image_id = instance["Properties"]["ImageId"]
    assert list(image_id) == ["Fn::If"], "ImageId must be the UseLatestAmi If, nothing else"
    cond, when_latest, when_pinned = image_id["Fn::If"]
    assert cond == "UseLatestAmi"
    assert when_latest == LATEST_AMI_LOOKUP
    assert when_pinned == {"Ref": "AmiId"}


def test_rendering_with_a_pinned_ami_leaves_no_dynamic_reference_in_image_id():
    rendered = _render_image_id("ami-0fedcba9876543210")
    assert rendered == "ami-0fedcba9876543210"
    assert "{{resolve:" not in rendered


def test_rendering_with_an_empty_ami_id_keeps_the_latest_lookup():
    assert _render_image_id("") == LATEST_AMI_LOOKUP


def test_the_only_dynamic_reference_in_the_template_is_the_use_latest_ami_branch():
    # Any other `{{resolve:ssm:...}}` (or secretsmanager) reference would be re-resolved
    # on every update and bring the same replacement hazard back through another door.
    template = _load()
    hits = _strings_containing(template, "{{resolve:")
    assert [path for path, _ in hits] == [
        ("Resources", "Instance", "Properties", "ImageId", "Fn::If", 1)
    ], hits
    [instance] = _resources_of_type(template, "AWS::EC2::Instance")
    assert json.dumps(instance).count("{{resolve:") == 1


def test_ami_id_is_not_a_user_data_concern():
    # The pin is a CloudFormation-side property; it never reaches the stub or the body,
    # so the 16 KB budget and the bootstrap.env key parity are unaffected.
    assert "AmiId" not in _sub_tokens(_user_data_text())
    assert "AmiId" not in _both_texts()


def test_deploy_doc_documents_the_ami_pin_procedure():
    deploy = (Path(__file__).resolve().parents[2] / "docs" / "cn" / "interfaces" / "deploy.md")
    deploy = deploy.read_text(encoding="utf-8")
    # the operator command that reads the running AMI, and the rule that every update
    # passes it; a change set with an Instance replacement row is never executed
    assert "aws ec2 describe-instances --instance-ids" in deploy
    assert "'Reservations[0].Instances[0].ImageId'" in deploy
    assert "AmiId=<running-ami>" in deploy or "AmiId=<running AMI>" in deploy
    assert "N11-obs48b" in deploy
    section_8_2 = deploy[deploy.index("### 8.2 Template path"):deploy.index("### 8.3 ")]
    assert "AmiId" in section_8_2
    assert "must never be executed" in section_8_2
    section_3 = deploy[deploy.index("## 3. EC2 stack parameters"):deploy.index("### 3.1 ")]
    assert "AmiId" in section_3


def test_template_is_cfn_lint_clean():
    import pytest

    # Same tool and skip rule as scripts/verify.sh: cfn-lint runs from uv's tool cache.
    if shutil.which("uvx") is None:
        pytest.skip("uvx is not on PATH — install uv to lint infra/ec2/*.yaml")
    probe = subprocess.run(
        ["uvx", "cfn-lint", "--version"], capture_output=True, text=True, check=False
    )
    if probe.returncode != 0:
        pytest.skip("uvx could not provide cfn-lint — run `uvx cfn-lint --version` once")
    result = subprocess.run(
        ["uvx", "cfn-lint", str(TEMPLATE_PATH)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_iam_statement_grants_the_registry_api_key_pattern():
    """D21 (M1-b): the model registry reads every provider's `launchpad-<name>-api-key`
    SecureString itself (`aws_clients.ssm_client` → `GetParameter`), so the ops role's
    `ReadLaunchpadSecretParameters` statement carries the pattern next to the two explicit
    parameters — partition-safe, region/account-bound, `ssm:GetParameter` only."""
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [ssm_statement] = [s for s in statements if s.get("Sid") == "ReadLaunchpadSecretParameters"]
    assert ssm_statement["Action"] == "ssm:GetParameter"
    subs = [r["Fn::Sub"] for r in ssm_statement["Resource"]
            if isinstance(r, dict) and "Fn::Sub" in r]
    [pattern] = [s for s in subs if s.endswith(":parameter/launchpad-*-api-key")]
    assert pattern == (
        "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/launchpad-*-api-key"
    )
    # the explicit parameters are still granted (the host's boot path reads them)
    joined = str(ssm_statement["Resource"])
    assert "GlobalBedrockApiKeyParameter" in joined and "ConsolePasswordParameter" in joined
    # the pattern matches exactly the names the registry accepts (model_credential.SSM_PARAMETER_RE)
    assert re.fullmatch(r"launchpad-[a-z0-9-]+-api-key", "launchpad-siliconflow-api-key")
    assert not re.fullmatch(r"launchpad-[a-z0-9-]+-api-key", "launchpad-console-password")


def test_iam_statement_lets_the_console_host_write_and_delete_provider_key_parameters():
    """N17-obs203 §3: a model-provider API key typed in the console is written to its SSM
    SecureString by the console host and deleted with the provider — `SsmWriteLaunchpadSecret
    Parameters` grants PutParameter / DeleteParameter / AddTagsToResource on exactly the
    `launchpad-*-api-key` family (partition-safe, region/account-bound), nothing wider, and no
    KMS twin (the default `alias/aws/ssm` key needs none — the read statement relies on that
    already)."""
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]
    [write] = [s for s in statements if s.get("Sid") == "SsmWriteLaunchpadSecretParameters"]
    assert write["Effect"] == "Allow"
    assert sorted(write["Action"]) == ["ssm:AddTagsToResource", "ssm:DeleteParameter",
                                       "ssm:PutParameter"]
    assert write["Resource"] == [{"Fn::Sub": (
        "arn:${AWS::Partition}:ssm:${AWS::Region}:${AWS::AccountId}:parameter/launchpad-*-api-key"
    )}]
    # the boot-path parameters (console password, Global Bedrock key) are NOT writable: their
    # names do not match the pattern, and no other statement grants ssm:PutParameter on them
    for name in ("launchpad-console-password", "launchpad-global-bedrock-key"):
        assert not re.fullmatch(r"launchpad-.*-api-key", name)
    writers = [s for s in statements if s.get("Effect") == "Allow"
               and "ssm:PutParameter" in (s["Action"] if isinstance(s["Action"], list)
                                          else [s["Action"]])]
    assert {s["Sid"] for s in writers} == {"SsmWriteLaunchpadSecretParameters",
                                           "SsmForCdkBootstrapVersion"}
    assert not any("kms:" in a for s in statements for a in (
        s["Action"] if isinstance(s["Action"], list) else [s["Action"]]))


def test_ops_role_can_read_the_evidence_bucket_without_a_partition_literal():
    # N13-obs11 (r13-e): demo-video media are staged onto the host from the evidence
    # bucket `launchpad-cn-evidence-<account>` with the instance role (`aws s3 cp` on the
    # host, deploy.md §8.8). The role's CDK-bootstrap grant (Sid CdkAndStackLifecycle,
    # `s3:*` on `*`) already covers s3:GetObject / s3:ListBucket there, so no scoped
    # statement was added — this test pins that *some* Allow statement keeps covering the
    # bucket, and that any bucket-specific ARN is written with ${AWS::Partition}. Narrowing
    # the wide grant later must come with `Sid: EvidenceBucketRead`.
    template = _load()
    [role] = _resources_of_type(template, "AWS::IAM::Role")
    [policy] = role["Properties"]["Policies"]
    statements = policy["PolicyDocument"]["Statement"]

    def actions(statement: dict) -> set[str]:
        action = statement["Action"]
        return {action} if isinstance(action, str) else set(action)

    def resources(statement: dict) -> list:
        resource = statement["Resource"]
        return [resource] if not isinstance(resource, list) else resource

    def covers_bucket(resource) -> bool:
        if resource == "*":
            return True
        text = json.dumps(resource)
        return "launchpad-cn-evidence" in text and "AWS::Partition" in text

    readers = [
        s for s in statements
        if s["Effect"] == "Allow"
        and actions(s) & {"s3:*", "s3:Get*", "s3:GetObject"}
        and any(covers_bucket(r) for r in resources(s))
    ]
    assert readers, "no Allow statement grants s3:GetObject on launchpad-cn-evidence-<account>"
    for statement in readers:
        assert "arn:aws:" not in json.dumps(statement), statement.get("Sid")


def test_bootstrap_pins_the_unattended_upgrades_reboot_window():
    # N13-obs13: unattended-upgrades rebooted the console hosts at 02:04Z (10:04 Beijing, inside
    # recording hours). The bootstrap body writes a drop-in sorted after 50unattended-upgrades
    # that keeps automatic reboots but moves them to 20:00 UTC (04:00 Beijing) and never with a
    # user logged in. Idempotent: a plain `cat >` of fixed content, no append.
    body = _script_text()
    start = body.index("cat >/etc/apt/apt.conf.d/51-launchpad-upgrades <<'APT'")
    block = body[start: body.index("\nAPT\n", start)]
    for line in (
        'Unattended-Upgrade::Automatic-Reboot "true";',
        'Unattended-Upgrade::Automatic-Reboot-Time "20:00";',
        'Unattended-Upgrade::Automatic-Reboot-WithUsers "false";',
    ):
        assert line in block, line
    assert ">>/etc/apt/apt.conf.d/51-launchpad-upgrades" not in body          # never appended
    assert "chmod 0644 /etc/apt/apt.conf.d/51-launchpad-upgrades" in body
