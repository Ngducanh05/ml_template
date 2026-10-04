"""Read-only, bounded Kubernetes/HPA evidence, with optional localhost Prometheus."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import urlopen


def kube(context, namespace, *args):
    result = subprocess.run(["kubectl", "--context", context, "--namespace", namespace,
                             "--request-timeout=10s", *args], capture_output=True,
                            text=True, encoding="utf-8", timeout=35)
    if result.returncode:
        # stderr can contain external configuration; retain controlled error only.
        return {"status": "BLOCKED", "exit_code": result.returncode}
    return json.loads(result.stdout)


def capture(context, namespace, prometheus_port=None):
    dep = kube(context, namespace, "get", "deployment", "model-service", "-o", "json")
    pods = kube(context, namespace, "get", "pods", "-l", "app.kubernetes.io/name=model-service", "-o", "json")
    hpa = kube(context, namespace, "get", "hpa", "model-service", "-o", "json")
    metrics = kube(context, namespace, "get", "--raw",
                   f"/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods")
    names = {p["metadata"]["name"] for p in pods.get("items", [])}
    selected = []
    for pod in pods.get("items", []):
        selected.append({"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"],
                         "created_at": pod["metadata"]["creationTimestamp"],
                         "deleting": bool(pod["metadata"].get("deletionTimestamp")),
                         "node": pod["spec"].get("nodeName"),
                         "images": {c["name"]: c["image"] for group in ("containers", "initContainers")
                                    for c in pod["spec"].get(group, [])},
                         "status": pod.get("status", {})})
    result = {"timestamp": datetime.now(timezone.utc).isoformat(), "context": context,
              "namespace": namespace, "deployment": {"generation": dep.get("metadata", {}).get("generation"),
                 "desired": dep.get("spec", {}).get("replicas"), "status": dep.get("status", {}),
                 "images": {c["name"]: c["image"] for group in ("containers", "initContainers")
                            for c in dep.get("spec", {}).get("template", {}).get("spec", {}).get(group, [])}},
              "pods": selected, "hpa": {"spec": hpa.get("spec"), "status": hpa.get("status")},
              "resource_metrics": [m for m in metrics.get("items", []) if m["metadata"]["name"] in names],
              "checks": {"deployment": "PASS" if "metadata" in dep else "BLOCKED",
                         "pods": "PASS" if "items" in pods else "BLOCKED",
                         "hpa": "PASS" if "metadata" in hpa else "NOT_RUN",
                         "resource_metrics": "PASS" if "items" in metrics else "BLOCKED"}}
    if prometheus_port:
        queries = {
            "per_pod_requests": f'sum by (pod, status) (model_service_http_requests_total{{namespace="{namespace}",route="/api/v1/predict"}})',
            "p95": f'histogram_quantile(0.95, sum by (le) (rate(model_service_http_request_duration_seconds_bucket{{namespace="{namespace}",route="/api/v1/predict"}}[1m])))',
            "targets": f'up{{namespace="{namespace}",service="model-service"}}',
        }
        result["prometheus"] = {}
        for key, query in queries.items():
            try:
                with urlopen(f"http://127.0.0.1:{prometheus_port}/api/v1/query?" + urlencode({"query": query}), timeout=5) as response:
                    result["prometheus"][key] = json.load(response)
            except (OSError, ValueError):
                result["prometheus"][key] = {"status": "BLOCKED"}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=600)
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--prometheus-port", type=int)
    args = parser.parse_args()
    if not (0 <= args.duration <= 1200 and 10 <= args.interval <= 60):
        parser.error("duration must be 0..1200 and interval 10..60 seconds")
    if args.prometheus_port and not 1024 <= args.prometheus_port <= 65535:
        parser.error("Invalid localhost Prometheus port")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + args.duration
    with args.output.open("x", encoding="utf-8") as stream:
        while True:
            stream.write(json.dumps(capture(args.context, args.namespace, args.prometheus_port)) + "\n")
            stream.flush()
            if time.monotonic() >= end:
                break
            time.sleep(min(args.interval, max(0, end - time.monotonic())))


if __name__ == "__main__":
    main()
