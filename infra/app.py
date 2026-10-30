import os

import aws_cdk as cdk

from stacks.base_stack import LaunchpadBaseStack
from stacks.memory_domain_stack import DOMAIN_STACK_NAME, LaunchpadMemoryDomainStack
from stacks.memory_stack import LaunchpadMemoryStack
from stacks.naming import env_name_from_context, memory_domain_env, suffixed

app = cdk.App()
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "cn-northwest-1"),
)
# `-c env_name=t1` suffixes every stack and physical resource name with `-t1` so a
# second, parallel environment can coexist with the default one (stacks/naming.py).
# Empty (the default) leaves every name exactly as it was.
env_name = env_name_from_context(app.node)
base = LaunchpadBaseStack(
    app,
    suffixed("launchpad-base", env_name),
    env=env,
    description="AgentCore Launchpad shared infrastructure",
)
# D17: ONE OpenSearch domain per account+region, shared by every environment — the
# stack id is deliberately not suffixed. `cdk deploy --all` from any environment creates
# it when absent and finds it unchanged otherwise (idempotent); teardown removes it last.
domain = LaunchpadMemoryDomainStack(
    app,
    suffixed(DOMAIN_STACK_NAME, memory_domain_env(app.node)),
    env=env,
    description="AgentCore Launchpad shared OpenSearch domain for self-hosted Memory/KB",
)
memory = LaunchpadMemoryStack(
    app,
    suffixed("launchpad-memory", env_name),
    base=base,
    env=env,
    description="AgentCore Launchpad self-hosted Memory/KB substrate (per environment)",
)
# No CloudFormation export links the two (memory_stack.py resolves the endpoint from
# SSM), so the deploy order is stated explicitly: the domain's parameter must exist
# before the per-environment stack is created.
memory.add_dependency(domain)
app.synth()
