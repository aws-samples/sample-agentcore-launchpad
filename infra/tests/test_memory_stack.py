"""The self-hosted Memory/KB substrate stacks (N6), split by D17 into the shared
`launchpad-memory-domain` (OpenSearch domain + FGAC master, one per account+region) and
the per-environment `launchpad-memory[-env]` (queue, extractor Lambda, IAM)."""

import ast
import json
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.base_stack import LaunchpadBaseStack
from stacks.memory_domain_stack import DOMAIN_STACK_NAME, LaunchpadMemoryDomainStack
from stacks.memory_stack import (
    DEFAULT_EXTRACT_MODEL_READ_TIMEOUT_SECONDS,
    LAMBDA_TIMEOUT_SECONDS,
    QUEUE_VISIBILITY_TIMEOUT_SECONDS,
    LaunchpadMemoryStack,
)
from stacks.naming import memory_domain_env, suffixed

ACCOUNT = "111111111111"


def test_extractor_transport_budget_fits_lambda_and_queue_visibility():
    """Cross-layer pin: increasing read budgets cannot leave CDK's deadline behind."""
    source = Path(__file__).resolve().parents[2] / "backend/lambdas/memory_extractor/handler.py"
    names = {"MODEL_CONNECT_TIMEOUT_S", "MODEL_READ_TIMEOUT_S", "MODEL_FINISH_RESERVE_S"}
    constants = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in names
    }
    assert constants["MODEL_READ_TIMEOUT_S"] == DEFAULT_EXTRACT_MODEL_READ_TIMEOUT_SECONDS
    assert 3 * (
        constants["MODEL_CONNECT_TIMEOUT_S"] + constants["MODEL_READ_TIMEOUT_S"]
    ) + constants["MODEL_FINISH_RESERVE_S"] <= LAMBDA_TIMEOUT_SECONDS
    assert LAMBDA_TIMEOUT_SECONDS == 480
    assert QUEUE_VISIBILITY_TIMEOUT_SECONDS == LAMBDA_TIMEOUT_SECONDS * 6


@pytest.mark.parametrize("seconds", [1, 90, 120])
def test_model_read_timeout_is_configurable_without_extending_lambda(seconds):
    template, _, _ = _synth({"extract_model_read_timeout_seconds": str(seconds)})
    template.has_resource_properties("AWS::Lambda::Function", {
        "Timeout": 480,
        "Environment": {"Variables": {
            "LAUNCHPAD_EXTRACT_MODEL_READ_TIMEOUT_SECONDS": str(float(seconds)),
        }},
    })
    template.has_resource_properties("AWS::SQS::Queue", {"VisibilityTimeout": 2880})


@pytest.mark.parametrize("value", ["0", "-1", "121", "nan", "inf", "bad"])
def test_model_read_timeout_rejects_invalid_or_unbudgeted_values(value):
    with pytest.raises(ValueError):
        _synth({"extract_model_read_timeout_seconds": value})


def _stacks(context=None, region="cn-northwest-1"):
    context = dict(context or {})
    env_name = context.get("env_name", "")
    app = cdk.App(context=context)
    env = cdk.Environment(account=ACCOUNT, region=region)
    base = LaunchpadBaseStack(app, suffixed("launchpad-base", env_name), env=env)
    domain = LaunchpadMemoryDomainStack(
        app, suffixed(DOMAIN_STACK_NAME, memory_domain_env(app.node)), env=env,
    )
    mem = LaunchpadMemoryStack(app, suffixed("launchpad-memory", env_name), base=base, env=env)
    mem.add_dependency(domain)
    return base, domain, mem


def _synth(context=None, region="cn-northwest-1"):
    """(per-env memory template, base template, domain template)."""
    base, domain, mem = _stacks(context, region)
    return Template.from_stack(mem), Template.from_stack(base), Template.from_stack(domain)


@pytest.fixture(scope="module")
def template():
    return _synth()[0]


@pytest.fixture(scope="module")
def domain_template():
    return _synth()[2]


# ── the shared domain stack (D17) ─────────────────────────────────────────────


