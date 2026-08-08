# AWS deployment runbook

This runbook deploys the HindSight web application through Amazon ECS Express Mode. It does
not deploy or print credentials. Use a dedicated AWS account when possible, protect the root
user with MFA, and deploy through a short-lived administrator or CI role rather than root
access keys.

## Responsibility split

| Supplied by the operator | Created by CloudFormation |
|---|---|
| AWS account, Region, and deployment identity | Dedicated VPC and two public subnets |
| Runtime CockroachDB secret ARN | ECS cluster and Express Gateway service |
| Immutable application image | Execution, infrastructure, and runtime task roles |
| Optional Bedrock model/profile IDs and exact ARNs | Generated 64-character rate-limit HMAC secret |
| Optional MCP/reset secret ARNs | HTTPS ALB, WAF association, logs, SNS, and alarms |
| Alert email and AWS Budget | Fixed one-task scaling and least-privilege provider policy |

The service stack does not receive `MIGRATION_DATABASE_URL`, AWS access keys, or secret values
through ordinary environment variables.

## 1. Choose and secure the account

1. Choose one Region for ECR, ECS, Secrets Manager, WAF, CloudWatch, and Bedrock.
2. Verify that the selected Bedrock conversation model and Titan Text Embeddings V2 are
   available in that Region or through the intended inference profile.
3. Install and authenticate AWS CLI v2 and Docker. Confirm the account before any write:

   ```bash
   aws sts get-caller-identity
   aws configure get region
   ```

4. Create an account-level monthly cost budget with actual and forecast notifications at
   50%, 80%, and 100%. Add Cost Anomaly Detection. Budgets are delayed alerts, not hard
   spending caps; the application quotas and fixed one-task maximum remain the immediate
   controls.

## 2. Create the runtime database secret

In Secrets Manager, create a secret such as `hindsight/hackathon/database-url`. Its complete
plain-text secret value must be the least-privilege runtime connection URL:

```text
postgresql://hindsight_app:<password>@<host>:26257/hindsight?sslmode=verify-full
```

Do not wrap the URL in JSON. Record only its ARN. If a customer-managed KMS key encrypts it,
also record that key ARN for `SecretsKmsKeyArn`.

Keep the schema-owner URL outside ECS. From a trusted operator environment, run:

```bash
uv run --env-file .env hindsight migrate
```

Migration `011_rate_limits.sql` must be present. Confirm the runtime principal has:

```sql
GRANT SELECT, INSERT, UPDATE
ON TABLE api_rate_limit_buckets
TO hindsight_app;

GRANT SELECT, INSERT, DELETE
ON TABLE api_rate_limit_leases
TO hindsight_app;
```

The current public Express topology assigns dynamic public addresses to tasks. A public
CockroachDB endpoint must therefore already accept the service connection. For a stricter
long-lived environment, use CockroachDB AWS PrivateLink in the dedicated VPC when the cluster
plan supports it. Never weaken TLS verification.

## 3. Create ECR and push an immutable image

Deploy the bootstrap:

```bash
aws cloudformation deploy \
  --template-file deploy/ecr-bootstrap.yaml \
  --stack-name hindsight-ecr
```

Read the `RepositoryUri` output, authenticate Docker to that registry, and push a unique tag
such as the Git commit SHA:

```bash
docker build -t hindsight .
aws ecr get-login-password --region <region> \
  | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker tag hindsight:latest <repository-uri>:<git-sha>
docker push <repository-uri>:<git-sha>
```

Use ECR `describe-images` or the console to obtain the pushed `sha256:...` digest. The service
stack accepts only:

```text
<repository-uri>@sha256:<64 lowercase hexadecimal characters>
```

Do not deploy `latest` or another mutable tag.

## 4. Decide optional integrations

The safe baseline has `BedrockEnabled=false`, `VectorEnabled=false`, and `McpEnabled=false`.
Enable only the integrations needed for the public proof.

When Bedrock or vector retrieval is enabled, provide `BedrockResourceArns` as a comma-separated
list without spaces. Include:

- the conversation model or inference-profile ARN;
- every destination foundation-model ARN required by a cross-Region profile;
- the Titan V2 ARN when vector retrieval is enabled.

For the repository's current `eu.amazon.nova-2-lite-v1:0` profile, retrieve the authoritative
profile and destination model ARNs from the target account instead of guessing them:

```bash
aws bedrock get-inference-profile \
  --region eu-central-1 \
  --inference-profile-identifier eu.amazon.nova-2-lite-v1:0 \
  --query '{profile:inferenceProfileArn,models:models[*].modelArn}'
```

The task policy then grants only `bedrock:InvokeModel` and `bedrock:GetInferenceProfile` on
that list. It never attaches `AmazonBedrockFullAccess`.

MCP additionally requires `BedrockEnabled=true`, `McpClusterId`, and a Secrets Manager secret
whose complete value is `COCKROACH_MCP_API_KEY`. The reset endpoint is absent unless
`DemoResetTokenSecretArn` is supplied.

## 5. Deploy with managed WAF rules in count mode

Count mode lets the operator inspect managed-rule matches without disabling the per-IP and
global rate rules, which always block:

```bash
aws cloudformation deploy \
  --template-file deploy/ecs-express-service.yaml \
  --stack-name hindsight-web \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    EnvironmentName=hackathon \
    ImageIdentifier=<repository-uri>@sha256:<digest> \
    DatabaseSecretArn=<database-secret-arn> \
    AlertEmail=<operations-email> \
    ManagedRulesMode=COUNT
```

Add only the parameters for integrations that are enabled. CloudFormation creates unnamed,
stack-scoped roles, so `CAPABILITY_IAM` is sufficient.

Wait for the stack to reach `CREATE_COMPLETE`, then confirm the SNS email subscription. ECS
Express role propagation can take about a minute; if the first creation reports that a new
role cannot yet be assumed, wait briefly and retry the failed stack operation.

## 6. Verify before sharing the endpoint

Read the `ServiceEndpoint`, `ServiceArn`, `LoadBalancerArn`, log-group, and WAF outputs. Then
verify:

```bash
curl --fail --silent --show-error <service-endpoint>/health
curl --fail --silent --show-error <service-endpoint>/ready
```

Required evidence:

- ECS Express service status is `ACTIVE`;
- exactly one task is running;
- `/health` and `/ready` return success;
- structured requests appear in the application log group;
- WAF logs contain no unexpected managed-rule matches for normal dashboard use;
- repeated expensive requests receive `429` with `Retry-After`;
- a simulated runtime database failure fails protected routes closed with `503`;
- the 5xx and latency alarms show `OK` after metrics arrive;
- no secret value appears in CloudFormation outputs, task environment displays, or logs.

If a secret is rotated, force a new ECS Express deployment. Existing tasks do not receive
updated injected secret values automatically.

## 7. Enforce managed WAF rules

Before making the URL public, redeploy the same stack with:

```text
ManagedRulesMode=BLOCK
```

The Web ACL then provides AWS IP-reputation, known-bad-input, and common protections, a
300-request/minute per-IP ceiling, a 10-request/minute per-IP ceiling on expensive routes, and a
120-request/minute ceiling across all clients on those routes. CockroachDB-backed application
quotas remain the exact shared accounting layer and fail closed if unavailable.

The VPC is dedicated because ECS Express can consolidate multiple Express services behind one
ALB. Do not place unrelated Express services in this VPC: a Web ACL is associated with the ALB
and would consequently protect every service sharing it.

## 8. Submission evidence and cleanup

Capture the immutable image digest, successful stack events, public health/readiness, WAF
blocks, application logs, alarms, and confirmed budget notifications for the hackathon
submission.

The ECR repository, HMAC secret, and log groups use `Retain`. Deleting the stacks therefore
does not delete that evidence or secret material automatically. Inventory those retained
resources explicitly and remove them only after the submission and retention window are over.
