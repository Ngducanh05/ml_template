# P0 monitoring and autoscale

## Scope and baseline

P0 adds Prometheus metrics to the existing FastAPI service, kubelet/cAdvisor and
kube-state-metrics collection, a Grafana dashboard, CPU requests and a CPU HPA.
The HPA owns the Deployment replica count; `k8s/deployment.yaml` intentionally
omits `spec.replicas` and release rendering rejects it if it appears.

The verified target is EKS context
`arn:aws:eks:us-east-2:596432515999:cluster/ml-template-dev`, namespace
`ml-template-dev`, Kubernetes `1.34`, one node with `1930m` allocatable CPU and
`3371528Ki` allocatable memory. Before P0, Metrics Server was available and
reported the serving pod at `2374191n` CPU and `93872Ki` memory (about `3m` and
`92Mi`) while idle. Node usage was `53m` CPU and `1354Mi` memory; existing node
requests were `550m` and `540Mi`. The pod had no resource requests, used a legacy
monolithic image, had no HPA and made no separate model artifact mount. This is
an idle baseline; it does not establish saturated CPU or a safe production size.
Raw timestamped identity and Pod details are in
`evidence/p0-20261004/before.jsonl` and `baseline.json`.

The initial runtime request is `100m` CPU / `192Mi` memory. The CPU request gives
HPA a meaningful denominator near the smallest supported vCPU slice, while the
memory request is about twice observed idle RSS to include server, model and
request allocations. Memory limit `384Mi` gives additional working headroom; no
CPU limit is set, avoiding an unmeasured inference throttle. Artifact copy
requests `10m` / `16Mi` and a `64Mi` limit reflect a 2.1KB trusted bundle and a
short init step. These are Dev starting values to recheck under prediction load.

HPA targets `65%` CPU utilization of requests, with 1–3 replicas. The checked-in
Dev node has 1.93 cores and 3.2Gi allocatable memory; three serving requests use
`300m` / `576Mi`. The pinned monitoring profile requests about `550m` and `896Mi`
for its main containers. The 2026-10-04 continuation render confirms two Grafana
sidecars and two admission hook Jobs have no requests; Operator flags give the
Prometheus config reloader zero CPU/memory requests. Zero requests do not mean
zero usage. The load client adds `250m` / `128Mi` requests (`500m` / `256Mi` limits).
With existing `550m` / `540Mi` requests, the configured total is `1650m` / `2140Mi`.
However, current inventory has 11 pods and the node allows only 17: four steady
monitoring pods, two additional serving replicas and one load pod require **18**.
Rollout surge and admission hooks need further transient headroom. The combined
monitoring/HPA acceptance therefore needs approved additional pod capacity or
explicitly authorized relocation of other workloads; do not remove shared pods.
See `evidence/p0-20261004/continuation/inventory.json` and
`rendered-resources.json` for measured inventory and rendered request evidence.
The user subsequently deferred Grafana credentials; the installed partial stack
uses the same pinned chart with `grafana.enabled=false`, reducing monitoring to
three pods and 450m/768Mi requests. Its load trial fits exactly 17 pod slots,
without spare surge capacity. This does not remove the full-profile blocker or
constitute Grafana acceptance.
Scale-up is bounded to one pod per 30s;
scale-down stabilization is 180s and removes at most one pod per minute.

## Collection

Application metric definitions, outcome semantics, and tests are in
`src/model_service/metrics.py` and `tests/api/test_metrics.py`. HTTP duration
covers request handling through the final ASGI response body. Inference duration
covers one `InferenceService.predict` call. One batch is one inference call;
batch items are counted separately. P95 aggregates histogram buckets across
serving targets before quantile calculation. Probes and `/metrics` do not appear
in application traffic metrics. The build identity comes from the runtime's
baked source manifest and the loaded artifact version, not an environment value.

The conditional monitoring release and dashboard procedures are in
`k8s/monitoring/README.md`. The pinned chart is `91.9.0`; retain kubelet TLS
verification and use the existing managed Grafana Secret. Metrics Server is the
HPA resource API, not the dashboard history store.

## Load and acceptance

`scripts/service_load.py` sends valid Iris inputs to
`http://model-service.<namespace>.svc.cluster.local:8000/api/v1/predict`, opens a
fresh TCP connection for each request, bounds duration/rate/concurrency/batch
size, checks the known response contract and emits UTC JSONL measurements. Run it
as a temporary Pod/Job inside the Dev cluster. `--local-only PORT` is for a
clearly labelled local baseline and can never count as Kubernetes balancing
evidence.

Run `scripts/p0_observe.py` concurrently with the load client and after load
removal. It records Deployment/Pod readiness, exact image references, HPA spec and
conditions, per-Pod Metrics API CPU/memory, and optional localhost Prometheus
queries. Preserve the JSONL command output and observe at least the configured
180-second downscale stabilization window plus the controller's subsequent
reconciliation. Before/during/after observations must show newly Ready replicas,
increasing per-Pod prediction counters, successful responses within the agreed
latency/error envelope, and return to one replica. Clean up the temporary load
Job after its logs and measurements are preserved.

Runtime states and commands belong in `evidence/p0-20261004/evidence.md`; never
mark a step PASS from a manifest, Helm render, unit test, or local-only load.