def test_domain_is_single_small_node_private_and_fgac(domain_template: Template):
    template = domain_template
    template.resource_count_is("AWS::OpenSearchService::Domain", 1)
    template.has_resource_properties(
        "AWS::OpenSearchService::Domain",
        {
            "DomainName": "lp-memory-cn-northwest-1",
            "ClusterConfig": Match.object_like(
                {
                    "InstanceType": "t3.small.search",
                    "InstanceCount": 1,
                    "DedicatedMasterEnabled": False,
                    "ZoneAwarenessEnabled": False,
                }
            ),
            "EBSOptions": Match.object_like(
                {"EBSEnabled": True, "VolumeType": "gp3", "VolumeSize": 10}
            ),
            "EncryptionAtRestOptions": Match.object_like({"Enabled": True}),
            "NodeToNodeEncryptionOptions": {"Enabled": True},
            "DomainEndpointOptions": Match.object_like({"EnforceHTTPS": True}),
            "AdvancedSecurityOptions": Match.object_like(
                {
                    "Enabled": True,
                    "InternalUserDatabaseEnabled": False,
                    "MasterUserOptions": {"MasterUserARN": Match.any_value()},
                }
            ),
        },
    )
    (domain,) = template.find_resources("AWS::OpenSearchService::Domain").values()
    assert domain.get("UpdateReplacePolicy") == "Delete"
    assert domain.get("DeletionPolicy") == "Delete"
    # CDK's Domain.add_access_policies() applies the policy via a
    # Custom::OpenSearchAccessPolicy (an AwsCustomResource calling
    # UpdateDomainConfig) rather than a plain `AccessPolicies` CFN property —
    # inspect the literal JSON string parts of its "Create" call instead.
    (policy_cr,) = template.find_resources("Custom::OpenSearchAccessPolicy").values()
    # The policy document is doubly JSON-encoded (an AwsCustomResource SDK-call
    # payload whose "AccessPolicies" value is itself a JSON string), so every
    # quote inside it is backslash-escaped once.
    literal_parts = "".join(
        p for p in policy_cr["Properties"]["Create"]["Fn::Join"][1] if isinstance(p, str)
    )
    assert '\\"AWS\\":\\"*\\"' not in literal_parts
    assert '\\"Principal\\":\\"*\\"' not in literal_parts
    assert '\\"Action\\":\\"es:ESHttp*\\"' in literal_parts
    assert '\\"Effect\\":\\"Allow\\"' in literal_parts
    assert ":iam::111111111111:root" in literal_parts  # account root principal, never "*"


def test_admin_role_is_the_master_and_trusts_only_this_account(domain_template: Template):
    """D17: the FGAC master is always the stack-owned admin role — no `memory_master_arn`
    context any more, since a master bound to one environment's ops role would leave the
    shared domain master-less when that environment is torn down."""
    template = domain_template
    template.has_resource_properties(
        "AWS::IAM::Role", Match.object_like({"RoleName": "launchpad-memory-admin-cn-northwest-1"})
    )
    (role,) = [
        r
        for r in template.find_resources("AWS::IAM::Role").values()
        if r["Properties"].get("RoleName") == "launchpad-memory-admin-cn-northwest-1"
    ]
    assert ":iam::111111111111:root" in str(role["Properties"]["AssumeRolePolicyDocument"])
    (domain,) = template.find_resources("AWS::OpenSearchService::Domain").values()
    master = domain["Properties"]["AdvancedSecurityOptions"]["MasterUserOptions"]["MasterUserARN"]
    assert "Fn::GetAtt" in master and master["Fn::GetAtt"][1] == "Arn"
    assert master["Fn::GetAtt"][0].startswith("MemoryAdminRole")
    # the pre-D17 context is gone, not ignored silently: it changes nothing
    _, _, with_ctx = _synth({"memory_master_arn": "arn:aws-cn:iam::111111111111:role/ops"})
    assert with_ctx.to_json() == template.to_json()


