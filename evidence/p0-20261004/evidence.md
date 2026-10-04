# P0 runtime evidence — 2026-10-04

## Scope

P0 covers Monitoring + Autoscaling acceptance on EKS Dev.

In scope:

- Serving metrics instrumentation.
- Prometheus collection and Kubernetes infrastructure metrics.
- Grafana dashboard provisioning and visualization.
- HPA based on CPU utilization.
- Service-based prediction load.
- Scale-up and scale-down evidence.
- Exact runtime/artifact image validation before Dev rollout.
- Regression tests and operational evidence.

Out of scope for this phase:

- GitLab/P1 implementation.
- KServe.
- Canary rollout implementation.
- Automated rollback.
- MLflow Registry watcher / model lifecycle automation.
- Training, evaluation, model quality gates, candidate selection and other upstream responsibilities owned by Quang.

---

## Environment

| Item | Value |
|---|---|
| Repository | `Ngducanh05/ml_template` |
| Base branch | `main` |
| P0 branch | `feat/p0-monitoring-autoscaling` |
| EKS cluster | `ml-template-dev` |
| AWS region | `us-east-2` |
| AWS account | `596432515999` |
| Dev namespace | `ml-template-dev` |
| Monitoring namespace | `monitoring` |
| Kubernetes | EKS 1.34 |
| Node pod capacity | 17 pods |
| Monitoring chart | `kube-prometheus-stack-91.9.0` |
| Grafana | 13.2.3 |
| HPA | `autoscaling/v2`, min 1, max 3, CPU target 65% |

AWS commands for this work use the explicit profile `ml-template-dev`. The default AWS profile returned `InvalidClientTokenId`, while `aws sts get-caller-identity --profile ml-template-dev` successfully returned the expected Dev account and IAM identity.

---

## Acceptance summary

| Check | Result | Status |
|---|---|---|
| Existing runtime/artifact pair rescanned under unchanged security policy | Both exact-image scans passed; no rebuild required | PASS |
| Dev runtime rollout | Exact image pair deployed successfully | PASS |
| Artifact initialization | Artifact init container completed successfully | PASS |
| Readiness / liveness | Runtime became Ready and remained healthy | PASS |
| Prediction smoke | Valid prediction returned successfully (`setosa`) | PASS |
| Application `/metrics` | Instrumented runtime exposed serving metrics | PASS |
| Prometheus | Serving and required Kubernetes sources scraped successfully | PASS |
| Grafana | Dashboard provisioned and operational | PASS |
| HPA scale-up | Observed 1 → 3 replicas | PASS |
| HPA traffic distribution | New replicas received prediction traffic through Kubernetes Service | PASS |
| HPA scale-down | Observed 3 → 2 → 1 replicas after load removal | PASS |
| Service load run | 18,538 requests, 0 HTTP errors, client P95 ~19.8 ms | PASS |
| Regression suite | 424 tests passed | PASS |
| P1 GitLab | Intentionally not started in this scope | NOT_STARTED |

---

## 1. Exact-image scan gate

### Previous failed attempt

The previous scan evidence contained `FAIL` status for both runtime and artifact images, but the old status alone did not identify whether the failure was caused by vulnerabilities or by an operational scanner problem.

The retained artifact retry log confirmed an operational failure while Trivy was downloading/extracting its vulnerability database because the local disk did not have enough free space.

No valid Trivy JSON vulnerability report was produced by that failed attempt.

The exact cause of the old runtime scan failure cannot be reconstructed because the previous helper did not retain sufficient diagnostics.

Therefore:

- The old failures are **not** interpreted as vulnerability findings.
- No vulnerability result was fabricated.
- The security policy was not weakened.

### Current scan result

The same already-published immutable runtime/artifact digest pair was rescanned after sufficient local disk space became available.

Policy remained unchanged:

- Blocking severities: `HIGH,CRITICAL`
- `ignore-unfixed=false`
- Exact immutable image identities
- Single `linux/amd64` image identity contract

Result:

- Runtime exact-image scan: **PASS**
- Artifact exact-image scan: **PASS**
- Rebuild required: **No**

The existing images were reused rather than rebuilt solely to make the security gate pass.

Scan diagnostics were also improved so future failures can retain:

- process exit code;
- report existence;
- report parse state;
- sanitized operational error classification.

---

## 2. Dev runtime rollout

After both exact-image scans passed, the validated runtime/artifact pair was deployed to EKS Dev.

Verification included:

