"""LaunchpadMemoryDomainStack — the ONE OpenSearch domain every EnvName environment of an
account+region shares (D17).

Distribution extension: `memory_domain_mode=dedicated` selects an environment-owned
OpenSearch domain, admin role and discovery path. The D17 shared behavior described
below remains the default for existing configurations; see docs/cn/distribution.md.

Cold-starting an environment used to spend ~15–16 of its ~27 minutes creating its own
`lp-memory-<region>[-env]` domain. Since D17 the domain (k-NN, fine-grained access
control, encrypted, HTTPS-only) lives in this stack, deliberately **not** EnvName-suffixed
(`launchpad-memory-domain`), and every per-environment `launchpad-memory[-env]` stack
(`memory_stack.py`) only adds its own queue, extractor Lambda and IAM on top of it.
Isolation between environments is per-index: the backend prefixes every index with the
environment (`backend/app/core/naming.py::index_name`) and gives each environment an FGAC
role limited to its own prefix (`memory_bootstrap.py`).

FGAC master: a stack-owned admin role `launchpad-memory-admin-<region>` (trust = this
account). Binding the master to one environment's ops role (the pre-D17 `memory_master_arn`
context) would have left the shared domain without an administrable master the day that
environment was torn down, so the context is gone: every environment's backend assumes the
admin role for its OpenSearch calls (it already did whenever the stack exported one —
`memory_admin_role_arn` on the workspace context), and the EC2 ops role is granted
`sts:AssumeRole` on `role/launchpad-memory-admin-*` for it.

How later environments find the domain without a cross-stack export: the domain name and
the admin role name are fixed, so their ARNs are computed from `(partition, region,
account)`; only the endpoint hostname (random suffix) is published — as this stack's
`MemoryDomainEndpoint` output *and* the SSM parameter
`/launchpad/memory-domain/<region>/endpoint`, which the per-environment stack resolves
at deploy time through an `AWS::SSM::Parameter::Value<String>` parameter.
"""

from aws_cdk import CfnOutput, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_opensearchservice as opensearch
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from stacks.base_stack import LEGACY_REGION
from stacks.naming import memory_domain_env, suffixed

# Unsuffixed on purpose — one per account+region (mirrored by
# backend/app/services/memory_bootstrap.py::MEMORY_DOMAIN_STACK_NAME).
DOMAIN_STACK_NAME = "launchpad-memory-domain"
SSM_PREFIX = "/launchpad/memory-domain"

DEFAULT_INSTANCE_TYPE = "t3.small.search"
# UNVERIFIED in cn-northwest-1 (R13b); context override via `opensearch_version`,
# documented fallback OpenSearch_2.17.
DEFAULT_VERSION = "OpenSearch_2.19"


def regional_domain_name(region: str, env_name: str = "") -> str:
    """OpenSearch domain names must be <= 28 chars, lowercase, start with a letter."""
    return suffixed("lp-memory" if region == LEGACY_REGION else f"lp-memory-{region}", env_name)


def admin_role_name(region: str, env_name: str = "") -> str:
    return suffixed(f"launchpad-memory-admin-{region}", env_name)


def ssm_parameter_name(region: str, key: str, env_name: str = "") -> str:
    """`/launchpad/memory-domain/<region>/<key>` — `key` ∈ endpoint | arn | admin-role-arn."""
    return f"{SSM_PREFIX}/{region}/" + (f"{env_name}/" if env_name else "") + key


class LaunchpadMemoryDomainStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        domain_env = memory_domain_env(self.node)

        def ctx(key, default):
            return self.node.try_get_context(key) or default

        # ---- FGAC master: the stack-owned admin role every environment assumes ----
        # No principal is granted sts:AssumeRole on it *here* — the EC2 ops role's own
        # policy carries that grant (infra/ec2/launchpad-cn-ec2.yaml, Sid
        # MemoryAdminAssume); an off-host operator adds it to their identity.
        admin_role = iam.Role(
            self,
            "MemoryAdminRole",
            role_name=admin_role_name(self.region, domain_env),
            assumed_by=iam.AccountPrincipal(self.account),
            description=(
                "FGAC master of the shared Launchpad memory domain; assumed by every "
                "environment's hub for bootstrap/console access"
            ),
        )

        # ---- OpenSearch domain: one t3.small node, FGAC, encrypted, HTTPS-only ----
        domain = opensearch.Domain(
            self,
            "MemoryDomain",
            domain_name=regional_domain_name(self.region, domain_env),
            version=opensearch.EngineVersion.open_search(
                str(ctx("opensearch_version", DEFAULT_VERSION)).removeprefix("OpenSearch_")
            ),
            capacity=opensearch.CapacityConfig(
                data_nodes=1,
                data_node_instance_type=ctx("opensearch_instance_type", DEFAULT_INSTANCE_TYPE),
                multi_az_with_standby_enabled=False,
            ),
            ebs=opensearch.EbsOptions(volume_size=10, volume_type=ec2.EbsDeviceVolumeType.GP3),
            encryption_at_rest=opensearch.EncryptionAtRestOptions(enabled=True),
            node_to_node_encryption=True,
            enforce_https=True,
            fine_grained_access_control=opensearch.AdvancedSecurityOptions(
                master_user_arn=admin_role.role_arn
            ),
            # Deleting this stack is an explicit, last-environment-standing act
            # (scripts/teardown.py --delete-shared-domain); the domain goes with it.
            removal_policy=RemovalPolicy.DESTROY,
        )
        # Access policy names the master and the account root only — never "*" (C6).
        # The per-environment principals (exec role, extractor, agent roles) are unknown
        # here and arrive later; the account-root entry delegates them to their own IAM
        # identity policies (`es:ESHttp*` on this domain, granted in memory_stack.py /
        # agent_iam.py), and FGAC then confines each to its environment's indices.
        domain.add_access_policies(
            iam.PolicyStatement(
                actions=["es:ESHttp*"],
                resources=[f"{domain.domain_arn}/*"],
                principals=[admin_role, iam.AccountRootPrincipal()],
            )
        )

        # ---- discovery for environments created later (no cross-stack export) ----
        ssm.StringParameter(
            self,
            "MemoryDomainEndpointParameter",
            parameter_name=ssm_parameter_name(self.region, "endpoint", domain_env),
            string_value=domain.domain_endpoint,
            description="Shared Launchpad memory OpenSearch domain endpoint (D17)",
        )
        ssm.StringParameter(
            self,
            "MemoryDomainArnParameter",
            parameter_name=ssm_parameter_name(self.region, "arn", domain_env),
            string_value=domain.domain_arn,
            description="Shared Launchpad memory OpenSearch domain ARN (D17)",
        )
        ssm.StringParameter(
            self,
            "MemoryAdminRoleArnParameter",
            parameter_name=ssm_parameter_name(self.region, "admin-role-arn", domain_env),
            string_value=admin_role.role_arn,
            description="FGAC master role of the shared Launchpad memory domain (D17)",
        )

        for output_name, value in (
            ("MemoryDomainEndpoint", domain.domain_endpoint),
            ("MemoryDomainArn", domain.domain_arn),
            ("MemoryDomainName", domain.domain_name),
            ("MemoryAdminRoleArn", admin_role.role_arn),
        ):
            CfnOutput(self, output_name, value=value)
