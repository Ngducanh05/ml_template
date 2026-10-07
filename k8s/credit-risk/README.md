# Credit-risk production lifecycle

Account `596432515999`, region `ap-southeast-1`, cluster/context
`ml-template-dev-sg`, namespace `production`. Iris resources are independent.

## Deployment contract

- `foundation.json` is bootstrap only: create Stable/Canary once. Do not reapply
  after the controller has changed their digest or replica count.
- Build from the repository root with `Dockerfile.control-plane`. Dependencies
  live in `control_plane/requirements.txt`; the Dockerfile pins its base digest.
- Scan the resulting image for all HIGH/CRITICAL findings before publishing.
  Render `render.py --image <own-ECR-repository@sha256:...> --output <directory>`.
- Apply `rbac.json`. Create `state-create-only.json` only if the ConfigMap does
  not exist. Never apply or replace initial state over a live release.
- Install the EKS Pod Identity agent and associate SA `model-image-watcher`
  with `arn:aws:iam::596432515999:role/ml-template-dev-sg-image-watcher`.
  `watcher-trust.json` scopes namespace, service account and cluster;
  `watcher-policy.json` scopes the source queue, DLQ and Quang repository.
- Apply rendered workloads only after identity is associated. Controller defaults
  to zero replicas deliberately. Enable it only after capacity, queue access,
  reconciliation and Prometheus have been verified. Reapplying this bootstrap
  manifest also sets controller replicas back to zero; do not do so mid-release.
- Before every Kubernetes mutation assert current-context equals
  `ml-template-dev-sg`; pass explicit kubeconfig, context and namespace.

No static AWS credentials or node-role SQS permissions are used. Watcher cannot
patch deployments; controller can patch only the two credit-risk deployments;
router can only read the release state.

## Runtime and safety

Quang port 8080: GET `/health`, GET `/metadata`, POST `/predict` with an `inputs`
array containing the ten credit-risk features. Router forwards this exact path
and returns `X-Release-Track`. It owns request/error/latency metrics.

State resides in `production/model-release-state` under `state.json`, protected
by resourceVersion compare-and-swap. It includes stable identity, active phase,
pending candidates, processed/failed histories (100 each), audit (100), routing
revision, and a renewable controller lease. Pending capacity is 20; overflow
retries without deleting SQS messages. ConfigMap state survives Pod restart,
but is not an independent backup against ConfigMap/cluster deletion.

Watcher receives one message per 20-second long poll, uses 300-second visibility,
renews every 60 seconds while processing, persists decisions before deletion,
and retains transient failures for native retry/redrive. Queue retention must
be 14 days with redrive to the expected DLQ after five receives. DLQ is observed
only, never automatically consumed. No fabricated event is sent to the queue.

Hourly ECR reconciliation uses `DescribeImages` for registry `378457871131`,
repository `mlflow-models`; authorization failures retry after five minutes.
It uses the same durable ingestion path as SQS. Numeric canonical versions,
digest dedup, same-version rebuilds and stale-event rejection apply to both.
Merge `quang-ecr-statement.json` into Quang's existing repository policy;
it is one statement, NOT a replacement policy document.

Controller stages: PREPARING → RAMP (10/25/50/100) → FINALIZING → DRAINING → IDLE.
Evaluation waits 35 seconds after router acknowledges a stage, uses the last
30 seconds of metrics, requires successful real Canary predictions, router UP,
fresh samples, error ratio below 5%, and p95 below 100 ms. Insufficient traffic
keeps the current weight until the durable stage deadline; then rollback uses
`insufficient_canary_traffic`. Continuous legitimate prediction traffic is needed.
Unknown/stale telemetry fails closed. There is no unauthenticated write API.

On ramp failure Stable is untouched, routing returns to 0, Canary is scaled to
0 and the digest is recorded failed. On success Stable rolls to the exact
candidate digest while Canary serves 100%; Stable readiness and metadata are
verified before route 0 and Canary drain. If finalization fails, the controller
first restores the previous Stable. Unrecoverable restoration retains BLOCKED
state for operator review. Restart resumes durable phase; an expired active
controller lease makes the router unavailable rather than trusting stale routes.

