"""Reviewed, bounded KServe-owned HPA policy; activation is explicit."""
import os

DEFAULTS = {'V2_STABLE_MAX_REPLICAS': '1', 'V2_CANARY_MAX_REPLICAS': '1', 'V2_HPA_CPU_TARGET': '80'}


def predictor_policy(lane, env=None):
    if lane not in ('stable', 'canary'):
        raise ValueError('Unknown serving lane')
    env = os.environ if env is None else env
    values = {}
    for key, default in DEFAULTS.items():
        raw = env.get(key, default)
        if not isinstance(raw, str) or not raw.isdecimal():
            raise ValueError('Invalid autoscaling setting: ' + key)
        values[key] = int(raw)
    if any(not 1 <= values[key] <= 2 for key in ('V2_STABLE_MAX_REPLICAS', 'V2_CANARY_MAX_REPLICAS')):
        raise ValueError('Replica ceiling exceeds reviewed policy')
    if sum(values[key] for key in ('V2_STABLE_MAX_REPLICAS', 'V2_CANARY_MAX_REPLICAS')) > 3:
        raise ValueError('Combined replica ceiling leaves no rollout headroom')
    target = values['V2_HPA_CPU_TARGET']
    if not 50 <= target <= 90:
        raise ValueError('CPU target outside reviewed range')
    maximum = values['V2_' + lane.upper() + '_MAX_REPLICAS']
    if maximum == 1:
        return dict(minReplicas=1, maxReplicas=1)
    return dict(minReplicas=1, maxReplicas=maximum, scaleMetric='cpu', scaleTarget=target,
        autoScaling=dict(behavior=dict(
            scaleUp=dict(stabilizationWindowSeconds=0, policies=[dict(type='Pods', value=1, periodSeconds=60)]),
            scaleDown=dict(stabilizationWindowSeconds=300, policies=[dict(type='Pods', value=1, periodSeconds=60)]))))
