# Singapore serving and Canary stack

Account `596432515999`, AWS profile `ml-template-dev`, region `ap-southeast-1`,
cluster/context `ml-template-dev-sg`, namespace **production**. Ohio was deleted
by the owner and must not be recreated. `environment.json` is the local deployment
target; `images.json` pins the exact baseline and monitoring images.

This restores the existing serving/Canary demonstration on the requested namespace.
It is not a GitHub approved production release or a new production SLO. Existing
image source labels and release approval/scanning gates remain unchanged.

## Render and deploy

Use the existing Python environment with `requirements.txt` from this directory.
From the repository root:

```powershell
python k8s/singapore/render.py --images k8s/singapore/images.json --output evidence/singapore-rendered
aws eks update-kubeconfig --name ml-template-dev-sg --region ap-southeast-1 --profile ml-template-dev --alias ml-template-dev-sg --kubeconfig <dedicated-kubeconfig>
```

Before **each** Kubernetes change, read `kubectl --kubeconfig <dedicated-kubeconfig>
config current-context` and require exactly `ml-template-dev-sg`. All subsequent
commands must also specify `--context ml-template-dev-sg -n production` and the
same kubeconfig. Never fall back to the default context or namespace.

Apply `baseline.json`; verify serving before `hpa.json`. Baseline reuses the
repository Deployment/Service/ConfigMap and HPA; ECR pulls use node IAM, not a
registry password Secret. Deploy `canary.json`, `prometheus.json`, `grafana.json`,
`router.json`, `evaluator.json` after their dependencies are ready. Evaluator
starts at zero replicas: only scale it to one after required targets are UP and
real traffic exists. Do not reapply evaluator.json during an armed rollout.

Before Grafana deploy, create Secret `grafana-admin` with a cryptographically
random `admin-password` key. Keep it only in Kubernetes; do not save or print it
in evidence. Grafana uses anonymous Viewer for dashboard queries, signup disabled,
and a ClusterIP Service. Administrative login can use user `admin` and the password
retrieved by the authorized operator from that Secret. No LoadBalancer is created.

All immutable ConfigMaps are content-addressed. The Canary model uses the existing
trusted `canary/artifacts/canary-v2.joblib`; stable retains the baseline artifact
image and initContainer. No model is retrained. Monitoring uses the original
Prometheus config, alerts and Grafana dashboard, with DNS discovery of every
stable/canary pod. Evaluator availability uses min(up) and maximum scrape age so
multiple stable replicas do not create an ambiguous single-series query. Other
policy thresholds, dwell times, routing and fail-closed behavior are unchanged.

Prometheus/Grafana storage is bounded emptyDir for this minimal restore; data is
not durable across pod recreation. Dashboards/datasource configuration are retained
in source. A persistent storage design is outside this restore scope.

## Verify

```powershell
python scripts/verify_singapore.py baseline --kubeconfig <dedicated-kubeconfig> --output <evidence-dir>
python scripts/verify_singapore.py monitoring --kubeconfig <dedicated-kubeconfig> --output <evidence-dir>
python scripts/verify_singapore.py canary --kubeconfig <dedicated-kubeconfig> --output <evidence-dir>
```

The Canary check requires a fresh router at weight 10 and evaluator initially
scaled to zero. It sends real traffic, arms the existing evaluator, requires
10→25→50→100, scales only Canary to zero to reuse the existing stop-backend failure
mechanism, requires automatic rollback and 100/100 stable responses, and restores
Canary to one in a finally block. Router remains at 0 after rollback. The controller
waits there until an operator deliberately re-arms 10; it never auto-promotes from 0.

Canonical serving paths are `/health/live`, `/health/ready`, `/api/v1/predict`,
`/metrics`; shorthand `/live`, `/ready`, `/predict` are not new aliases.
HPA load uses `scripts/render_p0_load_job.py`, CPU target 65%, min 1/max 3 and the
existing 180-second scale-down window. Delete only the temporary load Job/ConfigMap
after collecting logs. Keep platform workloads running.

## GitHub and external dependencies

Release workflows already parameterize AWS/cluster/namespace and retain their
validation/approval gates. Do not map an unapproved validation job to the production
target or run the release workflow as part of this restoration. GitHub variable and
runner configuration is external state; local `environment.json` does not update it.
When configuring the approved production target, use the Singapore account/region/
cluster above and `PRODUCTION_NAMESPACE=production`. Keep validation isolation.

Quang ECR remains WAITING_EXTERNAL until repository policy and exact image URI are
provided. Test that image first with a standalone Pod in production and a genuine
fresh pull event; do not replace baseline based only on a successful authentication.
