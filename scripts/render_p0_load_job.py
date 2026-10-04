"""Render a bounded ClusterIP load Job; this command never contacts a cluster."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re


ECR_IMAGE = r"[0-9]{12}\.dkr\.ecr\.[a-z]{2}(?:-[a-z]+)+-[0-9]+\.amazonaws\.com/[a-z0-9]+(?:[._/-][a-z0-9]+)*@sha256:[0-9a-f]{64}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--image", required=True, help="Exact currently approved EKS-served container image@sha256")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=int, default=120)
    parser.add_argument("--rps", type=float, default=20)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--max-error-fraction", type=float, default=.01)
    parser.add_argument("--max-p95-seconds", type=float, default=1)
    args = parser.parse_args()
    if (not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.namespace)
            or not re.fullmatch(ECR_IMAGE, args.image)):
        parser.error("Supply a valid namespace and exact ECR image digest")
    if not (10 <= args.duration <= 600 and 0 < args.rps <= 500
            and 1 <= args.concurrency <= 16 and 1 <= args.batch_size <= 100
            and 0 <= args.max_error_fraction <= .05
            and 0 < args.max_p95_seconds <= 5):
        parser.error("Load must be bounded: 10..600s, <=500 RPS, <=16 clients, <=100 batch items")
    script = (Path(__file__).with_name("service_load.py")).read_text(encoding="utf-8")
    filename = f"p0-load-{datetime.now(timezone.utc):%Y%m%d%H%M%S}.yaml"
    payload = {"apiVersion": "v1", "kind": "ConfigMap",
               "metadata": {"name": "model-service-p0-load", "namespace": args.namespace},
               "data": {"service_load.py": script}}
    job = {"apiVersion": "batch/v1", "kind": "Job",
           "metadata": {"name": "model-service-p0-load", "namespace": args.namespace,
                        "labels": {"app.kubernetes.io/name": "model-service-p0-load"}},
           "spec": {"backoffLimit": 0, "activeDeadlineSeconds": args.duration + 60,
                    "ttlSecondsAfterFinished": 3600,
                    "template": {"metadata": {"labels": {"app.kubernetes.io/name": "model-service-p0-load"}},
                                 "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                                          "containers": [{"name": "load", "image": args.image,
                                              "imagePullPolicy": "IfNotPresent",
                                              "command": ["python", "/p0/service_load.py",
                                                          "--namespace", args.namespace,
                                                          "--duration", str(args.duration),
                                                          "--rps", str(args.rps),
                                                          "--concurrency", str(args.concurrency),
                                                          "--batch-size", str(args.batch_size),
                                                          "--max-error-fraction", str(args.max_error_fraction),
                                                          "--max-p95-seconds", str(args.max_p95_seconds)],
                                              "resources": {"requests": {"cpu": "250m", "memory": "128Mi"},
                                                            "limits": {"cpu": "500m", "memory": "256Mi"}},
                                              "volumeMounts": [{"name": "script", "mountPath": "/p0", "readOnly": True}]}],
                                          "volumes": [{"name": "script", "configMap": {"name": "model-service-p0-load",
                                                                                         "defaultMode": 292}}]}}}}
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / filename).write_text("---\n" + json.dumps(payload, indent=2)
                                      + "\n---\n" + json.dumps(job, indent=2) + "\n",
                                      encoding="utf-8")
    record = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
              "namespace": args.namespace, "image": args.image,
              "duration_seconds": args.duration, "rps": args.rps,
              "concurrency": args.concurrency, "batch_size": args.batch_size,
              "max_error_fraction": args.max_error_fraction,
              "max_p95_seconds": args.max_p95_seconds,
              "acceptance_scope": "Operational experiment envelope; not an approved SLO",
              "evidence_only": True, "cleanup": "kubectl delete job,configmap model-service-p0-load"}
    (args.output / "load-settings.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Rendered bounded load Job ({args.duration}s, {args.rps:g} RPS); review before apply.")


if __name__ == "__main__":
    main()
