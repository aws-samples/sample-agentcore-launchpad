import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.video_stack import LaunchpadVideoStack


@pytest.fixture(scope="module")
def template() -> Template:
    return Template.from_stack(
        LaunchpadVideoStack(
            cdk.App(), "test-videos",
            env=cdk.Environment(account="111111111111", region="us-west-2"),
        )
    )


def test_origin_is_private_versioned_and_retained(template: Template):
    template.has_resource(
        "AWS::S3::Bucket",
        {
            "DeletionPolicy": "Retain",
            "Properties": Match.object_like({
                "PublicAccessBlockConfiguration": {
                    "BlockPublicAcls": True, "BlockPublicPolicy": True,
                    "IgnorePublicAcls": True, "RestrictPublicBuckets": True,
                },
                "VersioningConfiguration": {"Status": "Enabled"},
                "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]},
            }),
        },
    )
    template.has_resource_properties(
        "AWS::CloudFront::OriginAccessControl",
        {"OriginAccessControlConfig": Match.object_like({
            "OriginAccessControlOriginType": "s3",
            "SigningBehavior": "always", "SigningProtocol": "sigv4",
        })},
    )
    policies = template.find_resources("AWS::S3::BucketPolicy")
    statements = next(iter(policies.values()))["Properties"]["PolicyDocument"]["Statement"]
    allows = [s for s in statements if s["Effect"] == "Allow"]
    assert len(allows) == 1
    assert allows[0]["Principal"] == {"Service": "cloudfront.amazonaws.com"}
    assert allows[0]["Action"] == "s3:GetObject"
    assert "AWS:SourceArn" in allows[0]["Condition"]["StringEquals"]


def test_cdn_read_only_https_and_cross_environment_cors(template: Template):
    template.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {"DistributionConfig": Match.object_like({
            "DefaultCacheBehavior": Match.object_like({
                "AllowedMethods": ["GET", "HEAD", "OPTIONS"],
                "ViewerProtocolPolicy": "redirect-to-https",
            }),
        })},
    )
    template.has_resource_properties(
        "AWS::CloudFront::ResponseHeadersPolicy",
        {"ResponseHeadersPolicyConfig": Match.object_like({
            "CorsConfig": {
                "AccessControlAllowCredentials": False,
                "AccessControlAllowOrigins": {"Items": ["*"]},
                "AccessControlAllowMethods": {"Items": ["GET", "HEAD", "OPTIONS"]},
                "AccessControlAllowHeaders": {"Items": ["Range"]},
                "AccessControlExposeHeaders": {
                    "Items": ["Accept-Ranges", "Content-Range", "Content-Length", "ETag"],
                },
                "AccessControlMaxAgeSec": 3600,
                "OriginOverride": True,
            },
        })},
    )


def test_media_stack_does_not_create_app_resources(template: Template):
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::IAM::Role", 0)
    template.resource_count_is("AWS::Lambda::Function", 0)
    for resource in template.to_json()["Resources"].values():
        assert resource["DeletionPolicy"] == "Retain"
        assert resource["UpdateReplacePolicy"] == "Retain"
