"""Provision the Store CRM demo (idempotent, out-of-band — not part of CDK).

  cd backend && uv run python ../samples/store-demo/store-crm-api/provision.py us-west-2 [--reseed]

Without --reseed only missing seed rows are added (demo-run changes are kept); --reseed resets
every table to seed.json.

Creates/updates: 4 DynamoDB tables + seed rows, Lambda role + function, the gateway role's
invoke grant, the Gateway target `store-crm` (7 inline tools) and one Cedar permit for the 7
tools (the shared gateway's policy engine is ENFORCE → new targets are default-deny).
"""

import io
import json
import pathlib
import sys
import time
import zipfile
from decimal import Decimal

import boto3
import yaml

HERE = pathlib.Path(__file__).resolve().parent
REGION = sys.argv[1]
RESEED = "--reseed" in sys.argv
CFG = yaml.safe_load((HERE.parents[2] / "config" / "launchpad.yaml").read_text())
assert CFG["region"] == REGION, (CFG["region"], REGION)
RES = CFG["resources"]
ACCOUNT = CFG["account_id"]
GATEWAY_ID = RES["gateway_id"]
GATEWAY_ROLE = RES["gateway_role_arn"].rsplit("/", 1)[1]
PREFIX = "store-demo-"
FN = "launchpad-store-crm"
ROLE = "launchpad-store-crm-lambda"
TARGET = "store-crm"

s = boto3.Session(region_name=REGION)
ddb, iam, lam = s.client("dynamodb"), s.client("iam"), s.client("lambda")
ctl = s.client("bedrock-agentcore-control")
seed = json.loads((HERE / "seed.json").read_text(), parse_float=Decimal)

TABLES = {
    "customers": [("customer_id", "HASH")],
    "purchases": [("customer_id", "HASH"), ("order_id", "RANGE")],
    "tickets": [("ticket_id", "HASH")],
    "products": [("sku", "HASH")],
}


def ensure_tables():
    existing = set(ddb.list_tables()["TableNames"])
    for t, keys in TABLES.items():
        name = PREFIX + t
        if name in existing:
            continue
        ddb.create_table(
            TableName=name, BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": a, "KeyType": k} for a, k in keys],
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a, _ in keys],
            Tags=[{"Key": "launchpad:demo", "Value": "store-crm"}])
        print("created table", name)
    for t in TABLES:
        ddb.get_waiter("table_exists").wait(TableName=PREFIX + t)


def load_seed():
    res = s.resource("dynamodb")
    for t, rows in (("customers", seed["customers"]), ("purchases", seed["purchases"]),
                    ("tickets", seed["tickets"]), ("products", seed["products"])):
        table = res.Table(PREFIX + t)
        keys = [k for k, _ in TABLES[t]]
        scan = table.scan(ProjectionExpression=", ".join(keys))["Items"]
        present = set()
        if RESEED:  # drop everything (incl. tickets created by demo runs)
            with table.batch_writer() as bw:
                for item in scan:
                    bw.delete_item(Key={k: item[k] for k in keys})
        else:  # only add missing rows: never overwrite state a demo run has changed
            present = {tuple(item[k] for k in keys) for item in scan}
        rows = [r for r in rows if tuple(r[k] for k in keys) not in present]
        with table.batch_writer() as bw:
            for r in rows:
                bw.put_item(Item={k: v for k, v in r.items() if v is not None})
        print("loaded", t, len(rows))


def ensure_role():
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
        "Action": "sts:AssumeRole"}]}
    try:
        arn = iam.get_role(RoleName=ROLE)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        arn = iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust),
                              Tags=[{"Key": "launchpad:demo", "Value": "store-crm"}])["Role"]["Arn"]
        print("created role", ROLE)
        time.sleep(10)
    tables = [f"arn:aws:dynamodb:{REGION}:{ACCOUNT}:table/{PREFIX}{t}" for t in TABLES]
    # IAM is account-global: one inline policy PER REGION, or a second region's run overwrites
    # the first one's table grants (live 2026-10-08: dev provisioning broke the prod Lambda)
    iam.put_role_policy(RoleName=ROLE, PolicyName=f"store-crm-data-{REGION}", PolicyDocument=json.dumps({
        "Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan",
                                           "dynamodb:PutItem", "dynamodb:UpdateItem"],
             "Resource": tables},
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                                           "logs:PutLogEvents"],
             "Resource": f"arn:aws:logs:{REGION}:{ACCOUNT}:log-group:/aws/lambda/{FN}:*"}]}))
    return arn


