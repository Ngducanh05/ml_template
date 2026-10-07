# Credit-risk lifecycle — 2026-10-06 16:10 Asia/Saigon

## STATUS: PARTIAL

Not READY_FOR_QUANG_PUSH. Watcher is live, but the controller remains deliberately
at zero replicas and ECR reconciliation is denied by the external repository policy.

## Unchanged working components

Iris Stable/Canary deployments, router, evaluator and HPA configuration were not
modified. Current Stable/Canary/router scrape targets are UP; Iris router weight
is 0 and evaluator replicas are 0. Grafana health reports database OK.
Prometheus was extended with three jobs and rolled successfully. Its old five
alert rules were preserved. Its emptyDir history was not durable across this
replacement; previous rollout evidence remains in the existing evidence files.

## Environment

- Account: 596432515999; profile: ml-template-dev.
- Region: ap-southeast-1; cluster/context: ml-template-dev-sg; namespace: production.
- Node: ip-172-31-34-101.ap-southeast-1.compute.internal, Ready.
- Capacity: one t3.medium, 17 allocatable Pod slots; 16 active Pods after this work.
- Ohio is intentionally absent; no Ohio resource was recreated.
- Repository: C:\ml_template; branch feat/canary-monitoring-demo;
  HEAD 6a75cedd22d368414bf543afdccaafd0e566362a.

## Quang model and routing

Stable version 1; digest:
`sha256:45dcaf3b993483344e55f71d7486cc83ab76fd9cff8a6690318449df48d4a3a5`.
Stable is 1/1 Ready. Metadata confirms credit-risk-classifier/version 1.
Router is 1/1 Ready; weight 0, revision 0. Live POST `/predict` with `inputs`
returned HTTP 200, `X-Release-Track: stable`, `{"predictions":[0]}`.
Credit-risk Canary replicas are 0. There is no active or pending candidate.

## Watcher

- Pod: model-image-watcher-56f84f96fd-fmgnm, 1/1 Ready, zero restarts at capture.
- Service account: production/model-image-watcher.
- IAM role: arn:aws:iam::596432515999:role/ml-template-dev-sg-image-watcher.
- EKS Pod Identity agent ACTIVE; association a-tpxccf295hce8havh.
- In-Pod STS verified the dedicated assumed role, account 596432515999.
- Source queue: mlflow-models-image-pushed in account 378457871131.
- Repeated 20-second long polls succeeded with zero messages.
- Last captured successful poll: 2026-10-06T09:10:02Z (16:10:02 local).
- Queue and DLQ attribute checks passed: retention 14 days, redrive 5 receives.
- DLQ visible count: 0.
- Delete/visibility renewal grants are configured; no real message was available
  to exercise those two actions. Their ordering/retry behavior is unit-tested.
- No fabricated message was sent.

## Durable state and controller

ConfigMap production/model-release-state was created once, not reapplied.
State: Stable v1/digest above, active null, pending [], failed [], processed
contains the initial digest, route weight 0/revision 0, lease null.
Controller deployment exists at 0 replicas. Its Prometheus target is therefore
DOWN intentionally; this is not a healthy active release controller.

Implemented and unit-tested: durable CAS, bounded histories/pending queue,
restart recovery, canonical/numeric versions, shared SQS/ECR ingestion,
10→25→50→100, Stable finalization, route return to 0, rollback preserving Stable,
and recovery of previous Stable after failed finalization.
These production-model promotion/rollback paths have NOT run live yet.
The earlier Iris EKS rollout remains historical accepted evidence only.

## Reconciliation blocker

In-Pod ECR call returned AccessDeniedException:
`no resource-based policy allows the ecr:DescribeImages action` on
`arn:aws:ecr:ap-southeast-1:378457871131:repository/mlflow-models`.
The local watcher identity policy already allows exactly this action/resource.
Quang must append the statement in `quang-ecr-statement.json` to the repository's
existing policy. Preserve existing node pull permissions; do not replace the
whole policy with this single statement. No AWS credential for Quang's account
was used. Latest canonical ECR identity cannot be verified while denied.

Watcher retries reconciliation after five minutes, independently of successful
SQS polling; `watcher_reconciliation_ready` is currently 0.

## Monitoring and security

- Prometheus: credit-risk-router and model-image-watcher UP; Iris targets UP.
- New weight, last-poll timestamp, DLQ count and reconciliation metrics verified.
- Controller metrics await activation. Grafana existing health OK; no new
  credit-risk dashboard was provisioned in this pass.
