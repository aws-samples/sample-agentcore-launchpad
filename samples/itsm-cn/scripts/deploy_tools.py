"""Deploy only scenario-owned service/Lambda/Gateway, referencing the fixed shared EC2."""

import base64
import io
import json
import secrets
import time
import zipfile

from lab import (
    AWS,
    CONFIG,
    CONFIG_DIGEST,
    NAME,
    ROOT,
    SCENARIO,
    SSM,
    evidence,
    grant_publish_targets,
    load,
    publish_targets,
    remote,
)
from lab import save as save_manifest


def save(state):
    # This process owns infrastructure fields only; agent preparation may run while
    # AWS provisions a VPC interface.
    import fcntl

    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        for key in ("service_installed", "service", "lambda_sg", "roles", "lambda", "gateway"):
            if key in state:
                merged[key] = state[key]
        save_manifest(merged)


ec2 = AWS.client("ec2")
iam = AWS.client("iam")
lam = AWS.client("lambda")
gw = AWS.client("bedrock-agentcore-control")
ACCOUNT = CONFIG["account_id"]
REGION = CONFIG["region"]
HOST = CONFIG["tools_host"]
PORT = CONFIG["tools_port"]
USER = CONFIG["tools_user"]
host = ec2.describe_instances(InstanceIds=[HOST])["Reservations"][0]["Instances"][0]
HOST_SG = host["SecurityGroups"][0]["GroupId"]
VPC = host["VpcId"]
SERVICE_URL = f"http://{host['PrivateIpAddress']}:{PORT}"
m = load()
if not m.get("kb") or not m["kb"].get("data_sources"):
    raise RuntimeError("Create KB first to scope publication permissions")
ds = m["kb"]["data_sources"][0]
bucket, prefix = ds["bucket"], ds["prefix"]
SHARED_TARGET = f"s3://{bucket}/" + (prefix.strip("/") + "/" if prefix.strip("/") else "")
# Every configured staff publish target (plus the shared source) the Lambda may write to.
targets = publish_targets(SHARED_TARGET)
key = "launchpad-" + SCENARIO + "-api-key"
try:
    token = SSM.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
except SSM.exceptions.ParameterNotFound:
    token = secrets.token_urlsafe(48)
    SSM.put_parameter(Name=key, Type="SecureString", Value=token, Overwrite=False)

if not m.get("service_installed"):
    code = base64.b64encode((ROOT / "lab-assets/service.py").read_bytes()).decode()
    remote(
        f"""set -eu
DIR=/srv/launchpad-demo/scenarios/{SCENARIO}
test ! -e "$DIR/service.py" || test "$(cat "$DIR/.run")" = "{CONFIG_DIGEST}"
if test ! -e "$DIR/service.py" && ss -ltn | awk '{{print $4}}' | grep -q ':{PORT}$'; then exit 12; fi
install -d -m 750 -o {USER} -g {USER} "$DIR"
printf '%s' '{code}' | base64 -d > "$DIR/service.py"
umask 077
aws ssm get-parameter --region {REGION} --name {key} --with-decryption --query Parameter.Value --output text | tr -d '\n' > "$DIR/service-token"
printf '%s' '{SCENARIO}' > "$DIR/.scenario"
printf '%s' '{CONFIG_DIGEST}' > "$DIR/.run"
chmod 600 "$DIR/service-token"
chown {USER}:{USER} "$DIR/service.py" "$DIR/service-token"
cat > /etc/systemd/system/{NAME}.service <<'UNIT'
[Unit]
Description=Launchpad synthetic ITSM workshop service
After=network.target
[Service]
Environment=ITSM_SCENARIO={SCENARIO}
User={USER}
WorkingDirectory=/srv/launchpad-demo/scenarios/{SCENARIO}
ExecStart=/usr/bin/python3 /srv/launchpad-demo/scenarios/{SCENARIO}/service.py --port {PORT}
Restart=on-failure
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/srv/launchpad-demo/scenarios/{SCENARIO}
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now {NAME}
sleep 2
systemctl is-active {NAME}
curl -fsS http://127.0.0.1:{PORT}/health
""",
        "service-install",
    )
    m["service_installed"] = True
    m["service"] = {
        "host": HOST,
        "private_url": SERVICE_URL,
        "unit": NAME + ".service",
        "path": f"/srv/launchpad-demo/scenarios/{SCENARIO}",
        "ownership": "created",
    }
    save(m)

if not m.get("lambda_sg"):
    sg = ec2.create_security_group(
        GroupName=NAME, Description="ITSM lab Lambda to private tools host", VpcId=VPC
    )["GroupId"]
    ec2.create_tags(Resources=[sg], Tags=[{"Key": "Scenario", "Value": SCENARIO}])
    ec2.authorize_security_group_ingress(
        GroupId=HOST_SG,
        IpPermissions=[
            {
                "IpProtocol": "tcp",
                "FromPort": PORT,
                "ToPort": PORT,
                "UserIdGroupPairs": [{"GroupId": sg, "Description": SCENARIO}],
            }
        ],
    )
    m["lambda_sg"] = sg
    save(m)