- Exact runtime digest.
- Exact artifact digest.
- Dual-image runtime/artifact separation.
- Artifact init-container completion.
- Non-root runtime execution.
- Readiness.
- Liveness.
- Prediction smoke test.
- Running Deployment/Pod image identity.

The shared deployment helper preserves HPA ownership of replica count. The rendered Deployment must not contain a fixed `spec.replicas` field.

Result:

- Deployment apply: **PASS**
- Rollout: **PASS**
- Artifact init: **PASS**
- Readiness: **PASS**
- Exact-image verification: **PASS**
- Prediction smoke: **PASS**

---

## 3. Monitoring

### Application metrics

The serving application exposes Prometheus metrics for operational inference behavior, including request/result/error and latency measurements.

Business prediction traffic is separated from health/metrics traffic.

Metrics avoid high-cardinality request IDs or raw payload labels.

### Prometheus

The pinned monitoring stack was installed in namespace `monitoring`.

Verified sources include:

- model-service application targets;
- kube-state-metrics;
- kubelet;
- cAdvisor/container metrics.

TLS verification was retained.

Prometheus query verification covered:

- prediction request rate;
- explicit prediction errors;
- HTTP latency histogram;
- inference latency histogram;
- CPU;
- memory;
- desired/current replica state;
- pod state;
- container restarts.

During the verified load window Prometheus observed approximately:

- request rate: ~79 requests/s at the observed load period;
- HTTP errors: 0;
- aggregate histogram P95: ~15.7 ms;
- serving CPU activity across all three replicas.

Before/after load:

- request rate correctly returned to zero;
- latency series may have no value when there are no requests;
- this is treated as zero-traffic semantics rather than missing collection.

Prometheus result: **PASS**

---

## 4. Grafana

Grafana was initially deferred while the required admin Secret was unavailable.

The following Kubernetes Secret was later provisioned outside the repository:

`monitoring/ml-observability-grafana-admin`

Expected keys:

- `admin-user`
- `admin-password`

No credential values are committed to the repository.

Grafana was enabled on Helm release:

`ml-observability`

Chart:

`kube-prometheus-stack-91.9.0`

### Initial Grafana memory failure

The initial configuration used:

- memory request: `128Mi`
- memory limit: `256Mi`

The Grafana container was confirmed to terminate with:

- Reason: `OOMKilled`
- Exit code: `137`

Observed Grafana memory before/around failure was approximately 241–286 MiB.

The failure was therefore attributed to the 256 MiB container memory limit rather than to the dashboard, datasource or Kubernetes Service.

The Grafana memory limit was increased to:

`512Mi`

The memory request remains:

`128Mi`

After rollout of the corrected configuration:

- Grafana pod: `3/3 Ready`
- Grafana container restart count: `0`
- Dashboard sidecar restart count: `0`
- Datasource sidecar restart count: `0`
- Grafana observed memory after stabilization: ~261 MiB
- `/api/health`: `database=ok`
- Grafana version: `13.2.3`

### Dashboard verification

Dashboard:

`ML model service — P0 monitoring`

The dashboard was automatically provisioned from the monitoring ConfigMap.

Verified Grafana visualization includes:

- serving scrape health;
- Kubernetes metric source health;
- required metric-family checks;
- prediction request rate;
- explicit HTTP error behavior;
- runtime CPU;
- runtime working-set memory;
- CPU/request ratio;
- Deployment/scrape target replicas;
- HPA desired/current replicas;
- pod phase;
- container restarts;
- runtime/model identity.

Current zero-traffic behavior correctly shows:

- prediction request rate = 0;
- HTTP errors = 0;
- latency panels may show no current value when there are no requests.

Historical request traffic was visible in Grafana during the retained monitoring window.

Historical latency values were independently verified from Prometheus query evidence collected during the load run.

Grafana result: **PASS**

---

## 5. Autoscaling

HPA configuration:

- API: `autoscaling/v2`
- Minimum replicas: 1
- Maximum replicas: 3
- CPU target: 65%
- Scale-down stabilization window: 180 seconds

Metrics Server was already installed and was reused.

No second Metrics Server was installed.

HPA owns replica count; the base Deployment does not own a fixed replica count.

### Load method

Traffic was generated through the Kubernetes Service using valid prediction input.

A pod-bound `kubectl port-forward` was not used as traffic-distribution evidence.

The load client was bounded and temporary.

Acceptance criteria used for this operational run were test criteria only, not mentor-approved production SLOs.

