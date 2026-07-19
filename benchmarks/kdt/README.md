# Knowledge-at-Decision-Time benchmark

Run the deterministic benchmark with:

```bash
uv run python -m hindsight.benchmarks.kdt
```

`kdt-synthetic-v1` contains 35 controlled scenarios across seven verdict families. It
measures temporal truth and knowledge reconstruction, retrieval attribution, verdicts,
root-cause attribution, provenance, unjustified blame, remediation idempotence, and
procedural-memory reuse. It is a synthetic regression benchmark, not an external model
evaluation. The committed result is reproducible without AWS credentials.
