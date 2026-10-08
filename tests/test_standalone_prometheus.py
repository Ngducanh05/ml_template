import copy
import pytest
import yaml
from scripts.prepare_standalone_prometheus import JOB, prepare


def fixtures():
    cm = {"metadata": {"name": "old", "namespace": "production"}, "data": {
        "prometheus.yml": yaml.safe_dump({"global": {"scrape_interval": "5s"}, "rule_files": ["/etc/prometheus/alerts.yml"],
          "scrape_configs": [{"job_name": "legacy", "static_configs": [{"targets": ["legacy:9090"]}]}]}),
        "alerts.yml": "groups: []\n"}}
    dep = {"metadata": {"name": "prometheus", "resourceVersion": "42"}, "spec": {"template": {"spec": {
        "volumes": [{"name": "data", "emptyDir": {}}, {"name": "config", "configMap": {"name": "old"}}]}}}}
    return cm, dep


def test_preserves_jobs_rules_and_volume_with_guarded_targeted_patch():
    cm, dep = fixtures()
    original = copy.deepcopy((cm, dep))
    new, patch, rollback = prepare(cm, dep)
    old_config = yaml.safe_load(cm["data"]["prometheus.yml"])
    new_config = yaml.safe_load(new["data"]["prometheus.yml"])
    assert new_config.pop("scrape_configs") == old_config.pop("scrape_configs") + [JOB]
    assert new_config == old_config
    assert new["data"]["alerts.yml"] == cm["data"]["alerts.yml"]
    assert (cm, dep) == original
    assert new["immutable"] is True
    assert patch[0] == {"op": "test", "path": "/metadata/resourceVersion", "value": "42"}
    assert patch[-1]["path"] == "/spec/template/spec/volumes/1/configMap/name"
    assert rollback[-1]["value"] == "old"
    assert prepare(cm, dep) == (new, patch, rollback)


def test_idempotent_job_and_reject_conflicting_existing_target():
    cm, dep = fixtures()
    config = yaml.safe_load(cm["data"]["prometheus.yml"])
    config["scrape_configs"].append(copy.deepcopy(JOB))
    cm["data"]["prometheus.yml"] = yaml.safe_dump(config)
    new, _, _ = prepare(cm, dep)
    assert len(yaml.safe_load(new["data"]["prometheus.yml"])["scrape_configs"]) == 2
    config["scrape_configs"][-1]["static_configs"][0]["targets"] = ["unexpected:9093"]
    cm["data"]["prometheus.yml"] = yaml.safe_dump(config)
    with pytest.raises(ValueError, match="conflicting"):
        prepare(cm, dep)


def test_reject_stale_config_snapshot():
    cm, dep = fixtures()
    cm["metadata"]["name"] = "different"
    with pytest.raises(ValueError, match="snapshot"):
        prepare(cm, dep)


def test_infrastructure_secure_scrapes_and_targeted_identity_rollback():
    cm, dep = fixtures()
    dep["spec"]["template"]["spec"]["automountServiceAccountToken"] = False
    new, patch, rollback = prepare(cm, dep, infrastructure=True)
    jobs = yaml.safe_load(new["data"]["prometheus.yml"])["scrape_configs"]
    assert [x["job_name"] for x in jobs] == ["legacy", "mlops-v2-state-exporter", "kube-state-metrics-v2", "kubelet-cadvisor-v2"]
    kubelet = jobs[-1]
    assert kubelet["scheme"] == "https" and kubelet["tls_config"]["insecure_skip_verify"] is False
    assert kubelet["authorization"]["credentials_file"].endswith("/token")
    assert any(x.get("value") == "prometheus-kubelet-v2" for x in patch)
    assert {"op": "replace", "path": "/spec/template/spec/automountServiceAccountToken", "value": False} in rollback
    assert {"op": "remove", "path": "/spec/template/spec/serviceAccountName"} in rollback


def test_kubelet_and_ksm_rbac_are_read_only_and_scoped():
    from pathlib import Path
    kubelet = list(yaml.safe_load_all(Path("k8s/monitoring/standalone/kubelet-rbac.yaml").read_text()))
    role = next(x for x in kubelet if x["kind"] == "ClusterRole")
    assert role["rules"] == [
        {"apiGroups": [""], "resources": ["nodes"], "verbs": ["get", "list", "watch"]},
        {"apiGroups": [""], "resources": ["nodes/metrics"], "verbs": ["get"]}]
    ksm = list(yaml.safe_load_all(Path("k8s/monitoring/standalone/ksm/resources.yaml").read_text()))
    cluster = next(x for x in ksm if x["kind"] == "ClusterRole")
    assert cluster["rules"] == [{"apiGroups": [""], "resources": ["nodes"], "verbs": ["list", "watch"]}]
    namespaced = next(x for x in ksm if x["kind"] == "Role")
    assert namespaced["metadata"]["namespace"] == "production"
    assert {r for rule in namespaced["rules"] for r in rule["resources"]} == {"pods", "deployments", "horizontalpodautoscalers"}
    assert all(rule["verbs"] == ["list", "watch"] for rule in namespaced["rules"])
    container = next(x for x in ksm if x["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
    assert "--namespaces=production" in container["args"]
    assert container["securityContext"]["readOnlyRootFilesystem"] is True


def test_ksm_render_requires_digest_and_has_only_component_objects():
    from scripts.render_standalone_ksm import render
    with pytest.raises(ValueError, match="digest"):
        render("example.invalid/ksm:latest")
    image = "example.invalid/ksm@sha256:" + "a" * 64
    objects = render(image)["items"]
    assert len(objects) == 7
    assert all(x["metadata"]["name"] == "kube-state-metrics-v2" for x in objects)
    assert next(x for x in objects if x["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]["image"] == image
