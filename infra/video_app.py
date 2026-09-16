"""Optional shared media stack; never deployed by the app's bootstrap."""

import os

import aws_cdk as cdk

from stacks.video_stack import LaunchpadVideoStack

app = cdk.App()
LaunchpadVideoStack(
    app,
    "launchpad-videos",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "us-west-2"),
    ),
    description="Shared public Launchpad tutorials with a private S3 origin",
    termination_protection=True,
)
app.synth()