def test_domain_stack_is_never_env_suffixed_and_has_no_exports():
    """The whole point of D17: `-c env_name=t1` must leave the shared stack byte-identical
    (same stack id, same domain, same admin role, same SSM parameters) so a second
    environment finds it rather than creating its own; and nothing is exported, so no
    CloudFormation import couples an environment stack to it."""
    _, default_domain, _ = _stacks()
    _, t1_domain, _ = _stacks({"env_name": "t1"})
    assert default_domain.stack_name == t1_domain.stack_name == "launchpad-memory-domain"
    assert Template.from_stack(default_domain).to_json() == Template.from_stack(t1_domain).to_json()
    body = Template.from_stack(default_domain).to_json()
    assert not any("Export" in out for out in body.get("Outputs", {}).values())
    assert "Fn::ImportValue" not in json.dumps(body)
    names = {
        r["Properties"].get("DomainName") or r["Properties"].get("RoleName")
        for r in body["Resources"].values()
        if r["Type"] in ("AWS::OpenSearchService::Domain", "AWS::IAM::Role")
        and (r["Properties"].get("DomainName") or r["Properties"].get("RoleName"))
    }
    assert names == {"lp-memory-cn-northwest-1", "launchpad-memory-admin-cn-northwest-1"}


def test_dedicated_domain_has_disjoint_names_and_discovery():
    _, domain, memory = _stacks({"env_name": "r28", "memory_domain_mode": "dedicated"})
    assert domain.stack_name == "launchpad-memory-domain-r28"
    dt, mt = Template.from_stack(domain), Template.from_stack(memory)
    dt.has_resource_properties("AWS::OpenSearchService::Domain", {
        "DomainName": "lp-memory-cn-northwest-1-r28",
    })
    dt.has_resource_properties("AWS::IAM::Role", {
        "RoleName": "launchpad-memory-admin-cn-northwest-1-r28",
    })
    parameters = dt.find_resources("AWS::SSM::Parameter").values()
    assert {r["Properties"]["Name"] for r in parameters} == {
        f"/launchpad/memory-domain/cn-northwest-1/r28/{key}"
        for key in ("endpoint", "arn", "admin-role-arn")
    }
    rendered = json.dumps(mt.to_json())
    assert "/launchpad/memory-domain/cn-northwest-1/r28/endpoint" in rendered
    assert "/launchpad/memory-domain/cn-northwest-1/endpoint" not in rendered
    assert "lp-memory-cn-northwest-1-r28" in rendered
    assert "launchpad-memory-admin-cn-northwest-1-r28" in rendered
    mt.resource_count_is("AWS::OpenSearchService::Domain", 0)


@pytest.mark.parametrize("context", [
    {"memory_domain_mode": "dedicated"}, {"memory_domain_mode": "typo"},
])
def test_invalid_domain_mode_refused_before_synthesis(context):
    with pytest.raises(ValueError):
        _stacks(context)


def test_domain_stack_publishes_endpoint_arn_and_admin_role_to_ssm(domain_template: Template):
    template = domain_template
    template.resource_count_is("AWS::SSM::Parameter", 3)
    params = {
        r["Properties"]["Name"]: r["Properties"]
        for r in template.find_resources("AWS::SSM::Parameter").values()
    }
    assert set(params) == {
        "/launchpad/memory-domain/cn-northwest-1/endpoint",
        "/launchpad/memory-domain/cn-northwest-1/arn",
        "/launchpad/memory-domain/cn-northwest-1/admin-role-arn",
    }
    endpoint = params["/launchpad/memory-domain/cn-northwest-1/endpoint"]["Value"]
    assert endpoint["Fn::GetAtt"][1] == "DomainEndpoint"
    for name in (
        "MemoryDomainEndpoint", "MemoryDomainArn", "MemoryDomainName", "MemoryAdminRoleArn",
    ):
        template.has_output(name, Match.any_value())


# ── the per-environment stack ─────────────────────────────────────────────────


