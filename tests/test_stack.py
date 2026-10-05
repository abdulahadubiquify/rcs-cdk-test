"""Synthesizes the CDK stack offline (no AWS calls, no Docker build) and checks the template."""

import json
import os
import sys

import pytest

aws_cdk = pytest.importorskip("aws_cdk")  # run with the cdk/.venv (see README "How to test")
from aws_cdk import assertions  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "cdk"))
import batch_sim_stack  # noqa: E402

ACCOUNT, REGION = "890742591300", "eu-north-1"
BUCKET = f"rcs-batch-sim-{ACCOUNT}"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    # A stand-in build context: synth only hashes the folder, it does not run docker.
    build = tmp_path_factory.mktemp("build")
    for rel in ("Dockerfile", "entry.py", "requirements.txt", "weights/rcs_weights.pth",
                "parking_occupancy/parking/pipeline.py", "rcs_geo/__init__.py"):
        path = build / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub")
    batch_sim_stack.BUILD_DIR = str(build)

    context = json.load(open(os.path.join(REPO, "cdk", "cdk.json")))["context"]
    app = aws_cdk.App(context=context, outdir=str(tmp_path_factory.mktemp("cdkout")))
    stack = batch_sim_stack.BatchSimStack(
        app, "RcsBatchSim", stack_name="rcs-batch-sim", settings=context["batchSim"],
        env=aws_cdk.Environment(account=ACCOUNT, region=REGION),
        synthesizer=aws_cdk.DefaultStackSynthesizer(qualifier="rcsbsim"))
    return assertions.Template.from_stack(stack)


def resources(template, kind):
    return list(template.find_resources(kind).values())


def test_missing_build_context_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(batch_sim_stack, "BUILD_DIR", str(tmp_path / "nope"))
    with pytest.raises(RuntimeError, match="prepare_build.sh"):
        batch_sim_stack._check_build_context()


def test_nothing_references_production(template):
    text = json.dumps(template.to_json())
    assert "rcsstoragebucket" not in text
    assert "supabase" not in text.lower()


def test_test_bucket_private_with_eventbridge(template):
    template.has_resource_properties("AWS::S3::Bucket", {
        "BucketName": BUCKET,
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True, "BlockPublicPolicy": True,
                                           "IgnorePublicAcls": True, "RestrictPublicBuckets": True},
    })
    template.has_resource_properties("Custom::S3BucketNotifications", {
        "NotificationConfiguration": {"EventBridgeConfiguration": {}}})


def test_job_role_only_test_bucket_and_denies_the_rest(template):
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {"Statement": assertions.Match.array_with([  # same order as the template
            assertions.Match.object_like({"Sid": "ListTestBucket", "Effect": "Allow", "Action": "s3:ListBucket"}),
            assertions.Match.object_like({"Sid": "ReadWriteTestBucket", "Effect": "Allow",
                                          "Action": ["s3:GetObject", "s3:PutObject"]}),
            assertions.Match.object_like({"Sid": "DenyEveryOtherBucket", "Effect": "Deny", "Action": "s3:*",
                                          "NotResource": assertions.Match.any_value()}),
        ])}})


def test_events_role_can_only_submit_the_job(template):
    for policy in resources(template, "AWS::IAM::Policy"):
        statements = policy["Properties"]["PolicyDocument"]["Statement"]
        if any(s.get("Action") == "batch:SubmitJob" for s in statements):
            assert [s["Action"] for s in statements] == ["batch:SubmitJob"]
            break
    else:
        pytest.fail("no batch:SubmitJob policy found")


def test_gpu_compute_environment_scales_from_zero(template):
    (ce,) = resources(template, "AWS::Batch::ComputeEnvironment")
    cr = ce["Properties"]["ComputeResources"]
    assert cr["InstanceTypes"] == ["g4dn.xlarge"]
    assert cr["MinvCpus"] == 0 and cr["MaxvCpus"] == 4
    assert cr["Ec2Configuration"] == [{"ImageType": "ECS_AL2023_NVIDIA"}]
    assert cr["Type"] == "EC2"  # on-demand
    template.has_resource_properties("AWS::EC2::LaunchTemplate", {"LaunchTemplateData": {"BlockDeviceMappings": [
        {"DeviceName": "/dev/xvda", "Ebs": {"VolumeSize": 100, "VolumeType": "gp3", "Encrypted": True,
                                            "DeleteOnTermination": True}}]}})


def test_job_definition(template):
    (jd,) = resources(template, "AWS::Batch::JobDefinition")
    props = jd["Properties"]
    cp = props["ContainerProperties"]
    assert {r["Type"]: r["Value"] for r in cp["ResourceRequirements"]} == {"GPU": "1", "VCPU": "4", "MEMORY": "12000"}
    assert cp["Command"] == ["python", "/app/entry.py", "--bucket", "Ref::bucket", "--key", "Ref::key",
                             "--event-time", "Ref::eventtime"]
    assert set(props["Parameters"]) == {"bucket", "key", "eventtime"}
    env = {e["Name"] for e in cp["Environment"]}
    assert {"ALLOWED_BUCKET", "AWS_DEFAULT_REGION", "PARKING_PROFILE"} <= env
    assert props["RetryStrategy"] == {"Attempts": 1}
    assert props["Timeout"] == {"AttemptDurationSeconds": 3600}


def test_trigger_matches_only_markers_and_passes_parameters(template):
    rules = {r["Properties"]["Name"]: r["Properties"] for r in resources(template, "AWS::Events::Rule")}
    trigger = rules["rcs-batch-sim-trigger"]
    assert trigger["EventPattern"]["detail"]["object"]["key"] == [
        {"wildcard": "incoming/*/_READY"}, {"wildcard": "incoming/*/_DRYRUN"}]
    transformer = trigger["Targets"][0]["InputTransformer"]
    assert set(transformer["InputPathsMap"].values()) == {"$.detail.bucket.name", "$.detail.object.key", "$.time"}
    for name in ('"bucket"', '"key"', '"eventtime"'):
        assert name in transformer["InputTemplate"]
    assert "rcs-batch-sim-job-state" in rules


def test_custom_resources_do_not_install_sdk_at_deploy_time(template):
    for cr in resources(template, "Custom::CloudwatchLogResourcePolicy"):
        assert cr["Properties"].get("InstallLatestAwsSdk") is False


def test_everything_removable_on_destroy(template):
    for kind in ("AWS::S3::Bucket", "AWS::Logs::LogGroup"):
        for r in resources(template, kind):
            assert r.get("DeletionPolicy") == "Delete", kind
