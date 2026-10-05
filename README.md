# Parking occupancy on AWS Batch (CDK) — prototype runbook

Deploying one CDK stack creates a private test bucket. Uploading pictures plus a `_READY`
file to that bucket starts an AWS Batch **GPU** job that runs the parking-occupancy
pipeline from `rcs-workflow-processor`. No GPU is needed on this PC: it only builds the
container image and uploads pictures.

```
s3://rcs-batch-sim-890742591300/incoming/<run>/*.JPG + _READY   (or _DRYRUN: list only)
  → S3 "Object Created" event → EventBridge rule (matches only incoming/*/_READY and _DRYRUN)
  → Batch job queue → g4dn.xlarge on-demand, Tesla T4 (starts from zero, stops when idle)
  → entry.py: download → RF-DETR + occupancy → results/<run>/<job id>/ in the same bucket
  → CloudWatch Logs: /aws/batch/rcs-batch-sim   (job output)
                     /aws/events/rcs-batch-sim  (job state changes)
```

## What this never touches

| Production / staging | Guarantee |
| --- | --- |
| `rcsstoragebucket`, the staging bucket | Not read or written. The job role has an explicit **Deny on every bucket except the test bucket**, and `entry.py` refuses any other bucket. It never calls `run_parked_car_pipeline_from_s3()` (which uploads to production). |
| Supabase (prod and staging) | No credentials in the job; the pipeline self-calibrates from the images. |
| The current GPU server | Not used. |
| Existing IAM roles, security groups, CDK bootstrap | Not edited. Everything is in the new stack `rcs-batch-sim` and its own bootstrap stack `CDKToolkit-rcs-batch-sim`, all tagged `project=batch-sim`. The account and region are hard-coded in `cdk/app.py`, so a deploy with credentials for another account fails before creating anything. |

## Files

| File | Role |
| --- | --- |
| `config.env` | Profile, account, region, names, local paths (`SRC`, `WEIGHTS`, `FRAMES_DIR`) |
| `preflight.sh` | Read-only checks before deploying (account, permissions, GPU quota, subnets, free names, tools) |
| `prepare_build.sh` | Stages the image's build context in `build/` (pipeline code, rcs_geo, weights) |
| `Dockerfile`, `requirements.txt`, `entry.py` | The job image and its entry point |
| `cdk/` | The CDK app (Python): `app.py`, `batch_sim_stack.py`, settings in `cdk.json` |
| `local_cpu_run.sh` | Runs the image end to end on CPU, offline, on the first N frames (no AWS, no GPU) |
| `tests/`, `pytest.ini`, `requirements-dev.txt` | Offline tests: `entry.py`, the synthesized stack, `preflight.sh` (with a fake `aws`) |
| `reference/` | The CPU reference result for the 20 test frames (`run_summary.json`) |
| `iam/` | What an account admin grants: `ADMIN_REQUEST.md` and the policy for user `ahad` |

