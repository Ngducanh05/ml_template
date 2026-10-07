import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import smoke_test

def call(url,data=None):
    request=Request(url,data=None if data is None else json.dumps(data).encode(),headers={'Content-Type':'application/json'})
    with urlopen(request,timeout=10) as response:
        raw=response.read().decode()
        return dict(status=response.status,headers=dict(response.headers),body=json.loads(raw) if 'json' in response.headers.get('Content-Type','') else raw)

def kube(*args):
    command=['kubectl','--kubeconfig',str(KUBECONFIG),'--context','ml-template-dev-sg','-n','production','--request-timeout=30s',*args]
    result=subprocess.run(command,capture_output=True,text=True,check=True)
    return result.stdout

def mutate(*args):
    context=subprocess.check_output(['kubectl','--kubeconfig',str(KUBECONFIG),'config','current-context'],text=True).strip()
    if context!='ml-template-dev-sg': raise RuntimeError('Wrong current context')
    return kube(*args)

@contextmanager
def forward(service,local,remote):
    with socket.socket() as test:
        test.bind(('127.0.0.1',local))
    log=(OUTPUT/f'port-forward-{service}.log').open('w')
    p=subprocess.Popen(['kubectl','--kubeconfig',str(KUBECONFIG),'--context','ml-template-dev-sg','-n','production','port-forward','--address','127.0.0.1',f'service/{service}',f'{local}:{remote}'],stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        for _ in range(60):
            if p.poll() is not None: raise RuntimeError('port-forward exited: '+service)
            try:
                with socket.create_connection(('127.0.0.1',local),timeout=.2): break
            except OSError: time.sleep(.5)
        else: raise TimeoutError('port-forward unavailable')
        yield f'http://127.0.0.1:{local}'
    finally:
        p.terminate()
        try: p.wait(timeout=10)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
        log.close()

def save(name,data):
    (OUTPUT/(name+'.json')).write_text(json.dumps(data,indent=2,allow_nan=False),encoding='utf-8')

def baseline():
    with forward('model-service',28081,8000) as base:
        smoke_test.run_smoke_test(base,10)
        result={p:call(base+p) for p in ('/health/live','/health/ready','/metrics')}
        result['/api/v1/predict']=call(base+'/api/v1/predict',{'instances':[smoke_test.VALID_INSTANCE]})
        assert 'model_service_http_requests_total' in result['/metrics']['body']
        save('baseline-smoke',result)
        print('PASS: /metrics; serving smoke complete',flush=True)
    save('baseline-pods',json.loads(kube('get','pods','-l','app.kubernetes.io/name=model-service','-o','json')))

def hpa():
    rows=[]
    for _ in range(45):
        state=json.loads(kube('get','hpa','model-service','-o','json'))
        row=dict(time=time.time(),status=state.get('status',{}))
        rows.append(row); save('hpa-observations',rows)
        print(json.dumps(row),flush=True)
        time.sleep(10)

def monitoring():
    with forward('prometheus',29090,9090) as prom, forward('grafana',23000,3000) as graf:
        targets=call(prom+'/api/v1/targets')['body']
        save('prometheus-targets',targets)
        assert targets['data']['activeTargets'] and all(x['health']=='up' for x in targets['data']['activeTargets'])
        query='model_service_build_info'
        result=call(prom+'/api/v1/query?'+urlencode({'query':query}))['body']
        assert result['data']['result']; save('prometheus-query',result)
        health=call(graf+'/api/health')['body']; assert health['database']=='ok'
        dashboard=call(graf+'/api/dashboards/uid/local-canary')['body']
        assert dashboard['dashboard']['panels']; save('grafana-dashboard',dashboard)
        data=call(graf+'/api/datasources/proxy/uid/canary-prom/api/v1/query?'+urlencode({'query':query}))['body']
        assert data['data']['result']; save('grafana-live-data',data)
        print('PASS: Prometheus targets UP, real model metrics, Grafana dashboard and datasource query',flush=True)

def canary():
    with forward('canary-router',28080,8080) as router, forward('prometheus',29090,9090) as prom:
        payload={'instances':[smoke_test.VALID_INSTANCE]}
        def request():
            try:
                response=call(router+'/api/v1/predict',payload)
                return {'status':response['status'],'track':response['headers'].get('X-Lab-Route'),'body':response['body']}
            except Exception as exc: return {'error':str(exc)}
        initial=[request() for _ in range(100)]
        assert all(x.get('status')==200 for x in initial)
        counts=Counter(x['track'] for x in initial)
        save('routing-initial',dict(counts=counts,responses=initial))
        assert counts=={'stable':90,'canary':10},counts
        print('INITIAL: 90 stable / 10 canary, all successful',flush=True)
        rows=[]; failure=False; restored=False
        # Match the existing bounded local demo: <=20 requests/sec, <=4 in flight.
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending=set(); start=time.monotonic(); last_state=0
            mutate('scale','deployment/canary-evaluator','--replicas=1')
            try:
                while time.monotonic()-start<300:
                    pending={f for f in pending if not f.done()}
                    if len(pending)<4: pending.add(pool.submit(request))
                    if time.monotonic()-last_state>=3:
                        state=call(router+'/__admin/state')['body']
                        row={'time':time.time(),**state}; rows.append(row);save('routing-progress',rows)
                        print(json.dumps(row),flush=True);last_state=time.monotonic()
                        weight=state['canary_weight']
                        if weight==100 and not failure:
                            events=kube('logs','deployment/canary-evaluator')
                            (OUTPUT/'evaluator-before-failure.jsonl').write_text(events)
                            assert all(f'"weight": {w}' in events for w in (25,50,100))
                            print(mutate('scale','deployment/model-service-canary','--replicas=0'),flush=True)
                            save('failure-injection',{'time':time.time(),'mechanism':'scale only canary deployment to zero','previous_replicas':1})
                            failure=True
                        elif weight==0:
                            if not failure: raise RuntimeError('Unexpected rollback before healthy progression')
                            break
                    time.sleep(.05)
                else: raise TimeoutError('Canary progression/rollback timed out')
            finally:
                if failure:
                    print(mutate('scale','deployment/model-service-canary','--replicas=1'),flush=True)
                    restored=True
        final=[request() for _ in range(100)]
        assert all(x.get('status')==200 and x.get('track')=='stable' for x in final)
        save('rollback-routing',dict(counts=dict(Counter(x['track'] for x in final)),responses=final,restored_canary=restored))
        events=kube('logs','deployment/canary-evaluator')
        (OUTPUT/'evaluator-events.jsonl').write_text(events)
        assert '"action": "ROLLBACK"' in events
        print('PASS: 10 -> 25 -> 50 -> 100 -> automatic 0; 100/100 stable after rollback; canary restoration requested',flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['baseline','hpa','monitoring','canary'])
    parser.add_argument('--kubeconfig',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); KUBECONFIG=args.kubeconfig;OUTPUT=args.output;OUTPUT.mkdir(exist_ok=True,parents=True)
    globals()[args.mode]()
