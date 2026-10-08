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
