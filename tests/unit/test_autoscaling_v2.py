import copy
import pytest
from control_plane.autoscaling_v2 import predictor_policy
from control_plane.kserve_effects import lane_manifest, KServeEffects
from test_controller_v2 import OLD
from test_processor_v2 import FP


def test_default_does_not_activate_scaling():
    assert predictor_policy('stable', {}) == dict(minReplicas=1, maxReplicas=1)
    assert predictor_policy('canary', {}) == dict(minReplicas=1, maxReplicas=1)


@pytest.mark.parametrize('settings', [
    {'V2_STABLE_MAX_REPLICAS':'0'}, {'V2_STABLE_MAX_REPLICAS':'3'},
    {'V2_STABLE_MAX_REPLICAS':'2','V2_CANARY_MAX_REPLICAS':'2'},
    {'V2_HPA_CPU_TARGET':'1'}, {'V2_HPA_CPU_TARGET':'nan'}])
def test_unreviewed_capacity_or_cpu_target_rejected(settings):
    with pytest.raises(ValueError): predictor_policy('stable',settings)


def test_hpa_keeps_serving_identity_and_owned_replica_control(monkeypatch):
    before=lane_manifest('stable',OLD,FP)
    monkeypatch.setenv('V2_STABLE_MAX_REPLICAS','2')
    after=lane_manifest('stable',OLD,FP)
    assert before['metadata']==after['metadata']
    assert before['spec']['predictor']['model']==after['spec']['predictor']['model']
    policy=after['spec']['predictor']
    assert policy['minReplicas']==1 and policy['maxReplicas']==2
    assert policy['scaleTarget']==80 and policy['scaleMetric']=='cpu'
    assert policy['autoScaling']['behavior']['scaleDown']['stabilizationWindowSeconds']==300
    assert not KServeEffects.lane_matches(before,after)
    assert KServeEffects.lane_matches(after,after)
    changed=copy.deepcopy(after); changed['spec']['predictor']['scaleTarget']=50
    assert not KServeEffects.lane_matches(changed,after)
    assert lane_manifest('canary',OLD,FP)['spec']['predictor']['maxReplicas']==1


@pytest.mark.parametrize('replicas', [1,2])
def test_readiness_requires_complete_rollout_at_hpa_replica_count(replicas):
    from control_plane.kserve_effects import deployment_ready
    obj=dict(metadata=dict(generation=2),spec=dict(replicas=replicas,
        template=dict(metadata=dict(annotations={'ml-template.io/model-version':OLD['model_version'],
            'internal.serving.kserve.io/storage-initializer-sourceuri':OLD['storage_uri']}),
        spec=dict(containers=[dict(name='kserve-container',image=OLD['runtime_digest'])]))),
        status=dict(observedGeneration=2,replicas=replicas,updatedReplicas=replicas,readyReplicas=replicas,availableReplicas=replicas))
    assert deployment_ready(obj,OLD)
    obj['status']['replicas']+=1
    assert not deployment_ready(obj,OLD)
    obj['status']['replicas']=replicas
    obj['status']['updatedReplicas']=0
    assert not deployment_ready(obj,OLD)
    obj['status']['updatedReplicas']=replicas
    obj['spec']['template']['metadata']['annotations']['ml-template.io/model-version']='999'
    assert not deployment_ready(obj,OLD)
