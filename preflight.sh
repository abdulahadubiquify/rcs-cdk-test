#!/usr/bin/env bash
# Read-only checks before `cdk deploy`. Creates and changes nothing.
#   PASS = checked and fine     FAIL = must be fixed before deploying
#   WARN = could not be checked (usually a missing read permission; see README "Before deploying")
# Usage: ./preflight.sh      Exit code 0 = no FAIL.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/config.env"
PROFILE="${AWS_PROFILE:-rcs}"
aws() { command aws --profile "$PROFILE" "$@"; }

ctx() { python3 -c "import json,sys;v=json.load(open('$HERE/cdk/cdk.json'))['context']['batchSim'][sys.argv[1]];print(' '.join(v) if isinstance(v,list) else v)" "$1"; }
SUBNETS=$(ctx subnetIds); VPC_ID=$(ctx vpcId); INSTANCE_TYPE=$(ctx instanceTypes | cut -d' ' -f1); MAX_VCPUS=$(ctx maxvCpus)
QUALIFIER=rcsbsim

N_FAIL=0; N_WARN=0
pass() { echo "  PASS  $*"; }
fail() { echo "  FAIL  $*"; N_FAIL=$((N_FAIL + 1)); }
warn() { echo "  WARN  $*"; N_WARN=$((N_WARN + 1)); }
info() { echo "  info  $*"; }

# probe CMD...: runs CMD, keeps its output in PROBE_OUT.
#   returns 0 ok, 1 not found, 2 access denied, 3 other error
probe() {
  PROBE_OUT=$("$@" 2>&1); local rc=$?
  [ $rc -eq 0 ] && return 0
  if grep -qiE 'AccessDenied|not authorized|UnauthorizedOperation|AuthorizationError|explicit deny' <<<"$PROBE_OUT"; then return 2; fi
  if grep -qiE 'does not exist|not ?found|NoSuchEntity|ResourceNotFound|ParameterNotFound|\(404\)' <<<"$PROBE_OUT"; then return 1; fi
  return 3
}
denied() { grep -oE 'perform: [A-Za-z0-9-]+:[A-Za-z0-9]+' <<<"$PROBE_OUT" | head -1 | sed 's/perform: //'; }
short() { tr '\n' ' ' <<<"$PROBE_OUT" | cut -c1-200; }

echo "== local tools (profile: $PROFILE)"
command -v aws >/dev/null && pass "aws CLI $(command aws --version 2>&1 | cut -d' ' -f1)" || fail "aws CLI not installed"
docker info >/dev/null 2>&1 && pass "docker daemon reachable" || fail "docker daemon not reachable (start Docker Desktop)"
if command -v node >/dev/null && node -e 'process.exit(parseInt(process.versions.node) >= 20 ? 0 : 1)'; then
  pass "node $(node --version)"
else
  fail "node >= 20 required (found: $(node --version 2>/dev/null || echo none)); run: source ~/.nvm/nvm.sh && nvm use 22"
fi
[ -s "$WEIGHTS" ] && pass "weights $WEIGHTS ($(du -h "$WEIGHTS" | cut -f1))" || fail "weights not found: $WEIGHTS"
N_FRAMES=$(find "$FRAMES_DIR" -maxdepth 1 -type f \( -iname '*.jpg' -o -iname '*.jpeg' \) 2>/dev/null | wc -l)
[ "$N_FRAMES" -ge 20 ] && pass "FRAMES_DIR has $N_FRAMES frames" || fail "FRAMES_DIR has $N_FRAMES frames (need >= 20): $FRAMES_DIR"
[ -f "$HERE/build/weights/rcs_weights.pth" ] && pass "build context staged (build/)" || fail "build/ not staged: run ./prepare_build.sh"

echo "== account"
if ! probe aws sts get-caller-identity --query '[Account,Arn]' --output text; then
  fail "no working credentials for profile '$PROFILE': $(short)"; echo; echo "Fix the credentials first."; exit 1
fi
read -r ACCT ARN <<<"$PROBE_OUT"
if [ "$ACCT" = "$ACCOUNT_ID" ]; then pass "profile '$PROFILE' is account $ACCT as $ARN"; else
  fail "profile '$PROFILE' is account $ACCT, expected $ACCOUNT_ID — stop here"; exit 1
fi

