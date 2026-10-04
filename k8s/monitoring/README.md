# P0 Dev monitoring

This directory is a reproducible configuration, **not runtime acceptance**.
Cluster inventory, target discovery, source queries, dashboard panels and load
evidence must pass before P0 is accepted. Do not install while cluster inventory
is unavailable, or assume that an API timeout means monitoring is absent.

The single installation path is `kube-prometheus-stack` **91.9.0**, Helm release
`ml-observability`, namespace `monitoring`. It includes Prometheus Operator,
Prometheus, Grafana, kube-state-metrics and authenticated kubelet/cAdvisor
scraping. Metrics Server is a separate prerequisite for HPA; it is not the
Prometheus history store. No node exporter, alert routing, public ingress or
control-plane scraping is added for this scope.

The [pinned chart](https://github.com/prometheus-community/helm-charts/blob/kube-prometheus-stack-91.9.0/charts/kube-prometheus-stack/Chart.yaml)
declares Kubernetes `>=1.25.0-0` and Operator `v0.94.1`; its dependencies pin
kube-state-metrics chart `8.6.0` (application `2.20.0`) and Grafana chart `13.2.7`.
Kubernetes 1.34 satisfies that chart constraint. This is not a claim that this
configuration has been installed or validated on the selected EKS cluster.
Use the chart package at the pinned version, not `main` or an unpinned upgrade.

### Current Dev installation (2026-10-04 continuation)

The user explicitly deferred credential-dependent Grafana work. The verified new
release was installed with the pinned archive and these values plus
`--set grafana.enabled=false`; this is an explicit temporary override, not a
default credential workaround. Prometheus, Operator and kube-state-metrics remain
installed. The application ServiceMonitor is installed; Grafana UI/panel display
acceptance remains BLOCKED until the managed Secret is provisioned. Dashboard
PromQL can be checked directly in Prometheus but that does not validate Grafana.

This partial profile needs three monitoring pods and 450m/768Mi main requests.
With the current workload, three serving replicas and load it reaches all 17
node pod slots. Complete load cleanup before any rollout that needs surge/hook
space. Enabling Grafana adds one pod and 100m/128Mi requests plus unrequested
sidecar usage, so the full three-replica/load experiment still needs additional
approved pod capacity. Reuse release `ml-observability`; never run a second
installation to enable Grafana. Check the Secret, capacity, current Helm values
and a reviewed upgrade render first. Current evidence is under
`evidence/p0-20261004/continuation/`.

## 1. Inventory and prerequisites

Set the context and Dev namespace **from verified deployment configuration**.
Do not derive a production target from the current kubectl context. Commands
below use PowerShell from the repository root. Inspect each result before
continuing; a failed inventory command blocks installation, not local rendering.

```powershell
$Context = '<verified-dev-context>'
$DevNamespace = '<verified-dev-namespace>'
kubectl --context $Context --request-timeout=10s get deployments,services,statefulsets -A
kubectl --context $Context --request-timeout=10s get crd prometheuses.monitoring.coreos.com servicemonitors.monitoring.coreos.com
helm --kube-context $Context list --all-namespaces
kubectl --context $Context --request-timeout=10s get apiservice v1beta1.metrics.k8s.io
kubectl --context $Context --request-timeout=10s get nodes
kubectl --context $Context -n $DevNamespace get deployment,service,endpointslice -l app.kubernetes.io/name=model-service
kubectl --context $Context top nodes
kubectl --context $Context top pods -A
```

When Prometheus/Operator/Grafana already exist, reuse the owning installation.
Inspect its Prometheus `serviceMonitorSelector`, namespace selector, CRD version,
KSM sources, kubelet scrapes and dashboard provisioning before adapting these
objects to those verified selectors. Do not deploy a second operator, install
these CRs against standalone Prometheus, or overwrite an existing Helm release
with these values. The commands in section 2 are for a **verified new** stack.

Before that new installation:

- Verify the `monitoring` namespace may be used and the release does not exist.
- Provision the existing Secret `ml-observability-grafana-admin` in `monitoring`
  using the environment's approved secret mechanism, with keys `admin-user`
  and `admin-password`. No value belongs in this repository, terminal evidence
  or screenshots. `kubectl get secret ... -o name` checks presence without data.
- Reserve capacity for the stack **plus three serving replicas and the load
  client**. The continuation render confirms main-container requests total
  550m CPU / 896Mi RAM. Two Grafana sidecars and two admission Jobs have no
  requests; the Operator's config-reloader request flags are zero. These
  containers still consume resources. Serving adds 300m / 576Mi at HPA maximum;
  the load client requests 250m / 128Mi. With measured existing requests of
  550m / 540Mi, the configured total is 1650m / 2140Mi against 1930m / 3371528Ki
  allocatable. **Pod capacity is insufficient for the full experiment:** current
  11 pods + four monitoring pods + two extra serving pods + one load pod = 18,
  versus 17 allocatable pod slots. Hooks and rollout surge need transient room.
  Obtain approved capacity/placement before combined acceptance; do not delete
  other workloads. Evidence: `evidence/p0-20261004/continuation/inventory.json`
  and `rendered-resources.json`. Pending pods never count as accepted scale-up.
- Permit the required internal traffic: Prometheus to serving endpoints:8000,
  KSM and kubelet:10250, and Grafana to Prometheus. Reuse the cluster's NetworkPolicy
  and security-group policy. These files do not relax those policies.
- Verify kubelet certificates against the cluster CA and address SANs. The chart
  default permits insecure kubelet TLS; these values explicitly turn that off.
  If targets fail certificate verification, obtain the correct trusted CA/name
  or approved scrape path; do not add `insecureSkipVerify: true` as a demo fix.

## 2. Render, inspect, then install the new stack

`render.py` uses the standard library and writes only local files. It requires
an explicit valid namespace, creates the ServiceMonitor and a dashboard ConfigMap,
and selects that namespace in the dashboard. It does not install or access a
cluster. Output is under the ignored test-cache directory.

```powershell
python k8s/monitoring/render.py --app-namespace $DevNamespace --output .pytest_cache/p0-monitoring
helm template ml-observability oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack --version 91.9.0 --namespace monitoring --kube-version 1.34.0 --values k8s/monitoring/values.yaml --include-crds > .pytest_cache/p0-monitoring/stack-rendered.yaml
```

Inspect the rendered workloads, images, resources, RBAC, CRDs, selectors, service
types and TLS settings. Record the chart archive digest and tool versions with
the runtime evidence. This repository does not include credentials or a rendered
chart bundle. A chart render pass is not a scrape pass.

Only after the inventory/prerequisites and rendered resources have been checked:

```powershell
kubectl --context $Context -n monitoring get secret ml-observability-grafana-admin -o name
helm --kube-context $Context install ml-observability oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack --version 91.9.0 --namespace monitoring --values k8s/monitoring/values.yaml --wait --timeout 10m
kubectl --context $Context wait --for=condition=Established --timeout=60s crd/servicemonitors.monitoring.coreos.com
kubectl --context $Context apply -f .pytest_cache/p0-monitoring/servicemonitor.yaml
kubectl --context $Context apply -f .pytest_cache/p0-monitoring/dashboard-configmap.json
kubectl --context $Context -n monitoring get pods,services,prometheus,servicemonitor
```

All ServiceMonitors are in `monitoring`. The new Prometheus selects that namespace
and `release=ml-observability`; the chart-generated KSM/kubelet monitors use the
same release label. The application ServiceMonitor selects the existing
`app.kubernetes.io/name=model-service` Service in the explicitly rendered Dev
namespace and its named `http` port. Operator discovery scrapes **each endpoint**,
with `namespace`, `pod`, `service` and a bounded `workload=model-service` target
label. It does not scrape the load-balanced ClusterIP as one target. Never apply
the unresolved `servicemonitor.yaml` source template directly.

The dashboard ConfigMap has the sidecar label `grafana_dashboard=1` in `monitoring`.
Grafana's chart provisions the Prometheus data source; select the source in the
dashboard if reusing an existing Grafana. Dashboard UID is `ml-model-service-p0`.
The exported JSON is authoritative; UI edits are disabled and no dashboard
credentials are embedded. Rendering again for a different namespace changes only
the selected namespace, not the deployed workload.

## 3. Verify sources and dashboard

In separate terminals, use localhost-only forwards for **observability access**:

```powershell
kubectl --context $Context -n monitoring port-forward service/ml-observability-prometheus 9090:9090 --address 127.0.0.1
kubectl --context $Context -n monitoring port-forward service/ml-observability-grafana 3000:80 --address 127.0.0.1
```

Open Grafana at `http://127.0.0.1:3000`, authenticate using the managed Secret,
select the verified namespace/workload and inspect every panel. Do not record
authentication or raw secret responses. A Service port-forward is suitable for
querying one Prometheus/Grafana instance; **it is not the serving load-test path**.

Save Prometheus `/api/v1/targets` and `/api/v1/query`/`query_range` JSON with UTC
timestamps, context/namespace and the load window. Confirm `health=up` and no
`lastError` for every intended serving endpoint, kube-state-metrics and each
node's kubelet/cAdvisor endpoints. Cross-check discovered app pods against ready
Deployment pods/EndpointSlices before, during and after scaling. At least two
scrapes are needed for `rate`; new-pod counters must subsequently increase.

Representative checks (substitute the verified namespace; these are PromQL):

```promql
up{namespace="DEV_NAMESPACE",workload="model-service"}
up{namespace="monitoring",service="ml-observability-kube-state-metrics"}
up{job="kubelet",metrics_path="/metrics"}
up{job="kubelet",metrics_path="/metrics/cadvisor"}
kube_deployment_status_replicas_ready{namespace="DEV_NAMESPACE",deployment="model-service"}
container_cpu_usage_seconds_total{namespace="DEV_NAMESPACE",container="model-service",image!=""}
sum by (pod) (increase(model_service_http_requests_total{namespace="DEV_NAMESPACE",workload="model-service",method="POST",route="/api/v1/predict"}[5m]))
histogram_quantile(0.95, sum by (le) (rate(model_service_http_request_duration_seconds_bucket{namespace="DEV_NAMESPACE",workload="model-service",method="POST",route="/api/v1/predict"}[5m])))
```

Example raw target evidence (contains no authentication data):

```powershell
Invoke-RestMethod http://127.0.0.1:9090/api/v1/targets | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 .pytest_cache/p0-monitoring/targets.json
$Query = 'up{namespace="' + $DevNamespace + '",workload="model-service"}'
Invoke-RestMethod ('http://127.0.0.1:9090/api/v1/query?query=' + [uri]::EscapeDataString($Query)) | ConvertTo-Json -Depth 30 | Set-Content -Encoding utf8 .pytest_cache/p0-monitoring/app-up.json
```

Review exports for non-sensitive deployment metadata before placing them in the
versioned evidence directory. Query results and timestamps are required alongside
screenshots; a provisioned dashboard alone proves no measurement.

Dashboard semantics:

- HTTP metrics use normalized routes/method/status. Business panels select only
  `POST /api/v1/predict`; probes and `/metrics` never contribute.
- HTTP elapsed time covers the ASGI response; inference elapsed time covers one
  service call. HTTP count, service-call count and attempted batch-item count are
  distinct. Inference `outcome` is success/input_error/not_ready/error. Rejected
  request schemas contribute HTTP 422 only. Operational failure tests require
  no production fault-injection endpoint.
- 4xx and 5xx have separate rates/fractions. Error fractions and P95 have no value
  without traffic. P95 sums histogram buckets across replicas first. The source
  health, required-family presence and traffic-state panels qualify all panels:
  missing discovery, scrape failure, insufficient samples and zero traffic must
  not be described as zero errors or healthy serving. Never set Grafana's null
  handling to zero.
- CPU/memory come from kubelet/cAdvisor. Replica/pod/restart/HPA state comes from
  kube-state-metrics. Pod filtering joins Pod owner → ReplicaSet owner → selected
  Deployment, including rollout ReplicaSets; it does not use a pod-name prefix.
- Per-pod CPU/request ratio is a dashboard estimate, not the HPA's exact sampling
  algorithm. Record actual Metrics API/HPA conditions and stabilization as well.
- Identity lives on `model_service_build_info`, not every request series. Exact
  runtime/artifact digests and init completion still require deployment evidence.

## 4. Retention and cleanup

This small Dev profile retains at most 24h/1GB in a 2Gi `emptyDir`; it is not a
production monitoring storage design. Grafana configuration is reproducible from
the ConfigMap. Export raw observations and the evidence table **before** pod
restart, uninstall or the end of the experiment. Check memory pressure, scrape
duration and resource use during load; initial monitoring requests may need
adjustment based on that measurement.

Clean up temporary load clients using the load runner's cleanup procedure and
stop port-forward terminals after capture. Retain the monitoring release for
continued Dev observation unless its removal is explicitly intended. Do not
delete shared CRDs or an existing stack as experiment cleanup.

Reference details: [pinned chart values](https://github.com/prometheus-community/helm-charts/blob/kube-prometheus-stack-91.9.0/charts/kube-prometheus-stack/values.yaml),
[ServiceMonitor discovery](https://prometheus-operator.dev/docs/developer/getting-started/),
[Prometheus histogram aggregation](https://prometheus.io/docs/practices/histograms/).