## Monitoring and operational limits

`extend_monitoring.py` accepts a live Prometheus ConfigMap export, preserves its
jobs/rules and adds the three control-plane scrape jobs. Its JSON patch checks
the old config volume name before replacing it. Validate with promtool first.
Never apply the old Singapore monitoring render over the extended configuration.

Current monitoring uses emptyDir: a Pod replacement loses its local history.
Persistent monitoring storage and node redundancy remain separately approved
production hardening. A 17-Pod node cannot fit the full release plus surge/HPA
alongside the preserved Iris stack. Do not arm the controller until capacity
is approved and Ready. No node scaling, PVC/EBS, load balancer or NAT is created
by this implementation. Helm is deferred until the real Quang E2E passes.

## Validation and recovery

Run `python -m pytest tests/unit/test_credit_risk_lifecycle.py -q`.
Tests cover canonical filtering, numeric ordering, dedup/rebuilds/staleness,
CAS/restart, SQS delete ordering, transient retries, DLQ, reconciliation,
lease ownership, fail-closed metrics, rollback and Stable finalization.
These are mock/pure tests; they do not substitute for the real ECR→SQS E2E.

To suspend new releases, first inspect durable phase and route. Scaling the
controller to zero during an active release eventually makes routing unavailable
due to its lease. Do not erase state to recover. Inspect Stable/Canary readiness,
their exact digests, route revision, controller logs and recorded previous Stable.
Retain failed/processed history and reconcile it with actual deployments before
an operator-authorized correction. Do not bypass health gates or replay DLQ
without inspecting the event and its failure reason.

## Observation gate and explicit retry

Release policy is validated in `control_plane/policy.py`, consumed from deployment
environment and rendered by `render.py`. Use `--policy-file <json>` to override
any of these five keys (unknown keys and invalid values are rejected):

| Environment | Default |
| --- | --- |
| MIN_CANARY_REQUESTS | 1 |
| CANARY_DWELL_SECONDS | 35 |
| RELEASE_STAGE_TIMEOUT_SECONDS | 300 |
| MAX_ERROR_RATIO | 0.05 |
| MAX_P95_SECONDS | 0.1 |

The new minimum defaults to one observation, preserving the previous requirement
for nonzero traffic as closely as possible. It is a compatibility default, not a
claim of statistical confidence. Observations use the existing rolling 30-second
Prometheus `increase` window (an extrapolated counter estimate); they do not
accumulate over the whole stage. Existing timing/error/latency defaults remain.

Below the minimum, `RAMP_WAIT_TRAFFIC` logs model/version/digest/weight/observations/
minimum/deadline and increments `release_insufficient_observations_total`. It
neither promotes nor records a failure. Router health/sample age must still be
valid. Error/p95 assessment occurs only after minimum observations and dwell.
Undefined p95 is preserved as NaN; it is not changed to zero. After sufficient
observations, missing/nonfinite quality metrics cause `invalid_canary_metrics`.
The error query clamps only its denominator to 1e-12; missing series stay missing.
Stage deadlines survive restarts and are not extended by low traffic.

`python -m control_plane.retry --digest <digest> --version <version>
--operator <identity> --note <review-explanation>` is an explicit operator action.
Run it inside the new watcher image (its existing SA may update the state), or
another authorized in-cluster client. It uses the same CAS Store, requires no
active release, a matching failed digest/version, available pending capacity and
no duplicate pending entry; it rejects Stable/processed/stale candidates. It
enqueues one candidate and audits `operator_retry` with operator/note and previous
failure. Failed history is retained so normal SQS/ECR ingestion remains deduped.
Legacy failed entries without source_time use 0; stale same-version retries are
rejected rather than inventing a new image timestamp. New failures retain it.

Retry only after review. This command does not scale the controller or deploy a
model. Do not directly edit the ConfigMap or send an artificial ECR/SQS event.
With controller replicas 0, the queued candidate remains pending until a separate
authorized activation. No retry was executed as part of the traffic-gate fix.