### Observed scale behavior

Observed sequence:

`1 → 3 → 2 → 1`

Both newly created serving replicas received prediction traffic through the Service.

No serving container restart was observed during the verified load.

### Verified Prometheus load run

Observed result:

- Requests: **18,538**
- HTTP 200: **18,538**
- HTTP errors: **0**
- Client P95: **~19.8 ms**
- Prometheus request rate during load: **~79 requests/s**
- Prometheus histogram P95 during load: **~15.7 ms**

After load termination:

- load Job removed;
- load ConfigMap removed;
- HPA returned to baseline;
- replicas reconciled back to 1.

Autoscaling result: **PASS**

---

## 6. Capacity constraint

The EKS Dev node exposes capacity for:

`17 pods`

After the complete monitoring stack including Grafana was enabled, the observed scheduled active pod count was:

`15 / 17`

At HPA maximum:

- current full-monitoring state: 15 pods;
- two additional model-service replicas: +2;
- projected total: `17 / 17`.

Therefore:

`Monitoring + Grafana + HPA max=3` fits the current single-node Dev cluster.

However, there is no remaining pod-slot headroom at maximum serving replica count.

The temporary load Job would require an additional pod and therefore cannot coexist with the full `17/17` max-replica state on this node.

This is recorded as a **single-node Dev capacity constraint**, not as a P0 functional failure.

The verified load runs were completed and their temporary resources were deleted before the final full Grafana state.

---

## 7. Resource observations

### Grafana

Current configuration:

- CPU request: `100m`
- Memory request: `128Mi`
- Memory limit: `512Mi`

Observed stabilized Grafana process memory:

~261 MiB

The current request is below observed runtime usage and should be reconsidered during later capacity tuning.

It is recorded as a sizing observation and does not invalidate the P0 acceptance result.

### Model serving

HPA behavior was demonstrated using the current configured serving resource requests.

Resource values must not be reduced merely to make the autoscaling demonstration trigger.

---

## 8. Regression validation

Current regression suite:

`.venv/Scripts/python.exe -m pytest -q`

Result:

**424 passed**

Additional checks:

- Python compile check: PASS
- Git diff whitespace check: PASS after dashboard JSON line-ending normalization

---

## 9. Evidence preservation

Historical failed scan attempts are intentionally retained separately from successful retries.

Raw evidence directories may contain:

- baseline inventory;
- failed scan attempts;
- successful scan retries;
- runtime build/publish metadata;
- rollout observations;
- Prometheus queries;
- load-run results;
- cleanup records;
- final cluster state.

The summary in this file is the authoritative P0 acceptance summary.

Raw evidence must not be interpreted independently without considering attempt status.

Status meanings:

- `PASS`: verified successfully.
- `FAIL`: attempted and failed.
- `BLOCKED`: prerequisite unavailable.
- `NOT_RUN`: not executed.
- `SKIPPED`: intentionally outside the current execution scope.

---

## Final P0 acceptance

# P0 Monitoring + Autoscaling: PASS

Acceptance basis:

- Exact immutable runtime/artifact pair passed the unchanged required scan policy.
- No rebuild was required to recover from the previous operational Trivy failure.
- Instrumented runtime was deployed successfully to verified EKS Dev.
- Artifact initialization, readiness, exact-image verification and prediction smoke passed.
- Prometheus scraped the serving workload and required Kubernetes infrastructure sources.
- Grafana was provisioned successfully and displayed real Prometheus-backed operational metrics.
- Confirmed Grafana OOM at 256 MiB was corrected to a 512 MiB limit; the replacement pod stabilized with 0 restarts.
- HPA was observed scaling `1 → 3 → 1`.
- Newly created replicas received traffic through the Kubernetes Service.
- Verified load run completed 18,538 requests with 0 HTTP errors and client P95 about 19.8 ms.
- Current full monitoring state uses 15/17 pod slots; HPA max=3 reaches 17/17.
- Temporary load workload must not coexist with max serving replicas on the current single-node Dev cluster.
- Current regression suite: 424 tests passed.

## Next phase

P1 GitLab: **NOT_STARTED**

The next phase may begin from this accepted P0 checkpoint.

P1 objective:

`code change → GitLab pipeline → tests → runtime build → Trivy → ECR exact digest → EKS Dev rollout → readiness/smoke → verify new digest/code`

KServe, Canary, automated rollback and MLflow lifecycle automation remain outside this P0 acceptance.