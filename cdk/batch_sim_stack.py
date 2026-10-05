"""The parking-occupancy AWS Batch prototype as one CDK stack.

    s3://rcs-batch-sim-<account>/incoming/<run>/*.jpeg + _READY (or _DRYRUN)
      -> S3 "Object Created" event -> EventBridge rule (matches only those two marker keys)
      -> Batch job queue -> GPU instance (starts from zero, stops when idle)
      -> entry.py: download -> RF-DETR + occupancy -> results/<run>/<job id>/ in the same bucket
      -> CloudWatch Logs: /aws/batch/rcs-batch-sim (job output), /aws/events/rcs-batch-sim (job states)

Everything is new and lives in this stack; `cdk destroy` removes all of it, test bucket included.
The job role may use the test bucket only: an explicit Deny covers every other bucket.
"""

import os

from aws_cdk import (
    Annotations,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Size,
    Stack,
    aws_batch as batch,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_events as events,
    aws_events_targets as targets,
    aws_iam as iam,
    aws_logs as logs,
    aws_s3 as s3,
)
from constructs import Construct

BUILD_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "build"))

BUCKET_NAME = "rcs-batch-sim-{account}"
JOB_QUEUE_NAME = "rcs-batch-sim-queue"
JOB_DEFINITION_NAME = "rcs-batch-sim-occupancy"
JOB_LOG_GROUP = "/aws/batch/rcs-batch-sim"
EVENTS_LOG_GROUP = "/aws/events/rcs-batch-sim"
MARKER_KEYS = ["incoming/*/_READY", "incoming/*/_DRYRUN"]


def _check_build_context():
    for rel in ("Dockerfile", "entry.py", "requirements.txt", "weights/rcs_weights.pth",
                "parking_occupancy/parking/pipeline.py", "rcs_geo/__init__.py"):
        if not os.path.isfile(os.path.join(BUILD_DIR, rel)):
            raise RuntimeError(f"{BUILD_DIR}/{rel} is missing: run ./prepare_build.sh before cdk synth/deploy")


class BatchSimStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, settings: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        _check_build_context()

        # --- test bucket: private, EventBridge on, 30-day expiry, emptied and deleted on destroy
        bucket = s3.Bucket(
            self, "Bucket",
            bucket_name=BUCKET_NAME.format(account=self.account),
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            event_bridge_enabled=True,
            lifecycle_rules=[s3.LifecycleRule(id="expire-after-30-days", expiration=Duration.days(30))],
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )

        job_logs = logs.LogGroup(self, "JobLogs", log_group_name=JOB_LOG_GROUP,
                                 retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY)
        event_logs = logs.LogGroup(self, "EventLogs", log_group_name=EVENTS_LOG_GROUP,
                                   retention=logs.RetentionDays.TWO_WEEKS, removal_policy=RemovalPolicy.DESTROY)

        # --- network: the existing default VPC's public subnets (outbound only, no inbound rules)
        vpc = ec2.Vpc.from_vpc_attributes(self, "Vpc", vpc_id=settings["vpcId"],
                                          availability_zones=settings["availabilityZones"])
        subnets = [ec2.Subnet.from_subnet_id(self, f"Subnet{i}", sid) for i, sid in enumerate(settings["subnetIds"])]
        for subnet in subnets:  # only the subnet ids are used; route tables are never read
            Annotations.of(subnet).acknowledge_warning("@aws-cdk/aws-ec2:noSubnetRouteTableId")
        security_group = ec2.SecurityGroup(
            self, "WorkerSg", vpc=vpc, allow_all_outbound=True,
            description="rcs-batch-sim Batch GPU workers - no inbound")

        # --- compute: on-demand GPU instances, scale 0 -> maxvCpus -> 0
        instance_role = iam.Role(
            self, "InstanceRole",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AmazonEC2ContainerServiceforEC2Role")])
        launch_template = ec2.LaunchTemplate(
            self, "WorkerLaunchTemplate",
            block_devices=[ec2.BlockDevice(
                device_name="/dev/xvda",  # root volume of the ECS AL2023 GPU AMI (image + pictures + outputs)
                volume=ec2.BlockDeviceVolume.ebs(settings["rootVolumeGiB"], volume_type=ec2.EbsDeviceVolumeType.GP3,
                                                 encrypted=True, delete_on_termination=True))])
        compute_env = batch.ManagedEc2EcsComputeEnvironment(
            self, "GpuComputeEnv",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnets=subnets),
            security_groups=[security_group],
            instance_role=instance_role,
            instance_types=[ec2.InstanceType(t) for t in settings["instanceTypes"]],
            use_optimal_instance_classes=False,
            images=[batch.EcsMachineImage(image_type=batch.EcsMachineImageType.ECS_AL2023_NVIDIA)],
            allocation_strategy=batch.AllocationStrategy.BEST_FIT_PROGRESSIVE,
            minv_cpus=0,
            maxv_cpus=settings["maxvCpus"],
            spot=False,
            launch_template=launch_template,
        )
        job_queue = batch.JobQueue(
            self, "JobQueue", job_queue_name=JOB_QUEUE_NAME, priority=1,
            compute_environments=[batch.OrderedComputeEnvironment(compute_environment=compute_env, order=1)])

        # --- job: what entry.py may do -- the test bucket only, everything else denied
        job_role = iam.Role(self, "JobRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        job_role.add_to_policy(iam.PolicyStatement(
            sid="ListTestBucket", actions=["s3:ListBucket"], resources=[bucket.bucket_arn]))
        job_role.add_to_policy(iam.PolicyStatement(
            sid="ReadWriteTestBucket", actions=["s3:GetObject", "s3:PutObject"],
            resources=[bucket.arn_for_objects("*")]))
        job_role.add_to_policy(iam.PolicyStatement(
            sid="DenyEveryOtherBucket", effect=iam.Effect.DENY, actions=["s3:*"],
            not_resources=[bucket.bucket_arn, bucket.arn_for_objects("*")]))

        image = ecr_assets.DockerImageAsset(self, "Image", directory=BUILD_DIR,
                                            platform=ecr_assets.Platform.LINUX_AMD64)
        job_definition = batch.EcsJobDefinition(
            self, "JobDefinition",
            job_definition_name=JOB_DEFINITION_NAME,
            container=batch.EcsEc2ContainerDefinition(
                self, "Container",
                image=ecs.ContainerImage.from_docker_image_asset(image),
                cpu=settings["jobVcpus"],
                memory=Size.mebibytes(settings["jobMemoryMiB"]),
                gpu=1,
                command=["python", "/app/entry.py", "--bucket", "Ref::bucket", "--key", "Ref::key",
                         "--event-time", "Ref::eventtime"],
                job_role=job_role,
                environment={
                    "ALLOWED_BUCKET": bucket.bucket_name,
                    "AWS_DEFAULT_REGION": self.region,
                    "PARKING_PROFILE": "1",  # the pipeline prints its per-phase timings
                },
                logging=ecs.LogDriver.aws_logs(stream_prefix="occupancy", log_group=job_logs),
            ),
            parameters={"bucket": bucket.bucket_name, "key": "incoming/manual/_DRYRUN", "eventtime": "none"},
            retry_attempts=1,
            timeout=Duration.minutes(settings["jobTimeoutMinutes"]),
            propagate_tags=True,
        )

        # --- trigger: a marker upload submits one job with the bucket, key and event time
        events.Rule(
            self, "TriggerRule",
            rule_name="rcs-batch-sim-trigger",
            event_pattern=events.EventPattern(
                source=["aws.s3"],
                detail_type=["Object Created"],
                detail={"bucket": {"name": [bucket.bucket_name]},
                        "object": {"key": [{"wildcard": k} for k in MARKER_KEYS]}}),
            targets=[targets.BatchJob(
                job_queue.job_queue_arn, job_queue,
                job_definition.job_definition_arn, job_definition,
                job_name="rcs-batch-sim-occupancy",
                event=events.RuleTargetInput.from_object({"Parameters": {
                    "bucket": events.EventField.from_path("$.detail.bucket.name"),
                    "key": events.EventField.from_path("$.detail.object.key"),
                    "eventtime": events.EventField.from_path("$.time"),
                }}))],
        )

        # --- job state changes (SUBMITTED ... SUCCEEDED/FAILED) into their own log group
        events.Rule(
            self, "JobStateRule",
            rule_name="rcs-batch-sim-job-state",
            event_pattern=events.EventPattern(
                source=["aws.batch"],
                detail_type=["Batch Job State Change"],
                detail={"jobQueue": [job_queue.job_queue_arn]}),
            targets=[targets.CloudWatchLogGroup(event_logs)],
        )

        CfnOutput(self, "BucketName", value=bucket.bucket_name)
        CfnOutput(self, "JobQueueName", value=job_queue.job_queue_name)
        CfnOutput(self, "JobDefinitionArn", value=job_definition.job_definition_arn)
        CfnOutput(self, "ComputeEnvironmentArn", value=compute_env.compute_environment_arn)
        CfnOutput(self, "ImageUri", value=image.image_uri)
        CfnOutput(self, "JobLogGroup", value=job_logs.log_group_name)
        CfnOutput(self, "EventsLogGroup", value=event_logs.log_group_name)
