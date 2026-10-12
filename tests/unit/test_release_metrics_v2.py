from prometheus_client import CollectorRegistry, generate_latest
from control_plane.release_metrics_v2 import ReleaseCollector
from control_plane.state_v2 import initial_state
from test_controller_v2 import OLD
from test_processor_v2 import FP


def test_fresh_state_has_no_sensitive_or_unbounded_labels():
    state=initial_state(OLD,FP)
    state['audit']=[dict(reason='release_completed', time=10, pair=OLD)]
    collector=ReleaseCollector(clock=lambda:20);collector.update(state)
    registry=CollectorRegistry();registry.register(collector)
    text=generate_latest(registry).decode()
    assert 'mlops_v2_release_snapshot_fresh 1.0' in text
    assert 'mlops_v2_stable_version' in text and 'outcome="success"' in text
    assert OLD['storage_uri'] not in text and OLD['runtime_digest'] not in text
    assert not any(line.startswith('mlops_v2_candidate_version ') for line in text.splitlines())


def test_stale_metrics_are_absent_not_zero():
    now=[0]; collector=ReleaseCollector(clock=lambda:now[0]);collector.update(initial_state(OLD,FP))
    now[0]=11;registry=CollectorRegistry();registry.register(collector);text=generate_latest(registry).decode()
    assert 'mlops_v2_release_snapshot_fresh 0.0' in text
    assert 'mlops_v2_stable_version' not in text