def test_environment_stack_owns_no_domain_and_resolves_the_endpoint_from_ssm(template: Template):
    template.resource_count_is("AWS::OpenSearchService::Domain", 0)
    template.resource_count_is("Custom::OpenSearchAccessPolicy", 0)
    roles = {
        r["Properties"].get("RoleName") for r in template.find_resources("AWS::IAM::Role").values()
    }
    assert "launchpad-memory-admin-cn-northwest-1" not in roles
    body = template.to_json()
    # CDK's own bootstrap-version check is an SSM parameter too; ours is the other one.
    ssm_params = [
        (name, p) for name, p in body["Parameters"].items()
        if p["Type"] == "AWS::SSM::Parameter::Value<String>"
        and p["Default"].startswith("/launchpad/")
    ]
    assert [p["Default"] for _, p in ssm_params] == [
        "/launchpad/memory-domain/cn-northwest-1/endpoint"
    ]
    (param_name, _) = ssm_params[0]
    # the Lambda and the output both carry the resolved endpoint
    fn = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    assert fn["Properties"]["Environment"]["Variables"]["LAUNCHPAD_MEMORY_OS_ENDPOINT"] == {
        "Ref": param_name
    }
    assert body["Outputs"]["MemoryDomainEndpoint"]["Value"] == {"Ref": param_name}
    # the base stack's exports are still imported (R21 — memory deletes before base);
    # none of the domain stack's are — that order is the teardown script's job (D17)
    imports = {
        value["Fn::ImportValue"]
        for value in _walk(body)
        if isinstance(value, dict) and "Fn::ImportValue" in value
    }
    assert imports and all(name.startswith("launchpad-base:") for name in imports), imports
    assert "launchpad-memory-domain" not in json.dumps(body)


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_environment_stack_builds_the_domain_and_admin_role_arns_from_the_fixed_names():
    tpl, base, _ = _synth({"env_name": "t1"})
    text = json.dumps(tpl.to_json()) + json.dumps(base.to_json())
    # partition-aware, region-pinned, the shared (unsuffixed) names
    assert ":es:cn-northwest-1:111111111111:domain/lp-memory-cn-northwest-1" in text
    assert "lp-memory-cn-northwest-1-t1" not in text
    assert ":iam::111111111111:role/launchpad-memory-admin-cn-northwest-1" in text
    assert "launchpad-memory-admin-cn-northwest-1-t1" not in text
    body = tpl.to_json()
    assert body["Outputs"]["MemoryDomainName"]["Value"] == "lp-memory-cn-northwest-1"
    admin = body["Outputs"]["MemoryAdminRoleArn"]["Value"]
    assert "launchpad-memory-admin-cn-northwest-1" in json.dumps(admin)
    assert "launchpad-memory-admin-cn-northwest-1-t1" not in json.dumps(admin)


def test_extractor_lambda_carries_its_environment_name():
    default, _, _ = _synth()
    t1, _, _ = _synth({"env_name": "t1"})
    for tpl, expected in ((default, ""), (t1, "t1")):
        fn = next(iter(tpl.find_resources("AWS::Lambda::Function").values()))
        assert fn["Properties"]["Environment"]["Variables"]["LAUNCHPAD_ENV_NAME"] == expected


def test_exec_role_gets_its_own_domain_grant_now_that_the_access_policy_cannot_name_it():
    """D17: the shared domain's access policy lists only the admin role + account root;
    the per-environment exec role therefore needs an identity grant (FGAC still scopes
    it to the environment's indices)."""
    _, base, _ = _synth()
    statements = []
    for policy in base.find_resources("AWS::IAM::Policy").values():
        statements.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    [stmt] = [s for s in statements if s.get("Sid") == "MemoryDomainReadWrite"]
    assert stmt["Action"] == ["es:ESHttpGet", "es:ESHttpPost", "es:ESHttpPut", "es:ESHttpHead"]
    assert "domain/lp-memory-cn-northwest-1/*" in json.dumps(stmt["Resource"])


def test_queue_dlq_and_lambda(template: Template):
    template.resource_count_is("AWS::SQS::Queue", 2)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        Match.object_like(
            {
                "QueueName": "launchpad-memory-extract-cn-northwest-1",
                "VisibilityTimeout": 2880,  # 6x the bounded 480s Lambda timeout
                "RedrivePolicy": Match.object_like({"maxReceiveCount": 3}),
            }
        ),
    )
    template.has_resource_properties(
        "AWS::Lambda::Function",
        Match.object_like(
            {
                "Runtime": "python3.12",
                "Architectures": ["arm64"],
                "Handler": "handler.lambda_handler",
                "Timeout": 480,
                "MemorySize": 512,
                "Environment": {
                    "Variables": Match.object_like(
                        {
                            "LAUNCHPAD_MODEL_REGION": "us-west-2",
                            "LAUNCHPAD_MODEL_CREDENTIAL": "launchpad-global-bedrock",
                            "LAUNCHPAD_EXTRACT_MODEL_ID": "global.zai.glm-5.3",
                            "LAUNCHPAD_EMBED_MODEL_ID": "amazon.titan-embed-text-v2:0",
                            "LAUNCHPAD_MEMORY_OS_ENDPOINT": Match.any_value(),
                            "LAUNCHPAD_MEMORY_BUCKET": Match.any_value(),
                        }
                    )
                },
            }
        ),
    )
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        Match.object_like({"BatchSize": 1, "FunctionResponseTypes": ["ReportBatchItemFailures"]}),
    )


