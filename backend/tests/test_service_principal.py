"""N11-bug5: IAM service principals are partition-invariant (aws-cn rejects *.amazonaws.com.cn)."""

from app.services import aws_clients
from app.services.workspace import WorkspaceContext


def test_service_principal_never_carries_the_cn_suffix():
    assert aws_clients.service_principal("bedrock-agentcore") == "bedrock-agentcore.amazonaws.com"
    cn = WorkspaceContext(account_id="123456789012", region="cn-northwest-1")
    assert cn.dns_suffix == "amazonaws.com.cn"
    assert cn.service_principal("bedrock-agentcore") == "bedrock-agentcore.amazonaws.com"
    assert cn.service_principal("lambda") == "lambda.amazonaws.com"
