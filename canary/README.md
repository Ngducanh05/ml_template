# Local Canary demo — Windows amd64

One source tree: this `canary/` directory. The demo runs the existing repository
model service natively; no Docker, external launcher, or source-copy step is required.

## Commands

From `C:\ml_template`, use the existing `.venv` (Python 3.11.9, sklearn 1.7.2 was
validated; the trusted artifacts retain their original Python metadata):

```powershell
.\.venv\Scripts\python.exe .\canary\bootstrap.py
.\.venv\Scripts\python.exe .\canary\test_canary.py
.\.venv\Scripts\python.exe .\canary\demo.py run
.\.venv\Scripts\python.exe .\canary\demo.py stop
```

With that environment activated, the canonical commands are simply
`python canary/bootstrap.py`, `python canary/demo.py run`, and
`python canary/demo.py stop`.
Bootstrap downloads on first use; later invocations verify and reuse one install.
`run` validates dependencies and starts the entire lifecycle. Stop an existing run
before starting another; occupied ports are never reclaimed by killing strangers.

## Layout

```text
canary/
  bootstrap.py, demo.py, router.py, evaluator.py
  check_native.py, test_canary.py
  prometheus.yml, alerts.yml, dashboard.json, README.md, .gitignore
  artifacts/                      original stable and canary models
  build_candidate.py, nginx.conf  preserved original model builder / routing PoC
  .cache/
    prometheus/                   pinned Windows ZIP download
    grafana/                      pinned Windows ZIP download
  .local/
    prometheus/                   extracted binary + installation receipt
    grafana/                      extracted binaries/assets + installation receipt
  .runtime/
    prometheus-data/
    grafana-data/, grafana-logs/, grafana-plugins/
    provisioning/datasources/prometheus.yml
    provisioning/dashboards/canary.yml
    prometheus.yml, grafana.ini, processes.json, last-stop.json
  output/
    canary-<UTC timestamp>/        logs, events, metrics, validation evidence
```

Cache, installs, runtime and output are ignored by Git. `dashboard.json` is the
single canonical dashboard. Grafana's generated provider references that file
rather than copying source to another directory. Exported dashboard API responses
under output are evidence, not a second source tree. Historical failed-run logs
and prior native smoke-test results were retained under output.

## Pinned dependencies

- Prometheus **3.15.0**, Windows amd64, ZIP:
  https://github.com/prometheus/prometheus/releases/download/v3.15.0/prometheus-3.15.0.windows-amd64.zip
  SHA256 `5d333b385557d9adc2ff015d13da9809baccc52fb800a82d1e5c94b79258f87e`.
- Grafana **OSS 12.2.0**, Windows amd64, ZIP:
  https://dl.grafana.com/oss/release/grafana-12.2.0.windows-amd64.zip
  SHA256 `e5d1bde97dd6f0920398c5cc3f03eebe314dcb6f20f04b0a94dcdea744b76780`.
  Official checksum: https://dl.grafana.com/oss/release/grafana-12.2.0.windows-amd64.zip.sha256

Bootstrap rejects a non-Windows host, wrong hash, unsafe ZIP paths, missing or
ambiguous executable, wrong PE architecture, version mismatch, or invalid Grafana
home. Installation receipts verify the server/core executable and defaults file.
Re-extraction is confined to that component's `.local` directory. Archives never
share directories with source or runtime state.

## What happens during run

1. Validate dependency installations, free ports and model hashes.
2. Create all runtime/data/log/plugin/provisioning directories before launching.
3. Start stable, canary and router, checking process exits during readiness waits.
4. Send 100 real predictions and require exactly 90 stable / 10 canary successes.
5. Start Prometheus and Grafana. Validate both health endpoints, two per-track
   targets, dashboard provisioning/HTTP access and live data through Grafana's
   Prometheus datasource proxy.
6. Send bounded prediction load (up to 20 requests/sec, at most four in flight).
7. Evaluator promotes 10 -> 25 -> 50 -> 100 after healthy observation windows.
8. Stop the verified canary process tree. Require automatic rollback to 0 and a
   firing `CanaryDown` alert. Check 100 successful stable-only predictions.
9. Restart canary, require `up=1` and weight still 0, then save PASS evidence.

After PASS, load and evaluator finish. Models, router, Prometheus and Grafana stay
available for review. Use the absolute Grafana URL in `summary.json` to inspect
the recorded run after live traffic has stopped. `stop` verifies PID creation
time, executable and full command line, then stops only recorded demo processes
and descendants. It also handles Windows Python launchers and their child servers.
An infrastructure failure records FAIL with diagnostics and cleans up launched
services. A metadata file may remain for the explicit `stop` check before retrying.

## Ports and viewing

All listeners bind to loopback; no firewall opening or public exposure is needed.

| Component | Address |
| --- | --- |
| Router | http://127.0.0.1:8080 |
| Stable | http://127.0.0.1:18081 |
| Canary | http://127.0.0.1:18082 |
| Prometheus | http://127.0.0.1:9090 |
| Grafana | http://127.0.0.1:3000/d/local-canary |

Grafana anonymous access is Viewer. The loopback router control API is intended
only for this local demo. The original 8081/8082 listeners are not modified.

## Policy, metrics and alerts

Prometheus scrapes every 5 seconds. Stages hold at least 35 seconds, using a
30-second metric window. Availability/freshness is checked every controller
cycle, including warm-up. Both targets must be up, scrape age <=15s, actual canary
prediction traffic and successful inference must be positive, both backend and
proxy 5xx fractions must be <5%, and model HTTP p95 must be <100ms. Missing,
duplicate or non-finite results fail closed. Thresholds and promotion semantics
were preserved. Metrics exclude probe/scrape traffic.

HPA is a separate loop. The evaluator decides from Prometheus observations; alert
rules do not drive promotion. Five Prometheus rules cover down/missing target,
no traffic, errors and latency. No email/Slack/Alertmanager notification setup is
included. This proves serving/rollout behavior, not model-quality improvement or
production SLO compliance.

## Original Grafana failure and regression coverage

The old log proves Grafana 12.2.0 started as a Windows executable and read both
configuration files. It exited because its configured `grafana-data` directory
did not exist, so SQLite could not create `grafana.db`. The `.tar.gz` extension
alone did not imply a Linux binary. The repaired bootstrap uses the explicitly
requested official Windows ZIP. Runtime directory creation fixes the observed
failure. Process-aware waits now include return code and the final log lines
instead of hiding early exit behind a readiness timeout.

The original seven policy tests remain. Five infrastructure regression tests cover
missing Windows executable diagnostics, early-exit logging, reused PID isolation,
orphaned launcher children and creation of the SQLite directory before startup.
`promtool check config canary/.runtime/prometheus.yml` also validates all five rules.

Raw evidence includes `readiness.json`, `grafana-live-data.json`,
`targets-before.json`, `events.jsonl`, `failure-injection.json`,
`alerts-during-failure.json`, `rollback-routing.json`, `targets-final.json`,
Prometheus range queries, Grafana dashboard export, and `summary.json`.
