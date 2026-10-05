# Admin request: AWS Batch GPU prototype (parking occupancy)

**Account** 890742591300 · **Region** eu-north-1 (Stockholm) · **Requested by** IAM user `ahad`

We are testing whether the parking-occupancy GPU work can run on AWS Batch. The test
creates one CloudFormation stack, `rcs-batch-sim`: a private test bucket, a Batch GPU queue
(g4dn.xlarge, scales from 0 to 1 instance) and two log groups. It never reads or writes
`rcsstoragebucket` or Supabase. Everything is tagged `project=batch-sim` and is removed
afterwards.

Three things are needed from an admin, once.

## 1. GPU quota (start this first: approval can take up to a day)

Service Quotas → Amazon EC2 → **Running On-Demand G and VT instances** (`L-DB2E81BA`), in
eu-north-1. The existing GPU server (a g4dn.4xlarge) already uses 16 vCPUs, and the test
needs 4 more. If the applied value is **below 20**, please request **24**.

## 2. CDK bootstrap (CloudShell in eu-north-1, admin credentials)

```bash
npx -y aws-cdk@2 bootstrap aws://890742591300/eu-north-1 \
  --qualifier rcsbsim --toolkit-stack-name CDKToolkit-rcs-batch-sim --tags project=batch-sim
```

This creates a separate toolkit stack, `CDKToolkit-rcs-batch-sim`, containing IAM roles
`cdk-rcsbsim-*`, an asset bucket and an ECR repository. It does not modify any existing CDK
bootstrap. By default, its CloudFormation execution role has `AdministratorAccess`, which is
standard for CDK.

## 3. Policy for `ahad` (upload the attached `ahad-batch-sim-policy.json` to CloudShell)

```bash
aws iam create-policy --policy-name rcs-batch-sim-ahad \
  --policy-document file://ahad-batch-sim-policy.json --tags Key=project,Value=batch-sim
aws iam attach-user-policy --user-name ahad \
  --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
```

The policy allows:
- assuming the `cdk-rcsbsim-*` roles (deploy and destroy go through them)
- read/write on the test bucket `rcs-batch-sim-890742591300` only
- tailing the two `rcs-batch-sim` log groups
- read-only checks: Batch and EC2 describes, quota read, simulating `ahad`'s own permissions

## Afterwards (when the test is over)

Once `ahad` has run `cdk destroy`, please remove the bootstrap and the policy:

```bash
aws ecr delete-repository --repository-name cdk-rcsbsim-container-assets-890742591300-eu-north-1 --force
aws cloudformation delete-stack --stack-name CDKToolkit-rcs-batch-sim
aws iam detach-user-policy --user-name ahad --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
aws iam delete-policy --policy-arn arn:aws:iam::890742591300:policy/rcs-batch-sim-ahad
```

Then, in the S3 console, empty and delete the bucket
`cdk-rcsbsim-assets-890742591300-eu-north-1` (it is versioned and is retained by design).