**Not in git** (see `.gitignore`):
- the model weights (`*.pth`)
- the staged `build/` folder (a copy of rcs-workflow-processor's code)
- the client's frames and annotated outputs (`*.JPG`, `local_cpu_check/`, `results/`)
- virtualenvs and `cdk.out/`

On a fresh clone, set `SRC`, `WEIGHTS` and `FRAMES_DIR` in `config.env` to where those live
on that machine.

Settings you may change, all in `cdk/cdk.json` → `batchSim`: `instanceTypes`, `maxvCpus`
(4 = one job at a time on g4dn.xlarge), `jobVcpus`, `jobMemoryMiB`, `rootVolumeGiB`,
`jobTimeoutMinutes`. After a change, run `cdk deploy` again.

Expected cost: under $1 for these tests (g4dn.xlarge on-demand ≈ $0.56/hour, billed per
second, one instance for roughly 10–15 minutes per test), plus about $0.50/month for the
image in ECR until cleanup.

Every step ends with a **Check**. Do not continue until it passes.

## How to test this pipeline

Test from the cheapest level to the most expensive. Each level catches what the one before
cannot, and levels 1–3 need no AWS at all.

| Level | What it proves | How | Time | AWS? |
| --- | --- | --- | --- | --- |
| **1. Offline tests** | `entry.py` logic, the stack's security and wiring, `preflight.sh`'s handling of denied checks | `cdk/.venv/bin/python -m pytest` (below) | seconds | no |
| **2. Image checks** | The image builds with the right versions; weights load offline; the pipeline's own tests pass inside it | Step 4 | 10–20 min first time | no |
| **3. CPU end-to-end** | The real pipeline on real frames gives a sensible result: the reference to compare against | `./local_cpu_run.sh 20` (Step 4) | ~2.5 min | no |
| **4. Dry run on Batch** | Upload → trigger → Batch → GPU instance → logs | Step 7 | ~5–10 min | yes |
| **5. GPU run on Batch** | Same result as level 3, plus real timings, memory and cost | Step 8 | ~10–15 min | yes |
| **6. Warm start** | Start-up time when the instance is already running | Step 9 | ~2 min | yes |

**Level 1, the offline tests.** Run these after any change to `entry.py`, `cdk/`, `preflight.sh`:
```bash
cd /home/ubiquify/Documents/rcs-cdk-test
source ~/.nvm/nvm.sh && nvm use 22          # aws-cdk-lib needs Node at synth time
[ -d cdk/.venv ] || python3 -m venv cdk/.venv
cdk/.venv/bin/pip install -q -r cdk/requirements.txt -r requirements-dev.txt
cdk/.venv/bin/python -m pytest
```
Expect `23 passed`. They cover the following:
- **`tests/test_entry.py`:**
  - other buckets and non-marker keys are refused
  - a dry run writes nothing
  - a real run writes `results/<run>/<job id>/run_summary.json` with all timings
  - S3 transfers use production's worker counts
  - a partial upload fails the job, but still writes the summary
  - a missing GPU fails fast
  - nested run names and local mode work
- **`tests/test_stack.py`** synthesizes the stack offline and checks:
  - nothing references `rcsstoragebucket` or Supabase
  - the bucket is private and sends events to EventBridge
  - the job role may use only the test bucket and is denied all others
  - the events role may only submit the job
  - the GPU compute environment scales from 0 to 4 vCPUs on ECS_AL2023_NVIDIA, with a 100 GB root disk
  - the job definition: 1 GPU, 4 vCPUs, 12000 MiB, the right command and parameters
  - the trigger matches only `_READY` and `_DRYRUN`
  - no custom resource installs an SDK at deploy time
  - the bucket and log groups are deleted on `destroy`
- **`tests/test_preflight.py`** runs `preflight.sh` against `tests/fake_aws/aws`, which
  replays the answers AWS gave on 2026-10-05. It checks that:
  - every call uses `--profile rcs`
  - "access denied" is reported as WARN, never as "free"
  - "not found" counts as free
  - the quota need includes the existing GPU server
  - a low quota is a FAIL

**Passing at levels 3 and 5 means:**
- `status: succeeded`
- all 20 frames with telemetry
- `calibration_source` set
- counts that match each other: the 20 test frames gave **19 spots in 2 bays** on CPU (`reference/cpu_20frames_run_summary.json`)

**Next level after these (not yet run): a full flight.** Upload a whole flight folder as a
new run, for example `RUN=fw-full-1`. The pipeline keeps full-resolution frames in memory,
so watch `peak_rss_mb`. If the job is killed with exit code 137, raise `jobMemoryMiB` or move
to `g4dn.2xlarge` (see "If something goes wrong").

---

## Step 1 — Tools on this PC (once)

1. **Docker Desktop** must be running: `docker info` prints server details, not an error.
2. **Node.js 22** (the CDK CLI no longer supports the system Node 18). It is installed with
   nvm. In every new terminal you use for CDK, run:
   ```bash
   source ~/.nvm/nvm.sh && nvm use 22
   ```
   On a fresh machine, install it first:
   `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash && source ~/.nvm/nvm.sh && nvm install 22`.
3. **AWS CLI v2** is installed (`aws --version` → `aws-cli/2.x`). Every AWS and CDK command
   here uses the profile **`rcs`** (access keys in `~/.aws/credentials`, region eu-north-1),
   both through `--profile rcs` and through `AWS_PROFILE=rcs` in `config.env`. The other
   profile on this PC (`labreports`) is a different account and is never used.
   Check: `aws --profile rcs sts get-caller-identity --query Account --output text` → `890742591300`.

**Check:** `node --version` prints `v22.…`; `docker info` works.

## Step 2 — Settings and preflight

```bash
cd /home/ubiquify/Documents/rcs-cdk-test
source config.env          # re-source in every new terminal
./preflight.sh
```

`preflight.sh` changes nothing, and every AWS call in it uses `--profile rcs`. It checks
the following:
- the profile is account `890742591300`, and which IAM user it is
- whether that user may bootstrap, deploy and run the test (`iam simulate-principal-policy`)
- that the on-demand G/VT vCPU quota leaves room for 4 more vCPUs
- that the three subnets are in the VPC, assign public IPs, and offer g4dn.xlarge
- that the bucket, log group, queue and rule names are still free
- that the weights, at least 20 frames and the staged build context are present

Each line is **PASS** (checked, fine), **FAIL** (must be fixed before deploying) or **WARN**
(could not be checked, usually because the user lacks a read permission).

**Check:** no FAIL. Any WARN about permissions or the quota is settled by the admin's two
steps below. If preflight says a name already exists (for example, left by an earlier
manual attempt), delete that resource in the AWS console first, then re-run preflight.

## Before deploying: two things an account admin does once

On 2026-10-05, the `rcs` profile signs in as IAM user `ahad`. That user cannot read the GPU
quota, Batch or CloudWatch Logs, and most likely cannot create IAM roles, which the CDK
bootstrap needs. An account admin settles this once.

### A. GPU quota

The existing GPU server (a g4dn.4xlarge) already uses **16** vCPUs of the quota that Batch
also needs. Batch needs **4** more, so the quota must be **at least 20**. Otherwise the job
waits in `RUNNABLE` forever.

1. Open the AWS console in region **eu-north-1 (Stockholm)**, go to **Service Quotas →
   AWS services → Amazon Elastic Compute Cloud (Amazon EC2)**, and find
   **Running On-Demand G and VT instances** (code `L-DB2E81BA`).
2. If **Applied account-level quota value** is below 20, choose **Request increase at
   account level** and ask for **24** (room for one Batch job plus a spare). Small increases
   are often approved within minutes, but can take a day, so **start this first**.

### B. Bootstrap, and permissions for `ahad`

1. **Run the bootstrap with admin credentials.** The easiest place is **AWS CloudShell** in
   the console (region eu-north-1), which already has Node and the CLI:
   ```bash
   npx -y aws-cdk@2 bootstrap aws://890742591300/eu-north-1 \
     --qualifier rcsbsim --toolkit-stack-name CDKToolkit-rcs-batch-sim --tags project=batch-sim
   ```
   This creates the separate toolkit stack `CDKToolkit-rcs-batch-sim`: its IAM roles
   `cdk-rcsbsim-*`, an S3 asset bucket and an ECR repository. It does not touch any other
   CDK setup in the account.
2. **Give `ahad` the minimal policy** in `iam/ahad-batch-sim-policy.json`, as a managed
   policy. (A user's inline policies share one 2048-character limit, and this one alone is
   1953.) In CloudShell, upload the file first:
   ```bash
   aws iam create-policy --policy-name rcs-batch-sim-ahad \
     --policy-document file://ahad-batch-sim-policy.json --tags Key=project,Value=batch-sim
   aws iam attach-user-policy --user-name ahad \
     --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
   ```
   It allows assuming the `cdk-rcsbsim-*` roles (`cdk deploy` and `cdk destroy` run entirely
   through them), reading and writing the test bucket only, tailing the two log groups, and
   the read-only checks preflight runs.

   Note for the admin: by default the bootstrap's CloudFormation role has
   `AdministratorAccess`. Anyone who can assume `cdk-rcsbsim-deploy-role-…` can therefore
   deploy any stack through it. That is standard CDK practice; remove the policy (and the
   bootstrap stack, see Cleanup) when the test is over.

**Simpler alternative:** the admin grants `ahad` `AdministratorAccess` for the duration of
the test. Then you can run Step 5's bootstrap yourself.

Afterwards, re-run `./preflight.sh`. It should end with `All checks passed.`

## Step 3 — Stage the build context

```bash
./prepare_build.sh
```

**Check:** it prints `weights_sha256: 33ce14c8…` and `Build context ready: …/build (129M)`.
`build/BUILD_INFO.txt` records the `rcs-workflow-processor` commit that goes into the image.
Re-run this step whenever the pipeline code, `entry.py` or the weights change.

## Step 4 — Build and check the image locally (no AWS, ~10–20 min the first time)

*Done on this PC on 2026-10-05:* the image `rcs-batch-sim:check` (11.1 GB) built with every
guard passing; the weights load offline; the repo's test suites pass inside it (78 pipeline
tests, 33 rcs_geo tests with 1 skipped because it needs CUDA); and a full CPU run succeeded
(see "Reference result" below).