echo "== permissions of $ARN (iam simulate-principal-policy, read-only)"
# simulate RESOURCE ACTION...: prints one line per action; sets SIM_DENIED to the denied actions
simulate() {
  local res=$1; shift
  SIM_DENIED=""
  if ! probe aws iam simulate-principal-policy --policy-source-arn "$ARN" --resource-arns "$res" \
         --action-names "$@" --query 'EvaluationResults[].[EvalActionName,EvalDecision]' --output text; then
    return 1
  fi
  while read -r act dec; do
    [ -z "$act" ] && continue
    [ "$dec" = "allowed" ] || SIM_DENIED="$SIM_DENIED $act"
  done <<<"$PROBE_OUT"
  return 0
}
ROLE_BASE="arn:aws:iam::$ACCOUNT_ID:role/cdk-$QUALIFIER"
SIM_OK=1
if simulate "*" cloudformation:CreateChangeSet cloudformation:ExecuteChangeSet cloudformation:DescribeStacks \
     iam:CreateRole iam:PutRolePolicy iam:AttachRolePolicy iam:PassRole s3:CreateBucket s3:PutBucketPolicy \
     ecr:CreateRepository ecr:SetRepositoryPolicy ssm:PutParameter; then
  [ -z "$SIM_DENIED" ] && pass "has the key permissions to run the CDK bootstrap (README Step 5) yourself" \
    || warn "cannot bootstrap yourself (denied:$SIM_DENIED) — an admin runs the bootstrap once (README \"Before deploying\" B)"
else
  SIM_OK=0; warn "cannot simulate permissions ($(denied || echo error)) — ask an admin to check README \"Before deploying\" B"
fi
if [ $SIM_OK -eq 1 ]; then
  DENIED_ALL=""
  for role in deploy-role file-publishing-role image-publishing-role; do
    simulate "$ROLE_BASE-$role-$ACCOUNT_ID-$AWS_REGION" sts:AssumeRole && DENIED_ALL="$DENIED_ALL${SIM_DENIED:+ assume:$role}"
  done
  [ -z "$DENIED_ALL" ] && pass "can assume the CDK bootstrap roles (deploy / destroy)" \
    || fail "cannot assume CDK bootstrap roles:$DENIED_ALL — needed for cdk deploy (README \"Before deploying\" B)"
  NEED=""
  simulate "arn:aws:s3:::$BUCKET" s3:ListBucket && NEED="$NEED$SIM_DENIED"
  simulate "arn:aws:s3:::$BUCKET/*" s3:PutObject s3:GetObject && NEED="$NEED$SIM_DENIED"
  for lg in "$JOB_LOG_GROUP" "$EVENTS_LOG_GROUP"; do
    simulate "arn:aws:logs:$AWS_REGION:$ACCOUNT_ID:log-group:$lg:*" logs:FilterLogEvents && NEED="$NEED$SIM_DENIED"
  done
  simulate "*" batch:ListJobs batch:DescribeJobs batch:DescribeComputeEnvironments && NEED="$NEED$SIM_DENIED"
  [ -z "$NEED" ] && pass "can upload pictures, read results, tail logs and list jobs" \
    || fail "missing permissions to run the test:$(tr ' ' '\n' <<<"$NEED" | sort -u | tr '\n' ' ') (README \"Before deploying\" B)"
fi

echo "== GPU capacity"
USED=0
if probe aws ec2 describe-instances --filters Name=instance-state-name,Values=pending,running \
      --query 'Reservations[].Instances[].InstanceType' --output text; then
  for t in $PROBE_OUT; do
    case "$t" in g*|vt*)
      v=$(aws ec2 describe-instance-types --instance-types "$t" --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text)
      info "running G/VT instance: $t ($v vCPU)"; USED=$((USED + v));;
    esac
  done
else
  warn "cannot list running instances ($(denied)); G/VT vCPUs in use unknown"
fi
NEEDED=$((USED + MAX_VCPUS))
if probe aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --query Quota.Value --output text; then
  QUOTA=${PROBE_OUT%%.*}
  if [ "$QUOTA" -ge "$NEEDED" ]; then pass "on-demand G/VT quota $QUOTA vCPU >= $USED in use + $MAX_VCPUS for Batch"
  else fail "on-demand G/VT quota $QUOTA vCPU < $USED in use + $MAX_VCPUS for Batch = $NEEDED — request an increase (README \"Before deploying\" A)"; fi
else
  warn "cannot read the quota ($(denied || echo error)). Check it in the console: it must be >= $NEEDED (README \"Before deploying\" A)"
fi

echo "== network"
if probe aws ec2 describe-instance-type-offerings --location-type availability-zone \
      --filters Name=instance-type,Values="$INSTANCE_TYPE" --query 'InstanceTypeOfferings[].Location' --output text; then
  OFFERED=$PROBE_OUT
