"""Offline append-only standalone scrape review; never applies live resources."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import yaml

JOB = {"job_name": "mlops-v2-state-exporter", "scrape_interval": "15s",
       "scrape_timeout": "10s", "static_configs": [{"targets": ["mlops-v2-state-exporter.production.svc:9093"]}]}


INFRA_JOBS = [
    {"job_name": "kube-state-metrics-v2", "scrape_interval": "15s", "static_configs": [{"targets": ["kube-state-metrics-v2.production.svc:8080"]}]},
    {"job_name": "kubelet-cadvisor-v2", "scheme": "https", "metrics_path": "/metrics/cadvisor", "scrape_interval": "30s", "scrape_timeout": "10s",
     "authorization": {"credentials_file": "/var/run/secrets/kubernetes.io/serviceaccount/token"},
     "tls_config": {"ca_file": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt", "insecure_skip_verify": False},
     "kubernetes_sd_configs": [{"role": "node"}],
     "relabel_configs": [{"source_labels": ["__meta_kubernetes_node_address_InternalIP"], "target_label": "__address__", "replacement": "$1:10250"},
                         {"source_labels": ["__meta_kubernetes_node_name"], "target_label": "node"}]}
]


def prepare(configmap, deployment, infrastructure=False):
    if configmap["metadata"]["namespace"] != "production" or deployment["metadata"]["name"] != "prometheus":
        raise ValueError("unexpected_target")
    data = copy.deepcopy(configmap["data"])
    config = yaml.safe_load(data["prometheus.yml"])
    jobs = config["scrape_configs"]
    for addition in [JOB] + (INFRA_JOBS if infrastructure else []):
        matches = [job for job in jobs if job["job_name"] == addition["job_name"]]
        if matches and matches != [addition]:
            raise ValueError("conflicting_scrape_job")
        if not matches:
            jobs.append(copy.deepcopy(addition))
    data["prometheus.yml"] = yaml.safe_dump(config, sort_keys=False)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    name = "prometheus-config-" + digest
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "immutable": True,
          "metadata": {"name": name, "namespace": "production"}, "data": data}
    volumes = deployment["spec"]["template"]["spec"]["volumes"]
    index = next(i for i,v in enumerate(volumes) if v["name"] == "config")
    path = f"/spec/template/spec/volumes/{index}/configMap/name"
    old = volumes[index]["configMap"]["name"]
    if old != configmap["metadata"]["name"]:
        raise ValueError("config_snapshot_mismatch")
    patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": deployment["metadata"]["resourceVersion"]},
             {"op": "test", "path": path, "value": old},
             {"op": "replace", "path": path, "value": name}]
    rollback = [{"op": "test", "path": path, "value": name},
                {"op": "replace", "path": path, "value": old}]
    if infrastructure:
        pod = deployment["spec"]["template"]["spec"]
        for field, value in (("serviceAccountName", "prometheus-kubelet-v2"), ("automountServiceAccountToken", True)):
            field_path = "/spec/template/spec/" + field
            if field in pod:
                patch.append({"op": "test", "path": field_path, "value": pod[field]})
                rollback.append({"op": "test", "path": field_path, "value": value})
                rollback.append({"op": "replace", "path": field_path, "value": pod[field]})
            else:
                rollback.append({"op": "test", "path": field_path, "value": value})
                rollback.append({"op": "remove", "path": field_path})
            patch.append({"op": "add", "path": field_path, "value": value})
    return cm, patch, rollback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configmap", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--infrastructure", action="store_true", help="Requires successful kubelet preflight and separate rollout approval")
    args = parser.parse_args()
    cm, patch, rollback = prepare(json.loads(args.configmap.read_text(encoding="utf-8-sig")),
                                  json.loads(args.deployment.read_text(encoding="utf-8-sig")), args.infrastructure)
    args.output.mkdir(parents=True, exist_ok=True)
    for name, obj in (("configmap.json", cm), ("patch.json", patch), ("rollback.json", rollback)):
        (args.output / name).write_text(json.dumps(obj, indent=2), encoding="utf-8")
    for name, text in cm["data"].items():
        if Path(name).name != name:
            raise ValueError("unsafe_config_key")
        (args.output / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