def test_extractor_role_reads_the_identity_secret_and_prefixes_only(template: Template):
    text = str(template.find_resources("AWS::IAM::Policy"))
    assert "bedrock-agentcore:GetApiKeyCredentialProvider" in text
    assert "secretsmanager:GetSecretValue" in text
    assert "apikeycredentialprovider/launchpad-global-bedrock" in text
    assert "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-*" in text
    # D21 (M1-d): the `launchpad-*` family — an OpenAI-compatible embedding row's provider
    assert "apikeycredentialprovider/launchpad-*" in text
    assert "bedrock-agentcore-identity!default/apikey/launchpad-*-*" in text
    assert "memory/*" in text and "kb/*" in text
    assert "es:ESHttp" in text and "sqs:ReceiveMessage" in text
    # C3/C4: Bedrock is reached with the bearer, never SigV4.
    assert "bedrock:InvokeModel" not in text


def _extractor_statements(template: Template) -> list[dict]:
    """Every statement of every IAM policy in the memory stack, resolved to plain dicts."""
    out: list[dict] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        out.extend(policy["Properties"]["PolicyDocument"]["Statement"])
    return out


def _resource_strings(statement: dict) -> list[str]:
    """Each Resource entry as its rendered string (Fn::Join pieces concatenated)."""
    raw = statement.get("Resource", [])
    entries = raw if isinstance(raw, list) else [raw]
    rendered = []
    for entry in entries:
        if isinstance(entry, dict) and "Fn::Join" in entry:
            sep, parts = entry["Fn::Join"]
            rendered.append(sep.join(p if isinstance(p, str) else "${token}" for p in parts))
        else:
            rendered.append(str(entry))
    return rendered


def test_extractor_may_read_the_credential_provider_at_the_token_vault_level(template: Template):
    # N10-bug2 (live): the service authorises GetApiKeyCredentialProvider on the
    # token VAULT ARN, not the provider ARN; every extractor invocation was denied
    # on `…:token-vault/default`. The vault-level resource must be present and the
    # statement must stay pinned to the one default vault (no `token-vault/*`,
    # never `*`).
    statements = _extractor_statements(template)
    [stmt] = [s for s in statements if s.get("Sid") == "ModelCredentialProvider"]
    assert stmt["Action"] == "bedrock-agentcore:GetApiKeyCredentialProvider"
    resources = _resource_strings(stmt)
    vault = [r for r in resources if r.endswith(":token-vault/default")]
    assert len(vault) == 1, resources
    assert vault[0].startswith("arn:${token}:bedrock-agentcore:cn-northwest-1:111111111111:")
    # the provider ARN is kept alongside (dual shape, same as the runtime role)
    assert any(
        r.endswith(":token-vault/default/apikeycredentialprovider/launchpad-global-bedrock")
        for r in resources
    ), resources
    # D21 (M1-d): plus the `launchpad-*` provider family, still under the one default vault
    assert any(
        r.endswith(":token-vault/default/apikeycredentialprovider/launchpad-*")
        for r in resources
    ), resources
    assert len(resources) == 3
    assert not any(r == "*" or "token-vault/*" in r for r in resources)


def test_extractor_grants_are_never_widened_to_star_or_secret_star(template: Template):
    # Companion to the bug2 fix: fixing the vault resource must not be done by
    # widening either the Identity or the Secrets Manager grant.
    statements = _extractor_statements(template)
    for stmt in statements:
        actions = stmt["Action"] if isinstance(stmt["Action"], list) else [stmt["Action"]]
        resources = _resource_strings(stmt)
        if any(a.startswith("bedrock-agentcore:") for a in actions):
            assert "*" not in resources, stmt
            assert all("token-vault/default" in r for r in resources), resources
        if any(a.startswith("secretsmanager:") for a in actions):
            assert actions == ["secretsmanager:GetSecretValue"], actions
            # D21 (M1-d): the exact Bedrock provider secret AND the `launchpad-*` family —
            # every resource stays under the Identity API-key secret prefix
            assert all(
                "secret:bedrock-agentcore-identity!default/apikey/launchpad-" in r
                for r in resources
            ), resources
            assert any(
                "secret:bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-" in r
                for r in resources
            ), resources
            assert not any(r.endswith("secret:*") or r == "*" for r in resources)


