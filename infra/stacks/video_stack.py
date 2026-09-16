"""Environment-independent tutorial media, retained separately from app resources."""

import aws_cdk as cdk
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_s3 as s3
from constructs import Construct


class LaunchpadVideoStack(cdk.Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs):
        super().__init__(scope, construct_id, **kwargs)

        bucket = s3.Bucket(
            self,
            "Media",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            versioned=True,
            removal_policy=cdk.RemovalPolicy.RETAIN,
        )
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "MediaHeaders",
            comment="Anonymous public tutorials, playable across Launchpad environments",
            cors_behavior=cloudfront.ResponseHeadersCorsBehavior(
                access_control_allow_credentials=False,
                access_control_allow_origins=["*"],
                access_control_allow_methods=["GET", "HEAD", "OPTIONS"],
                access_control_allow_headers=["Range"],
                access_control_expose_headers=[
                    "Accept-Ranges", "Content-Range", "Content-Length", "ETag",
                ],
                access_control_max_age=cdk.Duration.hours(1),
                origin_override=True,
            ),
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "Cdn",
            comment="AgentCore Launchpad shared tutorial videos",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_GET_HEAD_OPTIONS,
                cached_methods=cloudfront.CachedMethods.CACHE_GET_HEAD,
                cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                response_headers_policy=headers,
                compress=True,
            ),
            minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            enable_ipv6=True,
        )
        # Keep the OAC, response policy, and bucket policy with the retained CDN.
        # Deleting one of those dependencies would break retained media URLs.
        for resource in self.node.find_all():
            if isinstance(resource, cdk.CfnResource):
                resource.apply_removal_policy(cdk.RemovalPolicy.RETAIN)
        cdk.Tags.of(self).add("Project", "AgentCoreLaunchpad")
        cdk.Tags.of(self).add("Purpose", "SharedTutorialMedia")
        cdk.CfnOutput(self, "BucketName", value=bucket.bucket_name)
        cdk.CfnOutput(self, "DistributionId", value=distribution.distribution_id)
        cdk.CfnOutput(
            self, "BaseUrl", value=f"https://{distribution.distribution_domain_name}"
        )
