"""Fail-closed Prometheus controller. Demo thresholds, not production SLOs."""
import argparse
import json
import math
import os
import time
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

PROM = os.environ.get('CANARY_PROMETHEUS_URL', 'http://127.0.0.1:9090').rstrip('/')
ROUTER = os.environ.get('CANARY_ROUTER_URL', 'http://127.0.0.1:8080').rstrip('/')
SELECTOR = 'job="model-service",track="canary",method="POST",route="/api/v1/predict"'
QUERIES = {
    'up': 'min(up{job="model-service",track="canary"})',
    'stable_up': 'min(up{job="model-service",track="stable"})',
    'age': 'max(time() - timestamp(up{job="model-service",track="canary"}))',
    'stable_age': 'max(time() - timestamp(up{job="model-service",track="stable"}))',
    'traffic': 'sum(increase(model_service_http_requests_total{'+SELECTOR+'}[30s]))',
    'success': 'sum(increase(model_service_inference_requests_total{job="model-service",track="canary",outcome="success"}[30s]))',
    'error_ratio': 'sum(rate(model_service_http_requests_total{'+SELECTOR+',status=~"5.."}[30s])) / sum(rate(model_service_http_requests_total{'+SELECTOR+'}[30s]))',
    'p95': 'histogram_quantile(0.95, sum by (le) (rate(model_service_http_request_duration_seconds_bucket{'+SELECTOR+'}[30s])))',
    'proxy_error_ratio': 'sum(rate(canary_router_requests_total{track="canary",status=~"5.."}[30s])) / sum(rate(canary_router_requests_total{track="canary"}[30s]))',
}

def api(url, data=None):
    req = Request(url, data=json.dumps(data).encode() if data is not None else None,
                  headers={'Content-Type':'application/json'})
    with urlopen(req, timeout=4) as response: return json.load(response)

def query(expr, at=None):
    result = api(PROM+'/api/v1/query?'+urlencode({'query':expr,'time':at or time.time()}))
    if result.get('status') != 'success': raise ValueError('Prometheus query failed')
    rows = result['data']['result']
    if len(rows) != 1: raise ValueError('Expected one series: '+expr)
    value = float(rows[0]['value'][1])
    if not math.isfinite(value): raise ValueError('Non-finite metric: '+expr)
    return value

def observe():
    at = time.time()
    return {name:query(expr, at) for name,expr in QUERIES.items()}

def assess(m):
    reasons=[]
    for key in QUERIES:
        if key not in m or not math.isfinite(m[key]): reasons.append('missing/nonfinite '+key)
    if reasons: return reasons
    for key in ('up','stable_up'):
        if m[key] != 1: reasons.append(key+' != 1')
    for key in ('age','stable_age'):
        if not 0 <= m[key] <= 15: reasons.append(key+' stale')
    if m['traffic'] <= 0: reasons.append('zero canary traffic')
    if m['success'] <= 0: reasons.append('zero successful inference')
    for key in ('error_ratio','proxy_error_ratio'):
        if not 0 <= m[key] < .05: reasons.append(key+' >= 5%')
    if not 0 <= m['p95'] < .1: reasons.append('p95 >= 100ms')
    return reasons

def set_weight(value):
    result = api(ROUTER+'/__admin/weight', {'canary_weight':value})
    if result.get('canary_weight') != value: raise RuntimeError('Router did not acknowledge update')
    if api(ROUTER+'/__admin/state')['canary_weight'] != value: raise RuntimeError('Router readback mismatch')

def run(output, dwell=35, poll=3, duration=600):
    output=Path(output)
    output.parent.mkdir(parents=True,exist_ok=True)
    def emit(action, **kw):
        row={'time':time.time(),'action':action,**kw}
        with output.open('a',encoding='utf-8') as f: f.write(json.dumps(row,allow_nan=False)+'\n')
        print(json.dumps(row,allow_nan=False),flush=True)
    current=api(ROUTER+'/__admin/state')['canary_weight']
    if current != 10: raise RuntimeError('Start a fresh demo at 10%')
    started=changed=time.monotonic()
    emit('START',weight=current)
    while time.monotonic()-started < duration:
        time.sleep(poll)
        metrics={}
        try:
            for key in ('up','stable_up','age','stable_age'): metrics[key]=query(QUERIES[key])
            reasons=[key+' unavailable' for key in ('up','stable_up') if metrics[key]!=1]
            reasons += [key+' stale' for key in ('age','stable_age') if not 0<=metrics[key]<=15]
            if not reasons and time.monotonic()-changed < dwell: continue
            if not reasons:
                metrics=observe()
                reasons=assess(metrics)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            reasons=['metrics unavailable: '+str(exc)]
        if reasons:
            for attempt in range(3):
                try:
                    set_weight(0)
                    emit('ROLLBACK',previous_weight=current,weight=0,reasons=reasons,metrics=metrics)
                    return
                except Exception as exc:
                    emit('ROLLBACK_FAILED',attempt=attempt+1,error=str(exc))
                    time.sleep(1)
            raise RuntimeError('Rollback not acknowledged; operator action required')
        if current < 100:
            previous=current
            current={10:25,25:50,50:100}[current]
            set_weight(current)
            changed=time.monotonic()
            emit('PROMOTE',previous_weight=previous,weight=current,metrics=metrics)
        else: emit('HEALTHY',weight=current,metrics=metrics)
    emit('TIMEOUT',weight=current)

if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',required=True)
    parser.add_argument('--duration',type=float,default=600)
    args=parser.parse_args()
    run(args.output,duration=args.duration)