This catches image problems before any AWS resource exists. `cdk deploy` reuses the Docker
cache afterwards, so this costs no extra time later.

```bash
docker build --platform linux/amd64 -t rcs-batch-sim:check build/
docker run --rm --network none rcs-batch-sim:check python -c "from rfdetr import RFDETRLarge; RFDETRLarge(pretrain_weights='/app/weights/rcs_weights.pth', num_classes=1, num_queries=300); print('weights load OK, no network needed')"
```

**Check:** the build finishes. It fails on purpose if pip replaced torch 2.6.0, installed a
second OpenCV, installed Ultralytics (AGPL), or if the pipeline or `exiftool` is missing.
The second command, which runs on the CPU with networking off, prints
`weights load OK, no network needed`.

Optional, but recommended after any pipeline change:

- **The pipeline's own tests, inside the image:**
  ```bash
  docker run --rm -v "$SRC":/src:ro rcs-batch-sim:check sh -c "pip install -q pytest && \
    cd /src/parking_occupancy && python -m pytest -q -p no:cacheprovider && \
    cd /src/rcs_geo && python -m pytest -q -p no:cacheprovider"
  ```
  Expect `78 passed`, then `33 passed, 1 skipped` (the skipped test needs CUDA).
- **The full pipeline on CPU, offline, on the 20 test frames** (about 2.5 minutes):
  ```bash
  ./local_cpu_run.sh 20
  ```
  Expect `"status": "succeeded"`, 19 spots in 2 bays, as in `reference/cpu_20frames_run_summary.json`.