else OFFERED=""; warn "cannot list $INSTANCE_TYPE offerings ($(denied))"; fi
# shellcheck disable=SC2086
if probe aws ec2 describe-subnets --subnet-ids $SUBNETS \
      --query 'Subnets[].[SubnetId,VpcId,AvailabilityZone,MapPublicIpOnLaunch]' --output text; then
  while read -r sid vpc az pub; do
    [ -z "$sid" ] && continue
    msg="$sid ($az)"
    [ "$vpc" = "$VPC_ID" ] || { fail "$msg is in $vpc, not $VPC_ID"; continue; }
    [ "$pub" = "True" ] || { fail "$msg does not auto-assign public IPs (instances could not reach ECR/S3)"; continue; }
    [ -z "$OFFERED" ] || grep -qw "$az" <<<"$OFFERED" || { fail "$msg: $INSTANCE_TYPE not offered in $az"; continue; }
    pass "$msg in $VPC_ID, public IPs on, $INSTANCE_TYPE offered"
  done <<<"$PROBE_OUT"
else
  fail "cannot read subnets $SUBNETS: $(short)"
fi

echo "== names the stack will create (must be free)"
probe aws cloudformation describe-stacks --stack-name rcs-batch-sim --query 'Stacks[0].StackStatus' --output text
case $? in
  0) STACK=$PROBE_OUT; info "stack rcs-batch-sim exists ($STACK) — deploy will update it";;
  1) STACK=""; pass "stack rcs-batch-sim does not exist yet";;
  2) STACK=""; warn "cannot read stacks ($(denied))";;
  *) STACK=""; warn "stack check failed: $(short)";;
esac
if [ -z "$STACK" ]; then
  probe aws s3api head-bucket --bucket "$BUCKET"
  case $? in
    1) pass "bucket $BUCKET is free";;
    0) fail "bucket $BUCKET already exists (made outside this stack) — delete it first";;
    *) fail "bucket $BUCKET exists but is not yours, or is unreadable: $(short)";;
  esac
  for lg in "$JOB_LOG_GROUP" "$EVENTS_LOG_GROUP"; do
    probe aws logs describe-log-groups --log-group-name-prefix "$lg" --query "logGroups[?logGroupName=='$lg'].logGroupName" --output text
    case $? in
      0) [ -z "$PROBE_OUT" ] && pass "log group $lg is free" || fail "log group $lg already exists — delete it first";;
      2) warn "cannot check log group $lg ($(denied))";;
      *) warn "log group check failed: $(short)";;
    esac
  done
  probe aws batch describe-job-queues --job-queues "$JOB_QUEUE" --query 'jobQueues[].status' --output text
  case $? in
    0) [ -z "$PROBE_OUT" ] && pass "job queue $JOB_QUEUE is free" || fail "job queue $JOB_QUEUE already exists ($PROBE_OUT)";;
    2) warn "cannot check job queue $JOB_QUEUE ($(denied))";;
    *) warn "job queue check failed: $(short)";;
  esac
  for r in rcs-batch-sim-trigger rcs-batch-sim-job-state; do
    probe aws events describe-rule --name "$r"
    case $? in
      1) pass "rule $r is free";;
      0) fail "EventBridge rule $r already exists — delete it first";;
      2) warn "cannot check rule $r ($(denied))";;
      *) warn "rule check failed: $(short)";;
    esac
  done
fi
probe aws logs describe-resource-policies --query 'length(resourcePolicies)' --output text
case $? in
  0) [ "$PROBE_OUT" -lt 10 ] && pass "CloudWatch Logs resource policies: $PROBE_OUT of 10 used" \
       || fail "CloudWatch Logs resource policies: $PROBE_OUT of 10 used (the stack needs one)";;
  2) warn "cannot count CloudWatch Logs resource policies ($(denied)); the stack needs 1 of the 10 allowed";;
  *) warn "resource policy check failed: $(short)";;
esac

echo "== CDK bootstrap"
probe aws cloudformation describe-stacks --stack-name CDKToolkit-rcs-batch-sim --query 'Stacks[0].StackStatus' --output text
case $? in
  0) pass "CDKToolkit-rcs-batch-sim exists ($PROBE_OUT) — skip the bootstrap";;
  1) info "CDKToolkit-rcs-batch-sim not found — the bootstrap is needed (README \"Before deploying\" B or Step 5)";;
  2) warn "cannot read the bootstrap stack ($(denied))";;
  *) warn "bootstrap check failed: $(short)";;
esac

echo
if [ $N_FAIL -eq 0 ] && [ $N_WARN -eq 0 ]; then echo "All checks passed."
elif [ $N_FAIL -eq 0 ]; then echo "No failures, $N_WARN check(s) could not run (WARN) — see README \"Before deploying\" B."
else echo "$N_FAIL check(s) FAILED, $N_WARN could not run — fix the FAILs before deploying."; fi
[ $N_FAIL -eq 0 ]
