# v2 automation checkpoint â€” 2026-10-08

## Verified live

- Explicitly approved nodegroup growth from 4 to 6 t3.medium completed successfully (EKS update 556a0c85-21a4-3737-ab4d-42729b978c7f). Six nodes Ready. No downscale performed.
- Before capacity recovery, old v3 Stable and v5 Canary pods were Ready while v6 pods were Pending. Durable v6 release audit alone did not prove backend identity.
- After recovery, Stable v6 exact immutable runtime digest, artifact, completed Deployment rollout, health, metadata (10 features) and native V2 inference were verified. Both v6 lanes initially became Ready.
- An external SQS v7 release arrived during this work. No event was fabricated or replayed. Controller eventually rolled it back with insufficient_canary_traffic. Stable remained v6, active null, weight 0.
- Approved runner Job sent 10 synthetic requests at 1 RPS, concurrency 1, to Router-v2 ClusterIP while idle on v6. PASS: 10 Stable responses, 0 errors, 0 Canary. Only Job-owned resources were removed after saving evidence.
- Evidence outside Git: ci-validation/capacity-six-v6-verification.json, capacity-six-placement.json, runner-live-baseline/events.jsonl and summary.json in the current Codex visualization workspace.

## Code prepared, not deployed

Runner implementation and usage: ../scripts/e2e_v2/README.md. Read-only exact-resource RBAC; payload validation against actual model signature; completed Deployment identity requirement; bounded synthetic traffic only by explicit mode.

KServe HPA configuration: V2_STABLE_MAX_REPLICAS, V2_CANARY_MAX_REPLICAS (both default 1), V2_HPA_CPU_TARGET (default 80). Policy caps each lane at 2 and combined ceiling at 3. Proposed activation: Stable 1..2, Canary 1..1. Keep 1 CPU/2 GiB requests and runtime fingerprint unchanged. CPU80 preserves the installed HPA target; scale-up at most one Pod per minute; scale-down stabilization300 seconds. Installed CRD supports predictor.autoScaling.behavior. Existing HPAs are owned by their InferenceServices. Two actual 1CPU/2Gi placement slots remained at inventory, sufficient for one additional Stable replica and one rollout surge; recheck before activation.

Official HPA reference: https://kserve.github.io/website/docs/model-serving/predictive-inference/autoscaling/hpa-autoscaler

Readiness now also requires generated Deployment observed generation, exact version/artifact/image, complete updated replica count and ready/available replicas. Controller needs get-only permission on the two v2 Deployments. No release policy/state machine changes. No Iris, IAM, SQS or ECR change.

Minimal release metrics are prepared on existing Router-v2 /metrics. Versions are gauge values, phase/status labels are bounded, no URI/digest labels, stale snapshots omit values. This is code-only; deployment and full Grafana dashboard remain outstanding.

## Outstanding acceptance

- Local control-plane candidate built: manifest index sha256:f4535bc1b99b0e4b97cca649a75ac472a6df301b686af7fbe2f4c05c80d5103e; config image ID sha256:1b4d35b4b1353b7c35671bb62016d21e112c14383bbc5e391377bd3ab120b061. Non-root/read-only/network-none import smoke PASS. Exact archive scanned with Trivy0.70.0: HIGH/CRITICAL=0, no exceptions. ECR publication/deployment NOT RUN.
- Full repository regression after readiness changes: 741 PASS, 2 existing deprecation warnings.
- Real KServe scale-out and scale-in under an approved bounded load test; render/tests are not live autoscaling PASS.
- Grafana v2 dashboard/provisioning and live query validation. Existing datasource UID canary-prom; Grafana emptyDir must be preserved. Current Prometheus target credit-risk-router-v2 is up; KSM/cAdvisor not scraped.
- BUILD_REQUIRED recipe, trusted trigger and end-to-end security/publication automation remain unfinished. Existing runtime-build workflow remains fail-closed.
- Existing Runtime-X security exceptions expire 2026-10-21; no extension or vulnerability-free claim.
- No merge of Draft PR8; no new promotion requested.