## Step 5 — CDK environment and bootstrap (once)

```bash
cd /home/ubiquify/Documents/rcs-cdk-test/cdk
source ~/.nvm/nvm.sh && nvm use 22
[ -d .venv ] || python3 -m venv .venv          # already created on this PC
source .venv/bin/activate && pip install -q -r requirements.txt -r ../requirements-dev.txt
npx -y aws-cdk@2 --version                     # 2.1143.0 or newer

# Separate bootstrap for this prototype. Skip it if the admin already ran it (A/B above), or
# if preflight says CDKToolkit-rcs-batch-sim exists.
npx -y aws-cdk@2 bootstrap --profile rcs aws://890742591300/eu-north-1 \
  --qualifier rcsbsim --toolkit-stack-name CDKToolkit-rcs-batch-sim --tags project=batch-sim
```

**Check:** the bootstrap ends with `✅  Environment aws://890742591300/eu-north-1 bootstrapped.`

## Step 6 — Deploy

```bash
# still in rcs-cdk-test/cdk with the venv active and config.env sourced
npx -y aws-cdk@2 synth --profile rcs > /dev/null && echo synth-ok
npx -y aws-cdk@2 deploy --profile rcs --require-approval broadening
```

The deploy first builds the image from cache, then pushes it to ECR. The push is about
4–5 GB, so it takes as long as your upload speed needs. CloudFormation then creates the
stack in about 5 minutes. Before creating anything, it lists the IAM changes and asks
`y/n`. Every role it lists belongs to this stack: the instance role, job role, execution
role, events role and the custom-resource roles.

**Check:**
```bash
CE_ARN=$(aws --profile rcs cloudformation describe-stacks --stack-name rcs-batch-sim \
  --query "Stacks[0].Outputs[?OutputKey=='ComputeEnvironmentArn'].OutputValue" --output text)
aws --profile rcs batch describe-compute-environments --compute-environments "$CE_ARN" \
  --query 'computeEnvironments[0].[status,state,computeResources.desiredvCpus,statusReason]' --output text
```
This prints `VALID  ENABLED  0  …`. With 0 vCPUs, no server is running and nothing is billed.

## Step 7 — Test A: dry run (proves trigger → Batch → logs)

Open two extra terminals. In each, run
`cd /home/ubiquify/Documents/rcs-cdk-test && source config.env`, then:

```bash
aws --profile rcs logs tail "$EVENTS_LOG_GROUP" --follow --format short   # terminal 1: job states
aws --profile rcs logs tail "$JOB_LOG_GROUP" --follow --format short      # terminal 2: job output
```

Upload the first 20 frames of the June 24 Fort Wayne flight, then the dry-run marker. Run
names may use only `A-Z a-z 0-9 . _ -`.

```bash
RUN=fw-test-1
ls "$FRAMES_DIR" | sort | head -20 | while read -r f; do
  aws --profile rcs s3 cp "$FRAMES_DIR/$f" "s3://$BUCKET/incoming/$RUN/$f" --only-show-errors
done
aws --profile rcs s3 ls "s3://$BUCKET/incoming/$RUN/" | wc -l             # 20
aws --profile rcs s3api put-object --bucket "$BUCKET" --key "incoming/$RUN/_DRYRUN"
```

**Expect:**
- Within seconds a job appears in terminal 1 (`SUBMITTED → RUNNABLE`).
- `STARTING` takes about 3–8 minutes on a cold start, while the instance launches and pulls the image.
- Terminal 2 then shows `job started`, `images listed  count: 20` and `dry run finished`.
- Terminal 1 ends with `SUCCEEDED`.

Job states at any time:
```bash
aws --profile rcs batch list-jobs --job-queue "$JOB_QUEUE" --filters name=JOB_NAME,values=rcs-batch-sim-occupancy \
  --query 'jobSummaryList[].[jobId,status,statusReason]' --output table
```

## Step 8 — Test B: real run on the GPU

```bash
aws --profile rcs s3api put-object --bucket "$BUCKET" --key "incoming/$RUN/_READY"
```

**Expect in terminal 2:**
- `compute` with `gpu_name: Tesla T4`
- `images downloaded  ok: 20`
- the pipeline's own lines: `Found 20 images`, throughput calibration, `[20/20] …`, self-calibration, `=== OCCUPANCY === … spots in … bays`
- the per-phase timing table
- `pipeline finished`, then `job finished` with `status: succeeded`

Read the summary and download the outputs:
```bash
KEY=$(aws --profile rcs s3 ls "s3://$BUCKET/results/$RUN/" --recursive | grep run_summary.json | sort | tail -1 | awk '{print $4}')
aws --profile rcs s3 cp "s3://$BUCKET/$KEY" -
aws --profile rcs s3 cp --recursive "s3://$BUCKET/$(dirname "$KEY")/" "results/$(dirname "${KEY#results/}")/"
```
`occupied_bays.json` holds the bays and spots. `spots/bay*_spot*/` holds each detected
car's annotated frames and thumbnails.

**Check:** `run_summary.json` has `"status": "succeeded"`, `"images_downloaded": 20` and
`"gpu": "Tesla T4"`.

## Step 9 — Warm start (optional, 1 minute)

As soon as Step 8 finishes, while the instance is still up, upload `_READY` again:
```bash
aws --profile rcs s3api put-object --bucket "$BUCKET" --key "incoming/$RUN/_READY"
```
The new job writes to its own `results/$RUN/<new job id>/`. Its `trigger_to_start_s` is the
warm start-up time: no instance launch, and the image is already on the instance.

## Reference result (CPU, this PC, 2026-10-05)

The same 20 frames went through this exact image on CPU, with networking off
(`./local_cpu_run.sh 20`). The summary is committed as
`reference/cpu_20frames_run_summary.json`. The full output (`occupied_bays.json`, spot
images, log) is written to `local_cpu_check/`, which stays out of git because it holds
client imagery and locations.

| | CPU reference |
| --- | --- |
| Result | **19 spots in 2 bays** (10 + 9, each row ≈ 33 m); 20/20 frames with telemetry; 933 detections binned |
| Self-calibration | applied: `features-roof`, scale ×0.8838 → 2.6127 mm/px, heading −1.74° |
| Time | `pipeline_s` 138.5 on 8 CPU cores: detect 45 s, **spot annotations 81 s**, self-calibration 8 s |
| Peak RAM | 2653 MB |

The GPU run in Step 8 should give the same counts. A spot or so of difference can come
from FP16 on the T4 versus FP32 on the CPU, and the per-spot images show any such case.
Writing the spot annotations (full-resolution progressive JPEGs) is CPU work that the GPU
does not speed up, and g4dn.xlarge has 4 vCPUs. Compare that phase in the job log's timing
table.

