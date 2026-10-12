# CANARY HANDOFF

## Verified checkpoint — 2026-10-05
Repository: C:\ml_template
Origin: https://github.com/Ngducanh05/ml_template.git
Current branch: feat/canary-monitoring-demo
HEAD: 6a75cedd22d368414bf543afdccaafd0e566362a
Existing untracked work: canary/ and k8s/canary-poc.yaml. Preserve it.
Both SHA256 hashes below were recomputed and match. Other runtime details are user-provided historical observations, not reverified during handoff creation. Branch absence on GitHub was reported, not checked.
Before changes, inspect current Git root/origin/branch/HEAD and repository instructions. Current repo/runtime/tests take precedence over this checkpoint.

## Current objective
Finish a local automated Canary deployment demo for mentor evidence:
- 2 real model artifacts
- weighted traffic
- Prometheus metrics per stable/canary
- automatic promotion: 10 -> 25 -> 50 -> 100
- automatic rollback to 0 on failure
- Grafana/Prometheus evidence
- no GitLab/EKS work for this task

## Confirmed model artifacts
Stable: canary/artifacts/stable.joblib
SHA256: C8639C3D762655B1AAB1805E84FAE97DAA08D7815A3C28A0E5DCB9C2420F0C59
Canary: canary/artifacts/canary-v2.joblib
SHA256: 1667C4559CED9EF58A2181EEFA626694C9EE6735AC0B15E7983018AEFB28DE35
Candidate builder: canary/build_candidate.py
Previously reported: old stable/canary images contained the SAME artifact and must not be used as evidence of two different models.

## Runtime — previously reported
Exact runtime image exists locally:
596432515999.dkr.ecr.us-east-2.amazonaws.com/model-service-runtime@sha256:2823f97487217cfe3d28c5690829704d80cf13b13dfcef2a25f05a79b6e07ac0

Endpoints:
- /health/live
- /health/ready
- /metrics
- /api/v1/predict

Prometheus metrics:
- model_service_http_requests_total
- model_service_http_request_duration_seconds
- model_service_inference_requests_total
- model_service_inference_items_total
- model_service_inference_duration_seconds
- model_service_build_info

## Existing router
canary/nginx.conf
Old routing PoC was reported verified around 90/10 using X-Lab-Route.
This only proves weighted routing, not full Canary lifecycle.

## Docker blocker — previously reported
Docker Desktop 4.50.0, Engine 28.5.1.
Docker daemon intermittently returned 500/502 and hangs on container start.
Last reported containers: canary-stable = Created; canary-new = Created.
They were created using the runtime above.
Artifacts were copied successfully:
- stable.joblib -> canary-stable:/tmp/model.joblib
- canary-v2.joblib -> canary-new:/tmp/model.joblib

Stable environment: MODEL_PATH=/tmp/model.joblib; MODEL_NAME=iris-stable; port 8081:8000.
Canary environment: MODEL_PATH=/tmp/model.joblib; MODEL_NAME=iris-canary; port 8082:8000.
Last observed: docker version PASS; docker ps -a shows both containers Created; docker start hangs.

## Immediate next action
Do not redo AWS/ECR/model research.
1. Diagnose Docker start minimally.
2. If Docker remains unstable, use the fastest reliable local execution method instead of spending time debugging Docker.
3. Get both services to /health/ready = 200 and /metrics = 200.
4. Restore router on :8080.
5. Send POST prediction load through router.
6. Prometheus scrape stable/canary separately with track labels.
7. Implement evaluator querying Prometheus: PASS -> 10 -> 25 -> 50 -> 100; FAIL -> 0.
8. Failure demo can stop the canary service/container so up{track="canary"} becomes 0.
9. Add minimal alert rules and Grafana evidence.

## Canary demo policy
Demo only, not production SLO:
- canary up == 1
- actual canary traffic exists
- 5xx rate < 5%
- p95 < 100ms
Missing metrics / zero traffic must not be treated as healthy.

## Important architecture rule
HPA/autoscaling is a separate control loop.
HPA does NOT decide Canary promotion.
Prometheus supplies observations.
Evaluator decides promote/rollback.
Alertmanager is notification, not the promotion controller.

## Scope lock
Do not work on GitLab, EKS, KServe, MLflow lifecycle or CI/CD until the local Canary demo is complete.

## Definition of done
- stable and canary are different artifacts
- both ready
- both expose metrics
- router starts at 10% canary
- Prometheus has per-track data
- evaluator automatically promotes healthy canary
- evaluator automatically rolls back failed canary to 0%
- Grafana/Prometheus evidence captured
