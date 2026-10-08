"""Offline append-only standalone scrape review; never applies live resources."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import yaml

JOB = {"job_name": "mlops-v2-state-exporter", "scrape_interval": "15s",
       "scrape_timeout": "10s", "static_configs": [{"targets": ["mlops-v2-state-exporter.production.svc:9093"]}]}


def prepare(configmap, deployment):
    if configmap["metadata"]["namespace"] != "production" or deployment["metadata"]["name"] != "prometheus":
        raise ValueError("unexpected_target")
    data = copy.deepcopy(configmap["data"])
    config = yaml.safe_load(data["prometheus.yml"])
    jobs = config["scrape_configs"]
    matches = [job for job in jobs if job["job_name"] == JOB["job_name"]]
    if matches and matches != [JOB]:
        raise ValueError("conflicting_scrape_job")
    if not matches:
        jobs.append(copy.deepcopy(JOB))
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
    return cm, patch, rollback


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configmap", type=Path, required=True)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cm, patch, rollback = prepare(json.loads(args.configmap.read_text(encoding="utf-8-sig")),
                                  json.loads(args.deployment.read_text(encoding="utf-8-sig")))
    args.output.mkdir(parents=True, exist_ok=True)
    for name, obj in (("configmap.json", cm), ("patch.json", patch), ("rollback.json", rollback)):
        (args.output / name).write_text(json.dumps(obj, indent=2), encoding="utf-8")
    for name, text in cm["data"].items():
        if Path(name).name != name:
            raise ValueError("unsafe_config_key")
        (args.output / name).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
