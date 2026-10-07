import copy
import json
from unittest.mock import Mock
from urllib.error import URLError
import pytest
from control_plane.artifact_metadata import validate_mlmodel
from control_plane.runtime_resolver import ArtifactContract, normalize_requirements
from control_plane.kserve_effects import KServeEffects, runtime_manifest, lane_manifest, LANES
from control_plane.reconciliation_v2 import Reconciler
from control_plane.state_v2 import Store
from test_state_v2 import Backend, event
from test_processor_v2 import FP, IMAGE
from test_controller_v2 import OLD, setup, until

CONTRACT = ArtifactContract('3.12.14', normalize_requirements('mlflow==3.14.0\nscikit-learn==1.7.2'))
MODEL = dict(mlflow_version='3.14.0', flavors=dict(
    python_function=dict(loader_module='mlflow.sklearn', model_path='model.skops', predict_fn='predict', python_version='3.12.14'),
    sklearn=dict(code=None, serialization_format='skops', pickled_model='model.skops', sklearn_version='1.7.2', skops_trusted_types=[])))


def test_artifact_surface_checked_without_hashing_model_identity():
    validate_mlmodel(json.dumps(MODEL), CONTRACT)
    for field, value in [('serialization_format', 'pickle'), ('code', 'code'), ('skops_trusted_types', ['evil.Type'])]:
        bad = copy.deepcopy(MODEL); bad['flavors']['sklearn'][field] = value
        with pytest.raises(ValueError): validate_mlmodel(json.dumps(bad), CONTRACT)
    bad = copy.deepcopy(MODEL); bad['mlflow_version'] = '3.15.0'
    with pytest.raises(ValueError): validate_mlmodel(json.dumps(bad), CONTRACT)


def test_kserve_ready_requires_generation_pair_model_health_and_metadata():
    runtime = runtime_manifest(FP, IMAGE)
    isvc = lane_manifest('stable', OLD, FP)
    isvc['metadata'].update(generation=2, resourceVersion='10')
    isvc['status'] = dict(observedGeneration=1, conditions=[dict(type='Ready', status='True')],
                          address=dict(url='http://credit-risk-stable-v2-predictor.production.svc.cluster.local'))
    api = Mock(); api.get.side_effect = lambda kind, name: runtime if kind == 'servingruntimes' else isvc
    probe = Mock(return_value=dict(name=LANES['stable'], inputs=[{}], outputs=[{}]))
    effect = KServeEffects(api, Mock(), Mock(), probe)
    assert not effect.ready('stable', OLD, FP); probe.assert_not_called()
    isvc['status']['observedGeneration'] = 2
    assert effect.ready('stable', OLD, FP)
    assert any('/v2/models/credit-risk-stable-v2/ready' in c.args[0] for c in probe.call_args_list)
    probe.return_value['name'] = 'wrong'
    with pytest.raises(ValueError): effect.ready('stable', OLD, FP)
    isvc['spec']['predictor']['model']['storageUri'] = event(9)['artifact_uri']
    assert not effect.ready('stable', OLD, FP)


def test_reconciliation_interval_stale_new_and_error_backoff():
    store = Store(Backend()); clock = [0]; source = Mock(return_value=event(5))
    r = Reconciler(store, source, clock=lambda: clock[0])
    assert r.tick() == 'accepted'; assert r.tick() is None
    clock[0] = 900; source.return_value = event(4); assert r.tick() == 'stale'
    clock[0] = 1800; source.side_effect = OSError(); assert r.tick() == 'blocked'
    assert r.tick() is None and source.call_count == 3


def test_finalizing_transport_then_success():
    _, s, e, _, c = setup(); until(c, s, 'FINALIZING')
    def transient(*args): raise URLError('transient')
    e.failure = transient; c.tick()
    assert s.read()['stable'] == OLD
    e.failure = None; c.tick(); c.tick()
    assert s.read()['stable']['model_version'] == '3' and s.read()['active'] is None


@pytest.mark.parametrize('track', ['stable', 'canary'])
def test_backend_failure_before_finalizing_preserves_stable(track):
    _, s, e, _, c = setup(); until(c, s, 'RAMP')
    e.failure = lambda lane, pair: lane != track
    c.tick(); assert s.read()['active']['phase'] == 'ROLLBACK'
    e.failure = None; c.tick(); assert s.read()['stable'] == OLD


def test_no_generic_requirement_wildcards():
    with pytest.raises(ValueError): normalize_requirements('mlflow==3.*')

def test_kube_v2_configmap_cas_is_isolated():
    from control_plane.kube_v2 import KubeV2
    from control_plane.state_v2 import initial_state
    kube = object.__new__(KubeV2); kube.ns = 'production'; kube.name = 'model-release-state-v2'
    kube.request = Mock(return_value={'metadata':{'resourceVersion':'17'}, 'data':{'state.json':json.dumps(initial_state())}})
    rev, state = kube.read_state()
    kube.write_state(rev, state)
    assert kube.request.call_args.args[0].endswith('/configmaps/model-release-state-v2')
    assert kube.request.call_args.kwargs['data']['metadata']['resourceVersion'] == '17'
    with pytest.raises(ValueError): kube.path('inferenceservices', 'credit-risk-v3-gate1')


def test_ready_never_creates_missing_runtime():
    isvc = lane_manifest('stable', OLD, FP)
    isvc['metadata']['generation'] = 1
    isvc['status'] = {'observedGeneration':1, 'conditions':[{'type':'Ready','status':'True'}]}
    api = Mock(); api.get.side_effect = lambda kind, name: isvc if kind == 'inferenceservices' else None
    effect = KServeEffects(api, Mock(), Mock())
    with pytest.raises(ValueError): effect.ready('stable', OLD, FP)
    api.create.assert_not_called()


def test_foreign_lane_and_image_override_fail_closed():
    desired = lane_manifest('stable', OLD, FP)
    foreign = copy.deepcopy(desired); foreign['metadata']['annotations'].pop('ml-template.io/control-plane')
    with pytest.raises(ValueError): KServeEffects.lane_matches(foreign, desired)
    compromised = copy.deepcopy(desired); compromised['spec']['predictor']['model']['image'] = 'other:latest'
    assert not KServeEffects.lane_matches(compromised, desired)