- Dedicated Pod Identity; no static AWS keys and no node-role SQS grant.
- RBAC checks: controller credit-risk patch YES, Iris patch NO; watcher
  deployment patch NO, release ConfigMap update YES.
- Control-plane immutable image:
  `596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:08500622bb68f3a3322e22ecd45f5ad5b5ab345da0f026452c4059ca81961b04`.
- Image nonroot 10001; workload root filesystem read-only; capabilities dropped.
- Trivy 0.70.0, HIGH/CRITICAL including unfixed: zero findings, exit 0.
- Prometheus extended config: prometheus-config-15ce84bf4568; promtool PASS.

## Validation

- PASS: `C:\ml_template\.venv\Scripts\python.exe -m pytest tests/unit/test_credit_risk_lifecycle.py -q` — 34 passed.
- PASS: Docker build for linux/amd64, immutable ECR push and actual Pod startup.
- PASS: Trivy scan with `--severity HIGH,CRITICAL --ignore-unfixed=false
  --ignorefile /dev/null --exit-code 1`.
- PASS: `promtool check config /etc/prometheus/prometheus.yml` — config and 5 rules.
- PASS: Stable rollout, health/metadata, router prediction, Pod Identity STS,
  repeated SQS polls, DLQ metrics, RBAC allow/deny checks, Prometheus rollout/targets.
- BLOCKED: ECR DescribeImages resource policy.
- NOT_RUN: real new-image production Canary promotion/rollback; controller disabled.
- SKIPPED: rerunning unaffected 437 baseline and 13 Iris Canary tests, per the
  latest instruction. Their accepted earlier results remain historical PASS.
- SKIPPED: Helm lint/template/migration — no chart; deferred until real E2E.
- PASS: git diff --check for tracked diff (new implementation is untracked).
- No Git commit, push, merge, reset or clean was performed.

## Problems and corrections

An initial smoke request used `dataframe_records` and received 422. The API
confirmed its required `inputs` field; corrected request returned 200.
An initial Iris health-check script used `/state` and received 404; source
inspection identified `/__admin/state`, which verified weight 0.
The first promtool image download timed out during TLS; one retry succeeded.
The controller now rejects queued same-version candidates older than the newly
promoted Stable, with a regression test. Dockerfile now copies its dedicated
requirements path correctly when building from the repository root.

## Remaining blockers and costs not applied

1. Capacity approval pending for one additional t3.medium worker (2 vCPU/4 GiB,
   20 GiB gp3). No node scaling has been applied. One free Pod slot cannot fit
   controller + candidate + rollout surge alongside existing workloads.
2. Quang repository must allow this watcher role `ecr:DescribeImages`.
3. Real external ECR→SQS→rollout acceptance remains untested until these are ready.

Persistent monitoring and node failure resilience remain production hardening
gaps. No EBS/PVC, LoadBalancer, NAT or other paid infrastructure was added.
Do not call this fully HA or announce READY_FOR_QUANG_PUSH yet.

## Files added in C:\ml_template

- Dockerfile.control-plane
- Dockerfile.control-plane.dockerignore
- control_plane/__init__.py
- control_plane/state.py
- control_plane/kube.py
- control_plane/telemetry.py
- control_plane/watcher.py
- control_plane/controller.py
- control_plane/router.py
- control_plane/requirements.txt
- tests/unit/test_credit_risk_lifecycle.py
- k8s/credit-risk/foundation.json
- k8s/credit-risk/render.py
- k8s/credit-risk/extend_monitoring.py
- k8s/credit-risk/watcher-trust.json
- k8s/credit-risk/watcher-policy.json
- k8s/credit-risk/quang-ecr-statement.json
- k8s/credit-risk/README.md
- k8s/credit-risk/STATUS.md

Evidence directory:
`C:\Users\Admin\.codex\visualizations\2026\10\06\01a10f41-2b35-7c93-970a-b58ad0977ecc\credit-risk`.
Includes final Trivy JSON, build metadata, live-check.txt, monitor-check.txt,
state-verified.json, pods-verified.json, rendered manifests, old/new Prometheus
configuration and its guarded patch. Existing untracked work was preserved.

## Memory

Obsidian MCP is unavailable in this session. Project memory mapping verification
and the Session write could not be performed. This operational report is not a
curated memory promotion; no STATE.md/DECISIONS.md was changed.

## Next action

Send Quang the dedicated watcher role and append-only ECR statement so he can
grant DescribeImages; capacity approval is still pending separately.
