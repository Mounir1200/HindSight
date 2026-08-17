# Authorized deployment record — 2026-08-17

This file records one authorized AWS deployment and the checks performed against it. It
states only what was observed. Figures that have not been measured are named as missing
rather than estimated.

## Deployed identity

| Item | Value |
|---|---|
| Region | `eu-central-1` (Frankfurt) |
| Stacks | `hindsight-ecr`, `hindsight-web` |
| Deployment profile | `showcase` (one task, fixed by a CloudFormation rule) |
| Commit | `bc4e99d2f14e06d4db39c1458a6c8d3c97c2cf3e` |
| Image | `300034476550.dkr.ecr.eu-central-1.amazonaws.com/hindsight-web@sha256:30ee21ed2e58be0a863962bedfab5bb951f1f7b679fdcce202878a45d5a81dd8` |
| Endpoint | `https://hi-cb63ff7571af4e20bd5d23991ee1dc44.ecs.eu-central-1.on.aws` |
| Managed WAF rules | `COUNT` (rate rules always block) |
| Enhanced observability | disabled (paid Container Insights and dashboard not created) |

The image is referenced by immutable digest. No mutable tag is deployed.

## Integrations exercised

| Integration | Configuration |
|---|---|
| Bedrock conversation | `eu.amazon.nova-2-lite-v1:0`, EU cross-region inference profile |
| Embeddings | `amazon.titan-embed-text-v2:0`, 1024 dimensions |
| Vector retrieval | CockroachDB distributed vector index |
| Managed MCP | CockroachDB Cloud managed MCP, read-only `select_query` |
| Database | CockroachDB Basic, `sslmode=verify-full` |

The task role grants `bedrock:InvokeModel` and `bedrock:GetInferenceProfile` on the named
inference profile, its six destination model ARNs, and the Titan ARN. Wildcards are rejected
by the template parameter pattern.

## Checks performed

Liveness and readiness against the public endpoint:

```json
{"status":"ok","backend":"cockroachdb","database":"unchecked"}
{"status":"ready","backend":"cockroachdb","database":"reachable"}
```

A full audit was then driven from the deployed dashboard: workspace preparation followed by
one audit execution. The result reproduced the learning proof on the deployed service:

- reused cases `0 -> 1`;
- four procedural steps loaded from a similar incident;
- recommendation changed after memory;
- root cause confirmed;
- retrieval method reported as **distributed vector index**, rank `#1`.

## Observed durations

These are **single observations** read from the deployed service's structured logs, not an
aggregate report. They are recorded to document orders of magnitude, not to claim percentiles.

| Operation | Observed |
|---|---|
| `GET /health` (load-balancer probe) | 0.61–0.96 ms |
| `GET /ready` (first call, cold pool) | 644.29 ms |
| `connection.checkout` (cold, TLS handshake included) | 495.95 ms |
| `connection.checkout` (warm) | 7.10–31.83 ms |
| `rate-limit.consume` | 37.27–111.36 ms |
| `rate-limit.acquire-lease` | 21.89–38.57 ms |
| `rate-limit.release-lease` | 12.10 ms |
| `GET /demo/workspace` | 80.42 ms |
| `POST /demo/seed` (full audit, all providers enabled) | 8 519.91 ms, status 200 |

The cold checkout cost is a consequence of the deliberately lazy pool (`min_size=0`), which
keeps idle cost at zero in the showcase profile.

### Local reference run, for contrast only

The same image was exercised against the same CockroachDB cluster from a developer machine
outside the cluster region before deployment. `POST /demo/seed` completed in 15 022 ms there,
of which roughly 5 s were the cumulative cost of 38 CockroachDB checkouts at 50–250 ms each.
Those figures describe a laptop-to-Frankfurt path and must not be published as deployed
performance. The per-operation checkout design is latency-sensitive by construction, which is
why the service and the cluster share a region.

The same audit took 8 520 ms once deployed beside the cluster — a 43 % reduction against the
same image, same cluster, and same provider configuration. The difference is network distance
alone, and it is the measured justification for co-locating the service with its database.

Provider calls counted in that reference run: 3 Bedrock `converse`, 2 Titan `embed`, 1 managed
MCP `select`. The third embedding is absent because indexing short-circuits when the memory
content digest is unchanged.

## Not yet established

The following are deliberately absent and must not be claimed until measured:

- p50/p95/p99 for any route on the deployed service; the seed figure above is one observation,
  not a distribution;
- throughput, concurrent-user, or sustained-load figures;
- behaviour with more than one ECS task, which the showcase profile fixes at one.

Producing the percentile report requires exporting the deployed JSONL logs and running
[`scripts/performance_evidence.py`](../scripts/performance_evidence.py) under the procedure in
[`evidence/performance/README.md`](performance/README.md). That script performs no network,
AWS, CockroachDB, or load-generation work.

## Retained resources

`delete-stack` does not remove everything. The ECR repository, both log groups, and the
generated rate-limit HMAC secret carry `DeletionPolicy: Retain`. They must be inventoried and
removed by hand once the evidence period ends, otherwise they keep accruing charges.
