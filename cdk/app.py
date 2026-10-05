"""CDK app for the parking-occupancy AWS Batch prototype (stack "rcs-batch-sim").

The account and region are fixed: a deploy with credentials for any other account fails
before anything is created. The stack uses its own bootstrap (qualifier "rcsbsim"), so it
never touches or upgrades another CDK toolkit stack in the account.
"""

import aws_cdk as cdk

from batch_sim_stack import BatchSimStack

ACCOUNT = "890742591300"
REGION = "eu-north-1"
BOOTSTRAP_QUALIFIER = "rcsbsim"

app = cdk.App()
settings = app.node.try_get_context("batchSim")
if not settings:
    raise ValueError('cdk.json is missing the "batchSim" context block')

BatchSimStack(
    app,
    "RcsBatchSim",
    stack_name="rcs-batch-sim",
    settings=settings,
    env=cdk.Environment(account=ACCOUNT, region=REGION),
    synthesizer=cdk.DefaultStackSynthesizer(qualifier=BOOTSTRAP_QUALIFIER),
    description="Parking-occupancy AWS Batch prototype (test bucket, GPU Batch, S3 trigger)",
)
cdk.Tags.of(app).add("project", "batch-sim")

app.synth()