def ensure_function(role_arn):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "handler.py", "handler.py")
    code = buf.getvalue()
    env = {"Variables": {"TABLE_PREFIX": PREFIX, "AS_OF": seed["as_of"]}}
    try:
        lam.get_function(FunctionName=FN)
        lam.update_function_code(FunctionName=FN, ZipFile=code)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FN)
        lam.update_function_configuration(FunctionName=FN, Environment=env)
        print("updated function", FN)
    except lam.exceptions.ResourceNotFoundException:
        for _ in range(6):
            try:
                lam.create_function(FunctionName=FN, Runtime="python3.12", Role=role_arn,
                                    Handler="handler.lambda_handler", Code={"ZipFile": code},
                                    Architectures=["arm64"], Timeout=15, MemorySize=256,
                                    Environment=env, Tags={"launchpad:demo": "store-crm"})
                break
            except lam.exceptions.InvalidParameterValueException:
                time.sleep(5)  # role not assumable yet
        print("created function", FN)
    lam.get_waiter("function_active_v2").wait(FunctionName=FN)
    return lam.get_function(FunctionName=FN)["Configuration"]["FunctionArn"]


def ensure_gateway_grant(fn_arn):
    iam.put_role_policy(RoleName=GATEWAY_ROLE, PolicyName="store-crm-invoke",
                        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
                            "Effect": "Allow", "Action": "lambda:InvokeFunction",
                            "Resource": [fn_arn, fn_arn + ":*"]}]}))


def ensure_target(fn_arn):
    tools = json.loads((HERE / "tool_schema.json").read_text())
    cfg = {"mcp": {"lambda": {"lambdaArn": fn_arn, "toolSchema": {"inlinePayload": tools}}}}
    common = dict(
        gatewayIdentifier=GATEWAY_ID, name=TARGET,
        description="无人机授权体验店 CRM（测试环境）：会员、购买记录、推荐商品、售后工单",
        targetConfiguration=cfg,
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}])
    items = ctl.list_gateway_targets(gatewayIdentifier=GATEWAY_ID)["items"]
    hit = next((t for t in items if t["name"] == TARGET), None)
    if hit:
        ctl.update_gateway_target(targetId=hit["targetId"], **common)
        tid = hit["targetId"]
        print("updated target", tid)
    else:
        tid = ctl.create_gateway_target(**common)["targetId"]
        print("created target", tid)
    for _ in range(60):
        st = ctl.get_gateway_target(gatewayIdentifier=GATEWAY_ID, targetId=tid)["status"]
        if st in ("READY", "FAILED", "UPDATE_UNSUCCESSFUL"):
            break
        time.sleep(3)
    print("target status", st)
    return tid, [t["name"] for t in tools]


def ensure_permit(tool_names):
    gw = ctl.get_gateway(gatewayIdentifier=GATEWAY_ID)
    engine = gw["policyEngineConfiguration"]["arn"].rsplit("/", 1)[1]
    actions = ", ".join(f'AgentCore::Action::"{TARGET}___{n}"' for n in tool_names)
    statement = (f"permit(\n  principal is AgentCore::OAuthUser,\n  action in [{actions}],\n"
                 f'  resource == AgentCore::Gateway::"{gw["gatewayArn"]}"\n);')
    name = "store_crm_demo_tools"
    pols = ctl.list_policies(policyEngineId=engine)["policies"]
    hit = next((p for p in pols if p["name"] == name), None)
    if hit:
        ctl.update_policy(policyEngineId=engine, policyId=hit["policyId"],
                          definition={"cedar": {"statement": statement}})
        pid = hit["policyId"]
    else:
        pid = ctl.create_policy(policyEngineId=engine, name=name,
                                description="Store CRM demo: allow the 7 store-crm tools",
                                definition={"cedar": {"statement": statement}})["policyId"]
    for _ in range(40):
        st = ctl.get_policy(policyEngineId=engine, policyId=pid)["status"]
        if st in ("ACTIVE", "CREATE_FAILED", "UPDATE_FAILED"):
            break
        time.sleep(3)
    print("permit", pid, st)


ensure_tables()
load_seed()
fn_arn = ensure_function(ensure_role())
ensure_gateway_grant(fn_arn)
tid, names = ensure_target(fn_arn)
ensure_permit(names)
print("done", fn_arn, tid)
