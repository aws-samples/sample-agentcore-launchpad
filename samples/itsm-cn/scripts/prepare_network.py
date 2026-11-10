"""Scenario-only Lambda subnet/route/S3 endpoint; never changes shared route tables."""

import fcntl
import ipaddress
import json

from lab import AWS, CONFIG, NAME, ROOT, SCENARIO, evidence, load, save

m = load()
ec2 = AWS.client("ec2")
host = ec2.describe_instances(InstanceIds=[CONFIG["tools_host"]])["Reservations"][0]["Instances"][0]
VPC = host["VpcId"]
tags = [{"Key": "Scenario", "Value": SCENARIO}, {"Key": "Name", "Value": NAME + "-private"}]


def persist(key, value):
    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load()
        state[key] = value
        save(state)
        m[key] = value


if not m.get("lambda_subnet"):
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [VPC]}])["Subnets"]
    proposed = ipaddress.ip_network(CONFIG["lambda_subnet_cidr"])
    if any(proposed.overlaps(ipaddress.ip_network(s["CidrBlock"])) for s in subnets):
        raise RuntimeError("lambda_subnet_cidr overlaps an existing subnet")
    vpc = ec2.describe_vpcs(VpcIds=[VPC])["Vpcs"][0]
    if not proposed.subnet_of(ipaddress.ip_network(vpc["CidrBlock"])):
        raise RuntimeError("lambda_subnet_cidr must be inside the tools host VPC")
    source = next(s for s in subnets if s["SubnetId"] == host["SubnetId"])
    subnet = ec2.create_subnet(
        VpcId=VPC,
        CidrBlock=str(proposed),
        AvailabilityZone=source["AvailabilityZone"],
        TagSpecifications=[{"ResourceType": "subnet", "Tags": tags}],
    )["Subnet"]
    persist("lambda_subnet", subnet["SubnetId"])
if not m.get("lambda_route_table"):
    route = ec2.create_route_table(
        VpcId=VPC, TagSpecifications=[{"ResourceType": "route-table", "Tags": tags}]
    )["RouteTable"]
    persist("lambda_route_table", route["RouteTableId"])
    assoc = ec2.associate_route_table(
        RouteTableId=route["RouteTableId"], SubnetId=m["lambda_subnet"]
    )
    persist("lambda_route_association", assoc["AssociationId"])
if not m.get("lambda_s3_endpoint"):
    ds = m["kb"]["data_sources"][0]
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": f"arn:aws-cn:s3:::{ds['bucket']}/{ds['prefix'].rstrip('/')}/*",
            }
        ],
    }
    endpoint = ec2.create_vpc_endpoint(
        VpcId=VPC,
        VpcEndpointType="Gateway",
        ServiceName="com.amazonaws." + CONFIG["region"] + ".s3",
        RouteTableIds=[m["lambda_route_table"]],
        PolicyDocument=json.dumps(policy),
        TagSpecifications=[{"ResourceType": "vpc-endpoint", "Tags": tags}],
    )["VpcEndpoint"]
    persist("lambda_s3_endpoint", endpoint["VpcEndpointId"])
if m.get("lambda"):
    lam = AWS.client("lambda")
    config = lam.get_function_configuration(FunctionName=m["lambda"])
    if config.get("VpcConfig", {}).get("SubnetIds") != [m["lambda_subnet"]]:
        lam.update_function_configuration(
            FunctionName=m["lambda"],
            VpcConfig={"SubnetIds": [m["lambda_subnet"]], "SecurityGroupIds": [m["lambda_sg"]]},
        )
        print("Lambda moving to scenario-private subnet", m["lambda_subnet"], flush=True)
        lam.get_waiter("function_updated_v2").wait(FunctionName=m["lambda"])
    evidence(
        "lambda-network-ready.json",
        {
            "subnet": m["lambda_subnet"],
            "route_table": m["lambda_route_table"],
            "endpoint": m["lambda_s3_endpoint"],
            "ownership": "created",
            "scope": "S3 PutObject only on this lab KB source prefix",
        },
    )
print("Scenario-private S3 network ready.", flush=True)
