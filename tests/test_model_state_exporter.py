import copy
from unittest.mock import Mock
from prometheus_client import CollectorRegistry,generate_latest
from control_plane.exporters.model_state_exporter import ModelStateCollector


def api():
    client=Mock(); client.get.return_value={'metadata':{'generation':3,'annotations':{'serving.kserve.io/deploymentMode':'Standard'}},'status':{'observedGeneration':2,'conditions':[{'type':'Ready','status':'True'}]}}
    client.read_state.return_value=('1',{'audit':[{'time':10,'reason':'release_completed','storage_uri':'sensitive','runtime_digest':'sensitive'}]})
    return client


def text(c):
    registry=CollectorRegistry();registry.register(c);return generate_latest(registry).decode()


def test_only_get_exact_lanes_and_metadata():
    client=api();c=ModelStateCollector(client);c.poll();output=text(c)
    assert [x.args for x in client.get.call_args_list]==[('inferenceservices','credit-risk-stable-v2'),('inferenceservices','credit-risk-canary-v2')]
    assert set(x[0] for x in client.mock_calls)=={'get','read_state'}
    assert 'latest_ready_revision="N/A"' in output and 'deployment_mode="Standard"' in output
    assert 'generation' in output and ' 3.0' in output and ' 2.0' in output
    assert 'storage_uri' not in output and 'runtime_digest' not in output and 'sensitive' not in output
    assert 'mlops_v2_stable_version' not in output


def test_existing_audit_not_replayed_new_rows_once_and_restart_baselines():
    client=api();c=ModelStateCollector(client);c.poll();assert not c.events
    client.read_state.return_value[1]['audit'] += [{'time':11,'reason':'stage_promoted','previous_weight':25},{'time':12,'reason':'anything-unbounded'}]
    c.poll();c.poll()
    assert c.events=={('stage_promoted','25'):1,('other','none'):1}
    restarted=ModelStateCollector(client);restarted.poll();assert not restarted.events
    assert 'anything-unbounded' not in text(c)


def test_ready_false_missing_and_api_failure_recovery():
    client=api();c=ModelStateCollector(client);c.poll()
    client.get.return_value['status']['conditions'][0]['status']='False';c.poll()
    assert any(line.startswith('mlops_v2_kserve_isvc_ready{') and line.endswith(' 0.0') for line in text(c).splitlines())
    client.get.side_effect=OSError('private-token');c.poll();assert 'mlops_v2_state_exporter_up 0.0' in text(c)
    assert 'mlops_v2_kserve_isvc_ready' not in text(c) and 'private-token' not in text(c)
    client.get.side_effect=None;c.poll();assert 'mlops_v2_state_exporter_up 1.0' in text(c)


def test_stale_poll_does_not_leave_healthy_data():
    now=[0];c=ModelStateCollector(api(),clock=lambda:now[0]);c.poll();now[0]=46
    assert 'mlops_v2_state_exporter_up 0.0' in text(c)


def test_exporter_rbac_get_only_and_no_duplicate_release_exporter():
    from scripts.render_dashboard_v2 import render
    from control_plane.state_v2 import REPOSITORY
    items=render(REPOSITORY+'@sha256:'+'a'*64)['items']
    role=next(x for x in items if x['kind']=='Role')
    assert all(x['verbs']==['get'] for x in role['rules'])
    assert role['rules'][0]['resourceNames']==['model-release-state-v2']
    dep=next(x for x in items if x['kind']=='Deployment');container=dep['spec']['template']['spec']['containers'][0]
    assert '@sha256:' in container['image'] and container['securityContext']['readOnlyRootFilesystem']
    assert container['securityContext']['capabilities']['drop']==['ALL']