def role(name, service):
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
        ],
    }
    try:
        return iam.create_role(
            RoleName=name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Tags=[
                {"Key": "Scenario", "Value": SCENARIO},
                {"Key": "LaunchpadRun", "Value": CONFIG_DIGEST},
            ],
        )["Role"]["Arn"]
    except iam.exceptions.EntityAlreadyExistsException:
        r = iam.get_role(RoleName=name)["Role"]
        if not any(
            t["Key"] == "LaunchpadRun" and t["Value"] == CONFIG_DIGEST for t in r.get("Tags", [])
        ):
            raise RuntimeError("Unowned existing role: " + name) from None
        return r["Arn"]


lambda_role = role(NAME + "-lambda", "lambda.amazonaws.com")
iam.attach_role_policy(
    RoleName=NAME + "-lambda",
    PolicyArn="arn:aws-cn:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
)
# N17-obs525: the role policy AND the S3 endpoint policy (when prepare_network.py created it)
# must both name every publish target; refresh_service.py repeats this on each roll.
grant = grant_publish_targets(m, targets, role_name=NAME + "-lambda")
if grant["endpoint_changed"]:
    print("S3 endpoint policy updated", grant["endpoint"], flush=True)
if m.get("service"):
    m["service"] = dict(m["service"], publish_targets=targets)
gateway_role = role(NAME + "-gateway", "bedrock-agentcore.amazonaws.com")
lambda_arn = f"arn:aws-cn:lambda:{REGION}:{ACCOUNT}:function:{NAME}"
iam.put_role_policy(
    RoleName=NAME + "-gateway",
    PolicyName="InvokeWorkshopTools",
    PolicyDocument=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": "lambda:InvokeFunction", "Resource": lambda_arn}
            ],
        }
    ),
)
m["roles"] = {"lambda": lambda_role, "gateway": gateway_role}
save(m)
if not m.get("lambda"):
    if not m.get("lambda_subnet"):
        raise RuntimeError(
            "Run prepare_network.py after KB preparation, before creating the Lambda."
        )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("lambda_function.py", (ROOT / "lab-assets/lambda_bridge.py").read_text())
    for attempt in range(12):
        try:
            lam.create_function(
                FunctionName=NAME,
                Role=lambda_role,
                Runtime="python3.12",
                Handler="lambda_function.handler",
                Code={"ZipFile": buf.getvalue()},
                Timeout=40,
                MemorySize=256,
                Architectures=["arm64"],
                VpcConfig={"SubnetIds": [m["lambda_subnet"]], "SecurityGroupIds": [m["lambda_sg"]]},
                Environment={
                    "Variables": {
                        "SERVICE_URL": SERVICE_URL,
                        "SERVICE_TOKEN": token,
                        "KB_BUCKET": bucket,
                        "KB_PREFIX": prefix,
                    }
                },
                Tags={"Scenario": SCENARIO},
            )
            break
        except lam.exceptions.InvalidParameterValueException:
            if attempt == 11:
                raise
            time.sleep(5)
    m["lambda"] = lambda_arn
    save(m)
    print("Lambda created", lambda_arn, flush=True)
lam.get_waiter("function_active_v2").wait(FunctionName=NAME)
if not m.get("gateway"):
    r = gw.create_gateway(
        name=NAME,
        description="ITSM CN workshop isolated gateway",
        roleArn=gateway_role,
        protocolType="MCP",
        authorizerType="AWS_IAM",
        tags={"Scenario": SCENARIO},
    )
    m["gateway"] = {
        "id": r["gatewayId"],
        "arn": r["gatewayArn"],
        "url": r["gatewayUrl"],
        "name": NAME,
        "targets": {},
    }
    save(m)
    print("Gateway created", r["gatewayId"], flush=True)
gid = m["gateway"]["id"]
for _ in range(80):
    r = gw.get_gateway(gatewayIdentifier=gid)
    if r["status"] == "READY":
        break
    time.sleep(3)
else:
    raise RuntimeError("Gateway not READY")
schemas = json.loads((ROOT / "lab-assets/tools.json").read_text())
for name, tools in schemas.items():
    if name in m["gateway"]["targets"]:
        continue
    r = gw.create_gateway_target(
        gatewayIdentifier=gid,
        name=name,
        description="Synthetic ITSM workshop " + name,
        targetConfiguration={
            "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tools}}}
        },
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    m["gateway"]["targets"][name] = {"id": r["targetId"], "name": name}
    save(m)
    print("Target created", name, r["targetId"], flush=True)
for target in m["gateway"]["targets"].values():
    for _ in range(60):
        r = gw.get_gateway_target(gatewayIdentifier=gid, targetId=target["id"])
        if r["status"] == "READY":
            break
        time.sleep(3)
    else:
        raise RuntimeError("Target not ready")
evidence(
    "tools-deployed.json", {"gateway": m["gateway"], "service": m["service"], "lambda": lambda_arn}
)
print("Workshop tools deployed.", flush=True)