## Record for the Batch decision

From each `run_summary.json`:

| Field | Meaning |
| --- | --- |
| `trigger_to_start_s` | `_READY` upload → container running (cold in Step 8, warm in Step 9) |
| `timings.download_s` / `pipeline_s` / `upload_s` / `total_s` | Time inside the job; S3 transfers use production's helpers and worker counts |
| `peak_rss_mb`, `gpu_peak_allocated_mb` | Memory, for sizing `jobMemoryMiB` and the instance for full flights |
| `bays`, `spots`, `calibration_source` | The result; `calibration_source` says whether self-calibration applied |

The job log also holds the pipeline's per-phase timing table (`PARKING_PROFILE=1`).

**Comparing with production:** this run always self-calibrates. Production uses the
AprilTag GSD when a flight has an AprilTag run, so compare spot counts only against a
production run of the same frames that also self-calibrated.

**Same-frames baseline on the current GPU server (optional):** copy the same 20 frames and
`entry.py` to the server, then run, outside the services and while it is idle:
```bash
RFDETR_WEIGHTS=/path/to/rcs_weights.pth PYTHONPATH=/path/to/rcs-workflow-processor \
  python entry.py --local-dir frames20 --out /tmp/occ-baseline
```
`/tmp/occ-baseline/run_summary.json` gives `pipeline_s` and the counts for the same frames.

## Changing code or settings

- **Pipeline code, `entry.py` or weights:** `./prepare_build.sh`, then `cdk deploy`. This builds a new image and a new job definition revision.
- **Instance type, memory, concurrency:** edit `cdk/cdk.json`, then `cdk deploy`.

## If something goes wrong

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Deploy: `current credentials are for account …` | Wrong profile | Use `--profile rcs` and `source config.env`; check `aws --profile rcs sts get-caller-identity`; re-run `preflight.sh` |
| Deploy: `SSM parameter /cdk-bootstrap/rcsbsim/version not found` | Bootstrap missing | Step 5 bootstrap |
| Deploy: `… is missing: run ./prepare_build.sh` | No build context | Step 3 |
| Deploy: `… already exists` | A resource with that name was made outside this stack | Delete it in the AWS console (the error names it), then deploy again |
| No job after the marker upload | Wrong key, or the trigger failed | The key must be exactly `incoming/<run>/_READY`; EventBridge console → rule `rcs-batch-sim-trigger` → Monitoring → `FailedInvocations` |
| Job stays `RUNNABLE` > 15 min | No capacity, quota, or compute environment invalid | Step 6 check → `statusReason`; re-run `preflight.sh` (GPU quota) |
| Job `FAILED`, `CannotPullContainerError` | Image pull | `list-jobs` → `statusReason`; re-deploy |
| Exit code 137 / `OutOfMemoryError` | Not enough memory | Raise `jobMemoryMiB` (on g4dn.xlarge stay ≤ 14500, or jobs never leave `RUNNABLE`), or use `g4dn.2xlarge` (32 GB) with `maxvCpus`/`jobVcpus` 8 |
| `run_summary.json` `status: failed` | See `error` in it | The job log has the full traceback (`job failed`) |

## Cleanup (when finished)

```bash
cd /home/ubiquify/Documents/rcs-cdk-test/cdk && source .venv/bin/activate && source ../config.env
npx -y aws-cdk@2 destroy --profile rcs
```
This removes everything in the stack, including the test bucket and its contents, the log
groups, roles, Batch resources and rules.

The bootstrap (which holds the pushed image, about $0.50/month) and `ahad`'s policy are
removed by the **admin**, with admin credentials (for example in CloudShell, eu-north-1):
```bash
aws ecr delete-repository --repository-name cdk-rcsbsim-container-assets-890742591300-eu-north-1 --force
aws cloudformation delete-stack --stack-name CDKToolkit-rcs-batch-sim
aws iam detach-user-policy --user-name ahad --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
aws iam delete-policy --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
```
Then empty and delete the bucket `cdk-rcsbsim-assets-890742591300-eu-north-1` in the S3
console (**Empty**, then **Delete**). It is versioned and kept on purpose by the bootstrap.
Locally: `docker rmi rcs-batch-sim:check; rm -rf ../build cdk.out`.
