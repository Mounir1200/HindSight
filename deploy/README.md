# AWS deployment runbook

This runbook deploys the HindSight web application through Amazon ECS Express Mode. It does
not deploy or print credentials. Use a dedicated AWS account when possible, protect the root
user with MFA, and deploy through a short-lived administrator or CI role rather than root
access keys.

The template has two explicit profiles. `showcase` is a public, synthetic, one-task default
with a bounded-cost posture: fixed task capacity plus application/provider quotas. `production`
is a multi-task, authenticated stack for one organization. Production here means an isolated
deployment boundary; it does not turn the public demo API into a shared multi-tenant SaaS.

## Responsibility split

| Supplied by the operator | Created by CloudFormation |
|---|---|
| AWS account, Region, and deployment identity | Dedicated VPC and two public subnets |
| Runtime CockroachDB secret ARN and database scope | ECS cluster and Express Gateway service |
| Immutable application image | Execution, infrastructure, and runtime task roles |
| Optional Bedrock model/profile IDs and exact ARNs | Generated 64-character rate-limit HMAC secret |
| Production application-key secret ARN; optional MCP/reset secret ARNs | HTTPS ALB, WAF association, and retained logs; SNS/alarms/dashboard only when configured |
| Alert email, AWS Budget, and approved capacity range | Profile-selected bounded scaling and least-privilege provider policy |

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
   spending caps; the selected maximum task count, application quotas, provider budget, and
   provider concurrency leases are the immediate controls. WAF, log ingestion, data transfer,
   and external database charges are not a hard monetary cap, so check their current prices and
   obtain approval before creating resources or running remote tests.

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

Migrations `011_rate_limits.sql` and `012_demo_workspaces.sql` must be present. Confirm the
runtime principal has:

```sql
GRANT SELECT, INSERT, UPDATE
ON TABLE api_rate_limit_buckets
TO hindsight_app;

GRANT SELECT, INSERT, DELETE
ON TABLE api_rate_limit_leases
TO hindsight_app;

GRANT SELECT, INSERT, UPDATE
ON TABLE demo_workspaces
TO hindsight_app;
```

`demo_workspaces` is the shared state machine for `empty`, `prepared`, `running`, and
`completed` demo states. Atomic version/lease transitions prevent two replicas from executing
the same prepared workspace and permit recovery after an execution lease expires. Do not deploy
more than one task until migration 012 and its runtime grants are verified.

The ECS template sets `HINDSIGHT_STARTUP_READINESS_CHECK=true`. A task therefore validates both
the application pool and the distributed rate-limit tables during startup and fails before
serving if the URL, grants, or migrations are invalid. Ongoing ALB liveness remains the
dependency-free `/health` endpoint to avoid database incidents causing a restart storm.

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

## 4. Select a profile and optional integrations

Choose the profile before calculating cost or creating the service:

| Parameter | `showcase` | `production` |
|---|---|---|
| Intended boundary | Public synthetic proof | One isolated organization stack |
| `MinTaskCount` / `MaxTaskCount` | `1` / `1` by default | Both must be at least `2`; set an approved upper bound |
| `ApplicationApiKeySecretArn` | Optional | Required by a CloudFormation rule |
| Database pool per task | Lazy `0` minimum, `5` maximum by default | Explicitly size within the organization's connection budget |
| Rate-limit pool per task | Lazy `0` minimum, `5` maximum by default | Keep as a separate bulkhead and size within the same connection budget |
| `RateLimitScale` / `ProviderConcurrency` | Fixed at `1` / `4` | Select reviewed values within traffic and provider-spend budgets |
| `EnhancedObservability` | `false` by default; enable only after cost approval | Required; enables Container Insights and the CloudWatch operations dashboard |

For production, create a Secrets Manager secret whose complete plain-text value is an
unguessable 32–1024 byte API key without whitespace. Pass its ARN as
`ApplicationApiKeySecretArn`; the task receives it as `HINDSIGHT_API_KEY`. Clients send
`Authorization: Bearer <key>` to business routes and `/ready`. Authentication compares digests
in constant time and does not log the credential. Dependency-free `/health`, the static
dashboard shell at `/`, and its `/assets/*` files remain public; the shell contains no business
data, while `/ready` and every current or future API route require the key. Use a different
application key, runtime database credential, WAF, logs, quotas, and roles for every organization
stack.