def test_lifecycle_rule_expires_memory_snapshots():
    _, base, _ = _synth()
    base.has_resource_properties(
        "AWS::S3::Bucket",
        Match.object_like(
            {
                "LifecycleConfiguration": {
                    "Rules": Match.array_with(
                        [
                            Match.object_like(
                                {
                                    "Prefix": "memory/",
                                    "ExpirationInDays": 90,
                                    "Status": "Enabled",
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )


def test_outputs_exported(template: Template):
    for name in (
        "MemoryDomainEndpoint",
        "MemoryDomainArn",
        "MemoryDomainName",
        "MemoryQueueUrl",
        "MemoryQueueArn",
        "MemoryExtractorRoleArn",
        "MemoryExtractorFunctionName",
        "MemoryAdminRoleArn",
    ):
        template.has_output(name, Match.any_value())


@pytest.mark.parametrize("which", ["environment", "domain"])
def test_no_hardcoded_partition_in_synth(which, template: Template, domain_template: Template):
    text = str((template if which == "environment" else domain_template).to_json())
    # CDK's own AwsCustomResource provider framework (used internally by
    # Domain.add_access_policies() to call UpdateDomainConfig) hardcodes its
    # Lambda service principal as a plain string — the same partition-invariant
    # exemption test_stack.py already makes for lambda.amazonaws.com.
    residual = text.replace("amazonaws.com.cn", "").replace("lambda.amazonaws.com", "")
    assert "arn:aws:" not in text and ".amazonaws.com" not in residual


def test_extractor_logs_into_a_retention_bounded_group(template: Template):
    """N11-obs56 (same contract as the base stack's sample Lambdas)."""
    groups = template.find_resources("AWS::Logs::LogGroup")
    assert len(groups) == 1
    (group,) = groups.values()
    assert group["Properties"]["RetentionInDays"] == 90
    assert "LogGroupName" not in group["Properties"]
    functions = template.find_resources("AWS::Lambda::Function")
    assert len(functions) == 1
    (fn,) = functions.values()
    assert fn["Properties"]["LoggingConfig"]["LogGroup"]["Ref"] in groups


def test_extractor_may_send_to_its_own_queue_and_knows_its_url(template: Template):
    """N17-obs247: the sync message fans one `kb_ingest_doc` message per changed document out
    onto the extractor's own queue — the role gets `sqs:SendMessage` on THAT queue only, and
    the function learns the queue URL. No other grant changes."""
    fn = next(iter(template.find_resources("AWS::Lambda::Function").values()))
    env = fn["Properties"]["Environment"]["Variables"]
    assert "Ref" in env["LAUNCHPAD_MEMORY_QUEUE_URL"]
    assert env["LAUNCHPAD_MEMORY_QUEUE_URL"]["Ref"].startswith("MemoryQueue")
    send_statements = [
        stmt for stmt in _extractor_statements(template)
        if "sqs:SendMessage" in (stmt["Action"] if isinstance(stmt["Action"], list)
                                 else [stmt["Action"]])
    ]
    assert len(send_statements) == 1, send_statements
    resource = send_statements[0]["Resource"]
    assert resource["Fn::GetAtt"][0].startswith("MemoryQueue")
    assert resource["Fn::GetAtt"][1] == "Arn"
    # the queue's event source stays one message per invocation (one document's work), capped
    # at 5 concurrent invocations by default (rev-r22: the single OpenSearch node, the account)
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", Match.object_like(
        {"BatchSize": 1, "ScalingConfig": {"MaximumConcurrency": 5}}))


def test_extract_max_concurrency_is_a_context_knob_with_the_service_minimum():
    import pytest

    knob, _, _ = _synth({"extract_max_concurrency": "3"})
    knob.has_resource_properties("AWS::Lambda::EventSourceMapping", Match.object_like(
        {"ScalingConfig": {"MaximumConcurrency": 3}}))
    with pytest.raises(ValueError, match="extract_max_concurrency must be >= 2"):
        _synth({"extract_max_concurrency": "1"})
