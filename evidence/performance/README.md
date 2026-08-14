# Performance evidence

This directory contains the offline path from exported HindSight JSONL logs to a
small, deterministic performance report. It does **not** send requests, run a load
test, query CockroachDB, call AWS, or use the network.

The source events are emitted by the application itself. `request_complete` records
server-side application handling duration, status, a stable normalized route, and a UTC
completion timestamp; `performance_span` records bounded component work such as workflow
execution, a CockroachDB checkout, Bedrock Converse, Titan embedding, memory retrieval, or
managed MCP selection. The live logs share one correlation ID so an operator can investigate
a request, but the published aggregate deliberately drops that ID.

## Safety boundary

`scripts/performance_evidence.py` only reads local files and prints JSON to standard
output. Its hard limits are 16 MiB of input, 100,000 lines, 64 KiB per line, and 256
distinct request or span groups. Smaller limits can be supplied on the command line;
the hard limits cannot be raised there.

The report has a closed schema. It retains only:

- normalized request paths, durations, HTTP status counts, and error counts;
- bounded span `component`/`operation` labels, durations, outcomes, and error counts;
- the first and last validated event timestamps, an input SHA-256 digest, and non-sensitive,
  explicitly supplied run metadata.

Correlation IDs, log messages, exception messages, arbitrary fields, query strings,
and raw event bodies are never copied. UUID, numeric, and opaque-ID path segments are
normalized before grouping; every asset name becomes `/assets/{asset}` and every unknown
route becomes `/{other}`. Raw `*.jsonl` files are ignored by Git in this directory.

Every relevant event must carry a canonical correlation UUID and an application-emitted UTC
`observed_at`. The analyzer rejects events outside the declared window, duplicate requests,
spans without a matching completed request, spans that finish after their request, captures
with no completed request, and captures whose completed-request count exceeds `request_cap`.
The declared measurement window must not exceed `duration_cap_seconds`. `test_concurrency` and
`retry_cap` remain explicit load-generator controls: completion logs alone cannot independently
prove those two settings, so retain the authorized workload command alongside the report.

Percentiles use the nearest-rank definition: sort the observations and select
`ceil(percentile * count)`. This makes p50/p95/p99 reproducible without third-party
libraries.

## What a report establishes

A report is a reproducible, sanitized record of the events in one byte-identical source file.
It validates those events against the release configuration, request cap, and UTC window
declared in its metadata. The SHA-256 digest detects a later change to that source file; it does
not independently prove CloudWatch provenance or that an image digest corresponds to a commit.
Publish the deployment record and approved CloudWatch capture with the report. The result is
useful evidence for observed latency, errors, and component attribution in that run; it is not,
by itself, a throughput limit, load-test result, service-level objective, or guarantee for
another environment. No production measurement is committed here until an authorized run has
been captured and reviewed.

## Prepare a report

1. Copy `metadata.template.json`, replace every placeholder, and record the exact
   immutable commit SHA, image digest, UTC measurement window, deployed capacity,
   database/rate-limit pool ceilings and rate-limit scale, server backpressure, provider toggles,
   and test caps. `request_cap` covers every `request_complete` event in the exported window, including
   health/readiness traffic and retries. Do not add credentials, URLs, account IDs, cluster IDs,
   or customer data;
   extra fields are rejected. The template intentionally fails validation until its commit,
   image, Region, and time-window placeholders have all been replaced.
2. Obtain explicit authorization for the remote test and log retrieval. Confirm the
   current CockroachDB and AWS/CloudWatch pricing first, then keep both the workload
   and log time window within the approved caps.
3. After the authorized run and log-delivery completion, export only the approved time window as
   JSONL: one raw HindSight log JSON object per line. Keep the raw file local.
4. Generate the sanitized report locally:

   ```powershell
   .\.venv\Scripts\python.exe scripts\performance_evidence.py `
     --input evidence\performance\authorized-run.jsonl `
     --metadata evidence\performance\metadata.local.json `
     --max-lines 1000 `
     --max-input-bytes 1048576 `
     > evidence\performance\report.json
   ```

Review `report.json` before publishing it. Re-running the command against byte-for-byte
identical logs and identical metadata produces byte-for-byte identical JSON.

## Bounded CloudWatch export (only after authorization)

The following PowerShell shape keeps the retrieval to one explicit time window and a
maximum of 1,000 messages. Fill in the approved values only after checking current
pricing and receiving authorization:

```powershell
$messages = aws logs filter-log-events `
  --log-group-name "<approved-log-group>" `
  --start-time <approved-start-epoch-ms> `
  --end-time <approved-end-epoch-ms> `
  --limit 1000 `
  --no-paginate `
  --query "events[].message" `
  --output json | ConvertFrom-Json

$messages | Set-Content -Encoding utf8 evidence\performance\authorized-run.jsonl
```

This example is documentation, not an instruction to execute it automatically. The
analyzer tolerates unrelated valid JSON log events and counts them as ignored; a
malformed relevant or unrelated JSON line fails the run so incomplete evidence is not
silently accepted.
