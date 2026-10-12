# Bounded v2 E2E runner

The runner only reads v2 release state and the two v2 Deployments. It never writes release state, receives SQS messages, promotes, rolls back, or changes weight.

Modes:
- `observe`: no inference traffic; watches a new pending/active/future version. A completed or failed version cannot be replayed. PASS requires all four stage audit records and exact terminal state.
- `baseline`: explicitly bounded synthetic requests against the current idle Stable. Requires exact completed Deployment rollout (version, artifact, digest), validates payload against live model metadata, and aborts when a release starts.
- `policy-test`: explicit `allow_synthetic_gate_traffic: true`; requests only during the target RAMP. These requests **can satisfy the production quality gate**. Obtain production approval before applying. Candidate metadata is used to validate the request.

A future release with an incompatible Stable/Canary feature contract cannot safely share one payload; review compatibility before using policy-test. Default observe mode never supplies gate traffic.

## Run

Create config JSON with `version` (string), `mode`, `namespace`, unique `run_id`, `rps` (0 for observe, otherwise 1..20), `concurrency` (1..8), `requests` (1..2000), `duration` (1..900 seconds), `api_retries` (0..3), and `allow_synthetic_gate_traffic` (false by default). Provide an approved native V2 payload JSON matching all model inputs; observe uses `{}`. No training records or credentials belong in the payload.

Run `start_e2e_v2.ps1` with explicit `-Context`, `-Namespace`, `-ConfigPath`, `-PayloadPath`, `-Image` (immutable control-plane digest), and `-OutputDirectory`. Without `-Apply` it only renders `job.json`. Review that artifact before approved execution with `-Apply`. Use a process-scoped signed-script policy according to your organization; never set machine-wide Unrestricted.

The Job uses no AWS/GitHub credential and has get-only access to one ConfigMap and two named Deployments. Its mounted reviewed runner source is immutable and included in the run identity. Same run ID/config/source/image reuses the existing Job; a changed identity is rejected. A fresh run needs a fresh ID. Job restart is disabled; deadline bounds cancellation/network failures.

Exit codes: PASS=0, FAIL=1, BLOCKED=2. Timeout is BLOCKED. `events.jsonl` and `summary.json` contain sanitized counts, status, latency, track, stages and explicit synthetic origin, never request/response bodies or raw errors. HTTP errors and missing track headers fail the run. Evidence is printed to Pod logs for retrieval after completion. `-Cleanup` deletes only exact resources created by that invocation, after evidence retrieval; otherwise retain for inspection. Kubernetes Job TTL is 24 hours; remove retained run-specific RBAC/ConfigMap resources after evidence retention review.

Live release verification requires a genuine external promotion; tests do not fabricate one. Offline tests are `python -m pytest tests/unit/test_e2e_runner_v2.py`.