The built-in browser UI is therefore a showcase shell in the production profile: it does not
embed the deployment-wide Bearer key in JavaScript. Use authenticated API clients, or place an
organization identity/session gateway in front of the stack before offering an interactive
production UI.

The business connection pool is process-wide and bounded per task. The template defaults to
zero eager connections, five maximum connections, a two-second checkout timeout, and a
900-second maximum connection lifetime. Repository adapters check out a connection for one
database operation and return it before Bedrock, embedding, or MCP waits. The shared rate
limiter deliberately uses a separate, bounded pool so request admission remains
a bulkhead rather than competing with business operations. Its default maximum is also five.
Before increasing capacity, verify that
`MaxTaskCount × (DatabasePoolMaxSize + RateLimitPoolMaxSize)` fits the CockroachDB
connection budget; both pools are lazy, so that formula is a hard ceiling rather than an idle
connection count.

Each task also has explicit overload backpressure: Uvicorn accepts at most 256 concurrent
connections by default and keeps a bounded backlog of 512. Tune `ServerLimitConcurrency` and
`ServerBacklog` together with task CPU/memory and autoscaling; do not replace those ceilings with
an unbounded queue. Keep-alive is five seconds. Graceful shutdown uses Fargate's 120-second
maximum; because the synchronous showcase audit can run longer, a task replacement may still
interrupt it. The durable workspace and leases make that interruption recoverable after their
bounded expiry, but this is not claimed as uninterrupted in-flight execution.

The showcase rule fixes task capacity at `1/1`; production uses reviewed capacity steps
(`2`, `4`, `8`, `16`, or `20` where applicable) and rejects a maximum below the minimum.
Business-pool sizes use the same explicit-step approach and reject `min > max`. This prevents a
stale parameter file from silently turning the bounded showcase into a large paid deployment.

Application quotas are also deployment inputs rather than hidden code changes. `RateLimitScale`
multiplies the reviewed request and provider budgets, and `ProviderConcurrency` bounds live
Bedrock/Titan work across every task through the shared CockroachDB lease. The showcase profile
fixes them at `1` and `4`. Production accepts a rate scale of `0.1`, `0.25`, `0.5`, `1`, `2`,
`5`, or `10`, and provider concurrency of `1`, `2`, `4`, `8`, `16`, `32`, or `64`; increase
either only after CockroachDB and provider costs have been approved and the immutable release has
been measured at the intended traffic shape.

`EnhancedObservability=false` keeps Container Insights, CloudWatch alarms, and the paid
dashboard off in the showcase by default. Enable it only after the observability cost is
approved. The production profile requires it so latency, errors, task capacity, and saturation
can be evidenced rather than asserted.

Autoscaling defaults to `REQUEST_COUNT_PER_TARGET` rather than CPU alone because HindSight is
primarily I/O-bound on CockroachDB and optional providers. The default target is 300 requests
per target for the metric's evaluation period; tune it only from observed latency, pool waits,
error rate, and task utilization captured for the immutable release.

The safe baseline has `BedrockEnabled=false`, `VectorEnabled=false`, and `McpEnabled=false`.
Enable only the integrations needed for the public proof.

When Bedrock or vector retrieval is enabled, provide `BedrockResourceArns` as a comma-separated
list without spaces. Wildcards (`*` and `?`) are rejected by the parameter pattern, so the
policy cannot silently widen to every model in the account. Include:

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
global rate rules, which always block. Counted matches are written to the WAF log group with
the same field redactions as blocks, so inspect them there rather than enabling sampled
requests — the console sample viewer does not apply `RedactedFields`:

```bash
aws cloudformation deploy \
  --template-file deploy/ecs-express-service.yaml \
  --stack-name hindsight-web \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
    EnvironmentName=hackathon \
    ImageIdentifier=<repository-uri>@sha256:<digest> \
    DatabaseSecretArn=<database-secret-arn> \
    DeploymentProfile=showcase \
    ManagedRulesMode=COUNT
```

For an isolated production stack, replace the showcase profile override and add the following
values after the capacity and connection budget have been approved:

```text
DeploymentProfile=production
MinTaskCount=2
MaxTaskCount=<approved-one-of-2-4-8-16-20>
ApplicationApiKeySecretArn=<organization-api-key-secret-arn>
EnhancedObservability=true
```

Add `AlertEmail` only when enhanced observability is enabled and an operator will confirm the
subscription. Add only the parameters for integrations that are enabled. CloudFormation creates
unnamed, stack-scoped roles, so `CAPABILITY_IAM` is sufficient.

Wait for the stack to reach `CREATE_COMPLETE`, then confirm the SNS email subscription when an
alert address was supplied. ECS Express role propagation can take about a minute; if the first
creation reports that a new role cannot yet be assumed, wait briefly and retry the failed stack
operation.

## 6. Verify before sharing the endpoint

Read the `ServiceEndpoint`, `ServiceArn`, `LoadBalancerArn`, log-group, and WAF outputs. Then
verify:

```bash
curl --fail --silent --show-error <service-endpoint>/health
curl --fail --silent --show-error \
  --header "Authorization: Bearer <production-key-if-enabled>" \
  <service-endpoint>/ready
```

Required evidence:

- ECS Express service status is `ACTIVE`;
- the running/desired task count matches the selected profile (`1` for the bounded showcase,
  at least `2` for production);
- `/health` returns success without credentials and `/ready` succeeds with the production
  credential when authentication is enabled;
- production business routes reject missing/incorrect Bearer credentials and accept the
  configured credential without exposing it in logs;
- the task's public address does **not** answer directly on port 8000, so no path bypasses the
  Web ACL (resolve the task ENI, then confirm `curl http://<task-public-ip>:8000/health` times
  out);
- structured requests appear in the application log group;
- WAF logs contain no unexpected managed-rule matches for normal dashboard use;
- repeated expensive requests receive `429` with `Retry-After`;
- a simulated runtime database failure fails protected routes closed with `503`;
- when `EnhancedObservability=true`, the 5xx and latency alarms show `OK` after metrics arrive,
  and the operations dashboard shows requests, target 4xx/5xx, p95/p99 latency, CPU/memory,
  task capacity, and target health;
- no secret value appears in CloudFormation outputs, task environment displays, or logs.

If a secret is rotated, force a new ECS Express deployment. Existing tasks do not receive
updated injected secret values automatically.

## 7. Enforce managed WAF rules

Before making the URL public, redeploy the same stack with:

```text
ManagedRulesMode=BLOCK
```

The Web ACL then provides AWS IP-reputation, known-bad-input, and common protections, a
300-request/minute per-IP ceiling, a 10-request/minute per-IP ceiling on expensive routes
(`POST /demo/seed`, `POST /demo/reset`, `POST /demo/prepare`, `GET /memories/search`), and a
120-request/minute ceiling across all clients on those routes. CockroachDB-backed application
quotas remain the exact shared accounting layer and fail closed if unavailable.

The VPC is dedicated because ECS Express can consolidate multiple Express services behind one
ALB. Do not place unrelated Express services in this VPC: a Web ACL is associated with the ALB
and would consequently protect every service sharing it.

## 8. Submission evidence and cleanup

Capture the immutable image digest, successful stack events, selected profile and capacity,
public health/readiness, production authentication result when applicable, WAF blocks,
application logs, alarms/dashboard, and confirmed budget notifications for the hackathon
submission. This proves one release; the template or a local test result is not deployment
evidence.

For performance evidence, export only an approved, bounded CloudWatch time window and process
it locally with `scripts/performance_evidence.py` as documented in
`evidence/performance/README.md`. The sanitizer reports request/span counts, status/error counts,
and nearest-rank p50/p95/p99 durations while dropping correlation IDs, messages, arbitrary
fields, and raw events. It rejects empty or truncated-looking captures, timestamps outside the
declared UTC window, request counts above the declared cap, and spans not correlated to a
completed request. Do not run a remote test or retrieve paid logs until current AWS and
CockroachDB costs have been checked and explicitly authorized; do not publish numbers that have
not been captured.

The ECR repository, HMAC secret, and log groups use `Retain`. Deleting the stacks therefore
does not delete that evidence or secret material automatically. Inventory those retained
resources explicitly and remove them only after the submission and retention window are over.
