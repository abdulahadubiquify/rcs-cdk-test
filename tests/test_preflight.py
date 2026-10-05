"""preflight.sh against a fake aws CLI (tests/fake_aws/aws): no AWS calls.

Guards the bug found on 2026-10-05: an "access denied" answer must never read as "free".
"""

import os
import subprocess

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE = os.path.join(REPO, "tests", "fake_aws")


def run_preflight(scenario):
    env = dict(os.environ, PATH=f"{FAKE}:{os.environ['PATH']}", FAKE_SCENARIO=scenario)
    proc = subprocess.run(["bash", os.path.join(REPO, "preflight.sh")], env=env,
                          capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def denied_run():
    return run_preflight("screenshot")


def test_every_call_uses_the_rcs_profile(denied_run):
    _, out = denied_run
    assert "PROFILE NOT PASSED" not in out and "UNHANDLED" not in out


def test_identity_is_reported(denied_run):
    _, out = denied_run
    assert "PASS  profile 'rcs' is account 890742591300 as arn:aws:iam::890742591300:user/ahad" in out


def test_denied_checks_are_warnings_not_passes(denied_run):
    _, out = denied_run
    assert "WARN  cannot check log group /aws/batch/rcs-batch-sim (logs:DescribeLogGroups)" in out
    assert "WARN  cannot check job queue rcs-batch-sim-queue (batch:DescribeJobQueues)" in out
    assert "WARN  cannot read the quota (servicequotas:GetServiceQuota)" in out
    assert "WARN  cannot simulate permissions (iam:SimulatePrincipalPolicy)" in out
    assert "PASS  log group" not in out and "PASS  job queue" not in out


def test_not_found_answers_are_free(denied_run):
    _, out = denied_run
    assert "PASS  bucket rcs-batch-sim-890742591300 is free" in out
    assert "PASS  rule rcs-batch-sim-trigger is free" in out
    assert "PASS  stack rcs-batch-sim does not exist yet" in out


def test_quota_need_counts_the_existing_gpu_server(denied_run):
    _, out = denied_run
    assert "running G/VT instance: g4dn.4xlarge (16 vCPU)" in out
    assert "it must be >= 20" in out


def test_low_quota_fails_and_missing_bootstrap_rights_warn():
    code, out = run_preflight("b")
    assert "FAIL  on-demand G/VT quota 16 vCPU < 16 in use + 4 for Batch = 20" in out
    assert "WARN  cannot bootstrap yourself" in out
    assert "PASS  can assume the CDK bootstrap roles" in out
    assert "PASS  can upload pictures, read results, tail logs and list jobs" in out
    assert code != 0
