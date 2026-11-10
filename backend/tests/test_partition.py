import pytest

from app.services import aws_clients
from app.services.workspace import WorkspaceContext


def test_partition_for_region_maps_cn_and_commercial():
    assert aws_clients.partition_for_region("cn-northwest-1") == "aws-cn"
    assert aws_clients.partition_for_region("cn-north-1") == "aws-cn"
    assert aws_clients.partition_for_region("us-west-2") == "aws"


def test_partition_for_region_raises_value_error_not_botocores_own():
    """A region that is shaped like a region but unknown to botocore's endpoint
    data must surface as a plain ValueError — not botocore's own
    UnknownRegionError leaking into every caller (ARN builders, routers)."""
    aws_clients.partition_for_region.cache_clear()
    with pytest.raises(ValueError, match="xx-fake-1"):
        aws_clients.partition_for_region("xx-fake-1")


def test_dns_suffix_for_partition():
    assert aws_clients.dns_suffix_for_partition("aws-cn") == "amazonaws.com.cn"
    assert aws_clients.dns_suffix_for_partition("aws") == "amazonaws.com"


def test_workspace_context_partition_and_suffix():
    cn = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    us = WorkspaceContext(account_id="111111111111", region="us-west-2")
    assert cn.partition == "aws-cn" and cn.dns_suffix == "amazonaws.com.cn"
    assert us.partition == "aws" and us.dns_suffix == "amazonaws.com"


def test_workspace_context_arn_builder():
    ctx = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    assert (
        ctx.arn("bedrock-agentcore", "runtime/*")
        == "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:runtime/*"
    )
    assert ctx.arn("s3", "my-bucket", region="", account="") == "arn:aws-cn:s3:::my-bucket"
    assert ctx.arn("iam", "role/x", region="") == "arn:aws-cn:iam::123456789012:role/x"
    assert (
        ctx.arn("bedrock", "foundation-model/m", region="*", account="")
        == "arn:aws-cn:bedrock:*::foundation-model/m"
    )
