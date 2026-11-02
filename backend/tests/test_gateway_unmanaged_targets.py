"""The Gateway's other targets (prod QA 2026-11-02, G3): a Lambda target made by hand
(not through the console) appeared in neither list of the 工具目标 tab and could not
be brought under management. `/api/gateway-tools/unmanaged` lists every target the
two lists leave out, with why it cannot be adopted; a hand-made inline-schema Lambda
target is adopted as a live console Lambda target with an approved baseline."""

# `world` / `app` / `aws` are fixtures imported from the hosted-MCP tests and then
# requested by name — which ruff reads as a redefinition.
# ruff: noqa: F811

from app.core.db import SessionLocal
from app.core.project_scope import project_context
from app.models.lambda_tools import LambdaToolTarget
from app.services import workspace_iam
from tests.test_mcp_rest import LAMBDA_ARN, SCHEMA, _admin, app, world  # noqa: F401
from tests.test_mcp_servers import aws  # noqa: F401


def _lambda_target(name: str, *, arn: str = LAMBDA_ARN, inline=True, schema=None) -> dict:
    tool_schema = {"inlinePayload": schema or SCHEMA} if inline else {
        "s3": {"uri": "s3://bucket/schema.json"}}
    return {"name": name, "description": f"{name} by hand", "targetConfiguration": {
        "mcp": {"lambda": {"lambdaArn": arn, "toolSchema": tool_schema}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]}


def _seed(world) -> None:
    targets = world.aws.control.targets
    targets["L1"] = _lambda_target("anyhr-leave")
    targets["L2"] = _lambda_target("hr-database")  # a bootstrap target
    targets["L3"] = _lambda_target(
        "other-region", arn="arn:aws:lambda:eu-west-1:111122223333:function:crm-tools")
    targets["L4"] = _lambda_target("s3-schema", inline=False)
    targets["O1"] = {"name": "office-facts-copy", "targetConfiguration": {
        "mcp": {"openApiSchema": {"inlinePayload": "{}"}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "API_KEY",
            "credentialProvider": {"apiKeyCredentialProvider": {
                "providerArn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/"
                               "default/apikeycredentialprovider/office-key"}}}]}
    # an MCP-server target belongs to the externally hosted list, not this one
    targets["X1"] = {"name": "legacy-crm", "targetConfiguration": {
        "mcp": {"mcpServer": {"endpoint": "https://crm.example.com/mcp"}}}}


def test_other_targets_are_listed_with_why_they_cannot_be_adopted(world):
    _seed(world)
    listed = {t["target_id"]: t for t in world.dev.get(
        "/api/gateway-tools/unmanaged").json()["targets"]}
    assert set(listed) == {"L1", "L2", "L3", "L4", "O1"}
    assert listed["L1"]["reason"] is None and listed["L1"]["kind"] == "lambda"
    assert listed["L1"]["tool_names"] == ["lookup"] and listed["L1"]["lambda_arn"] == LAMBDA_ARN
    assert listed["L2"]["reason"] == "system" and listed["L2"]["system"] is True
    assert listed["L3"]["reason"] == "environment"
    assert listed["L4"]["reason"] == "schema"
    assert listed["O1"]["reason"] == "connection" and listed["O1"]["connection"] == "office-key"
    assert listed["O1"]["kind"] == "openApiSchema"


def test_a_hand_made_lambda_target_is_adopted_with_an_approved_baseline(world):
    _seed(world)
    res = world.dev.post("/api/gateway-tools/unmanaged/L1/adopt")
    assert res.status_code == 403  # mcp.approve
    res = world.steward.post("/api/gateway-tools/unmanaged/L1/adopt")
    assert res.status_code == 403  # two projects: shared infrastructure, admins only
    admin = _admin(world.app)
    headers = {"X-Project": "alpha"}
    res = admin.post("/api/gateway-tools/unmanaged/L1/adopt", headers=headers)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["status"] == "live" and body["target_id"] == "L1"
    assert body["review"]["state"] == "approved"
    assert body["tool_schema"][0]["name"] == "lookup"
    # nothing about the target changed; the console's invoke grant now names the function
    assert world.aws.control.targets["L1"]["name"] == "anyhr-leave"
    policy = world.iam.policies[("gw-role", workspace_iam.LAMBDA_TOOLS_POLICY_NAME)]
    assert LAMBDA_ARN in policy

    # it moved from the "other" list to the Lambda list
    others = world.dev.get("/api/gateway-tools/unmanaged").json()["targets"]
    assert "L1" not in {t["target_id"] for t in others}
    [row] = world.dev.get("/api/gateway-tools/lambda").json()["targets"]
    assert row["target_id"] == "L1" and row["status"] == "live"

    again = admin.post("/api/gateway-tools/unmanaged/L1/adopt", headers=headers)
    assert again.status_code == 409 and again.json()["code"] == "gateway_tool.already_adopted"

    # managed now: the console's delete removes the Gateway target and the grant
    res = admin.delete(f"/api/gateway-tools/lambda/{body['id']}", headers=headers)
    assert res.status_code == 200, res.text
    assert "L1" not in world.aws.control.targets
    assert world.iam.policies == {}


def test_targets_that_cannot_be_adopted_are_refused(world):
    _seed(world)
    admin = _admin(world.app)
    headers = {"X-Project": "alpha"}
    for target_id, reason in (("L2", "system"), ("L3", "environment"), ("L4", "schema"),
                              ("O1", "connection")):
        res = admin.post(f"/api/gateway-tools/unmanaged/{target_id}/adopt", headers=headers)
        assert res.status_code == 409, target_id
        assert res.json()["code"] == "gateway_tool.not_adoptable"
        assert res.json()["detail"]["reason"] == reason
    res = admin.post("/api/gateway-tools/unmanaged/NOPE/adopt", headers=headers)
    assert res.status_code == 404


def test_a_target_another_project_manages_says_so(world):
    _seed(world)
    db = SessionLocal()
    with project_context("someone-else"):
        db.add(LambdaToolTarget(workspace_id="default", gateway_id="gw-1", target_id="L1",
                                target_name="anyhr-leave", lambda_arn=LAMBDA_ARN,
                                tool_schema=SCHEMA, status="live"))
        db.commit()
    db.close()
    listed = {t["target_id"]: t for t in world.dev.get(
        "/api/gateway-tools/unmanaged").json()["targets"]}
    assert listed["L1"]["adopted_elsewhere"] is True and listed["L1"]["reason"] == "elsewhere"
