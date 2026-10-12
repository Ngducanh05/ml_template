import json
from pathlib import Path
from scripts.build_dashboard_v2 import build

ROOT=Path(__file__).resolve().parents[1]


def dashboard():return json.loads((ROOT/'k8s/monitoring/grafana/dashboards/mlops_v2_infra.json').read_text(encoding='utf-8'))


def test_identity_variables_defaults_and_source_reproducible():
    d=dashboard();assert d==build()
    assert d['uid']=='mlops-v2-infra-serving' and not d['editable']
    assert d['title']=='MLOps v2 \u2014 Infrastructure & Serving'
    assert int(d['refresh'][:-1])>=15 and d['time']=={'from':'now-1h','to':'now'}
    variables={x['name']:x for x in d['templating']['list']}
    assert set(variables)>={'DS_PROMETHEUS','namespace','model','service','node','track','node_hourly_usd'}
    assert variables['namespace']['current']['value']=='production'
    assert 'ml-model-service-p0' not in json.dumps(d)


def test_unique_panels_descriptions_no_data_and_semantics():
    d=dashboard();panels=d['panels']
    assert len({x['id'] for x in panels})==len(panels)
    assert len({x['title'] for x in panels})==len(panels)
    for p in panels:
        assert p['description'] and p['fieldConfig']['defaults']['noValue']=='NO DATA'
        assert p['datasource']['uid']=='${DS_PROMETHEUS}'
        assert not p['fieldConfig']['defaults']['custom']['spanNulls']
        if p['title'] in ('5xx error ratio','p50 / p95 latency'):
            assert all('or vector(0)' not in x['expr'] and 'clamp_min' not in x['expr'] and '> 0' in x['expr'] for x in p['targets'])
    encoded=json.dumps(d)
    for metric in ('kube_node_info','kube_node_status_condition','kube_node_status_allocatable','container_cpu_usage_seconds_total','container_memory_working_set_bytes','kube_pod_container_resource_requests','kube_pod_container_resource_limits','kube_pod_container_status_restarts_total','kube_pod_container_status_ready','kube_horizontalpodautoscaler_status_desired_replicas','mlops_v2_kserve_isvc_ready','mlops_v2_stable_version','mlops_v2_candidate_version','mlops_v2_release_phase','mlops_v2_pending_releases','mlops_v2_last_outcome_timestamp_seconds','mlops_v2_stage_promotion_timestamp_seconds','credit_risk_router_requests_total','credit_risk_router_weight_percent','credit_risk_router_state_age_seconds'):
        assert metric in encoded
    assert 'storage_uri=' not in encoded and 'runtime_digest=' not in encoded


def test_configmap_is_exact_source_and_folder():
    cm=json.loads((ROOT/'k8s/monitoring/grafana/dashboard-mlops-v2-infra.configmap.yaml').read_text(encoding='utf-8'))
    assert cm['metadata']['namespace']=='monitoring'
    assert cm['metadata']['annotations']['grafana_folder']=='MLOps v2'
    assert json.loads(cm['data']['mlops_v2_infra.json'])==dashboard()
