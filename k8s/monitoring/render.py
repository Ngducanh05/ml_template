"""Render the two P0 monitoring objects locally; never contact a cluster."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-namespace", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", args.app_namespace):
        parser.error("--app-namespace must be an explicit Kubernetes namespace")
    directory = Path(__file__).resolve().parent
    monitor = (directory / "servicemonitor.yaml").read_text(encoding="utf-8")
    monitor = monitor.replace("APP_NAMESPACE_REQUIRED", args.app_namespace)
    dashboard = json.loads((directory / "dashboard.json").read_text(encoding="utf-8"))
    for variable in dashboard["templating"]["list"]:
        if variable["name"] == "namespace":
            variable["current"] = {
                "selected": True,
                "text": args.app_namespace,
                "value": args.app_namespace,
            }
    config_map = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "model-service-dashboard",
            "namespace": "monitoring",
            "labels": {"grafana_dashboard": "1"},
        },
        "data": {"model-service.json": json.dumps(dashboard, indent=2) + "\n"},
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "servicemonitor.yaml").write_text(monitor, encoding="utf-8")
    (args.output / "dashboard-configmap.json").write_text(
        json.dumps(config_map, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Rendered ServiceMonitor and dashboard for namespace {args.app_namespace}")


if __name__ == "__main__":
    main()
