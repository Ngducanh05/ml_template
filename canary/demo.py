"""Self-contained Windows Canary demo: python canary/demo.py run|stop."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import bootstrap
from evaluator import api, query, QUERIES

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RUNTIME = HERE/'.runtime'
OUTPUT = HERE/'output'
PIDFILE = RUNTIME/'processes.json'
STABLE_PORT, CANARY_PORT = 18081, 18082
FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
PAYLOAD = {'instances':[{'sepal_length':5.1,'sepal_width':3.5,'petal_length':1.4,'petal_width':.2}]}
HASHES = {'stable.joblib':'c8639c3d762655b1aab1805e84fae97daa08d7815a3c28a0e5dcb9c2420f0c59',
          'canary-v2.joblib':'1667c4559ced9ef58a2181eefa626694c9ee6735ac0b15e7983018aefb28de35'}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix+'.tmp')
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def process_snapshot():
    script = "@((Get-CimInstance Win32_Process -ErrorAction Stop) | ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; parent=[int]$_.ParentProcessId; created=$_.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString(); executable=$_.ExecutablePath; command=$_.CommandLine} }) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe','-NoProfile','-Command',script], capture_output=True,
                            text=True, encoding='utf-8', errors='replace', timeout=20, creationflags=FLAGS)
    if result.returncode: raise RuntimeError('Cannot inspect demo process identities: '+result.stderr[-1200:])
    data = json.loads(result.stdout or '[]')
    if isinstance(data, dict): data = [data]
    return {row['pid']:row for row in data}


def same_process(current, recorded):
    return bool(current and all(current.get(k)==recorded.get(k) for k in ('pid','created','executable','command')))


def owned_tree(record, snapshot):
    selected = {}
    for identity in [record['identity']]+record.get('children',[]):
        live = snapshot.get(identity['pid'])
        if same_process(live, identity): selected[live['pid']]=live
    while True:
        additions = {pid:row for pid,row in snapshot.items() if row['parent'] in selected and pid not in selected
                     and int(row['created']) >= int(selected[row['parent']]['created'])}
        if not additions: break
        selected.update(additions)
    return selected


def stop_record(record):
    snapshot = process_snapshot()
    owned = owned_tree(record, snapshot)
    # Single PowerShell call rechecks creation time, executable AND command line
    # for every PID immediately before stopping it (protects against PID reuse).
    rows = sorted(owned.values(), key=lambda x:int(x['created']), reverse=True)
    if not rows: return []
    payload = json.dumps(rows).replace("'", "''")
    script = "$ErrorActionPreference='Stop'; $records=ConvertFrom-Json '"+payload+"'; foreach($r in $records) { $p=Get-CimInstance Win32_Process -Filter ('ProcessId='+$r.pid); if($p -and $p.CreationDate.ToUniversalTime().ToFileTimeUtc().ToString() -eq $r.created -and $p.ExecutablePath -eq $r.executable -and $p.CommandLine -eq $r.command) { Stop-Process -Id $r.pid -ErrorAction Stop } }"
    result = subprocess.run(['powershell.exe','-NoProfile','-Command',script], capture_output=True,
                            text=True, timeout=25, creationflags=FLAGS)
    if result.returncode: raise RuntimeError('Stopping demo processes failed: '+result.stderr[-1200:])
    return [row['pid'] for row in rows]


def stop():
    if not PIDFILE.exists():
        print('No recorded demo processes.', flush=True)
        return
    state = json.loads(PIDFILE.read_text(encoding='utf-8'))
    if state.get('owner') != str(HERE) or state.get('schema') != 1:
        raise RuntimeError('Process metadata does not belong to this demo; refusing to stop processes')
    # Controller first: it owns the in-process load thread. Remaining service
    # identities are also recorded independently in case the controller exited.
    for name in ('controller','evaluator','grafana','prometheus','router','canary','stable'):
        if name in state['processes']:
            stopped = stop_record(state['processes'][name])
            print(f'{name}: stopped verified PIDs {stopped}', flush=True)
    snapshot = process_snapshot()
    remaining = [name for name,record in state['processes'].items() if owned_tree(record,snapshot)]
    if remaining: raise RuntimeError('Demo processes still alive: '+', '.join(remaining))
    save(RUNTIME/'last-stop.json', {'time':time.time(),'status':'PASS','processes':list(state['processes'])})
    PIDFILE.unlink()


def tail(path, lines=18):
    try: return '\n'.join(path.read_text(encoding='utf-8',errors='replace').splitlines()[-lines:])
    except OSError: return '(no log available)'


def wait_for(predicate, timeout=60, *, label='condition', processes=()):
    deadline=time.monotonic()+timeout
    last_error=''
    while time.monotonic()<deadline:
        for name,proc,log in processes:
            code=proc.poll()
            if code is not None:
                raise RuntimeError(f'{name} exited before {label}; returncode={code}\nLog: {log}\n{tail(log)}')
        try:
            value=predicate()
            if value: return value
        except (OSError, ValueError, KeyError) as exc: last_error=str(exc)
        time.sleep(.5)
    logs='\n'.join(f'{name}: {tail(log)}' for name,_,log in processes)
    raise RuntimeError(f'Timed out waiting for {label}: {last_error}\n{logs}')


def ready(port,path):
    with urlopen(f'http://127.0.0.1:{port}{path}',timeout=2) as response: return response.status==200


def predict():
    started=time.time()
    req=Request('http://127.0.0.1:8080/api/v1/predict',data=json.dumps(PAYLOAD).encode(),headers={'Content-Type':'application/json'})
    try:
        with urlopen(req,timeout=4) as response:
            body=json.load(response)
            return {'time':started,'track':response.headers.get('X-Lab-Route'),'status':response.status,'valid':body=={'predictions':['setosa']}}
    except HTTPError as exc:
        return {'time':started,'track':exc.headers.get('X-Lab-Route'),'status':exc.code,'valid':False}
    except OSError:
        return {'time':started,'track':'unknown','status':0,'valid':False}


def configure():
    bootstrap.require_windows()
    prom,_=bootstrap.validate_installation('prometheus')
    graf,graf_home=bootstrap.validate_installation('grafana')
    for part in ('prometheus-data','grafana-data','grafana-logs','grafana-plugins','provisioning/datasources','provisioning/dashboards'):
        (RUNTIME/part).mkdir(parents=True,exist_ok=True)
    dashboard=json.loads((HERE/'dashboard.json').read_text(encoding='utf-8-sig'))
    if dashboard['uid']!='local-canary': raise RuntimeError('Expected dashboard UID local-canary')
    config=(HERE/'prometheus.yml').read_text(encoding='utf-8-sig').replace('CANARY_ALERT_RULES',(HERE/'alerts.yml').as_posix())
    (RUNTIME/'prometheus.yml').write_text(config,encoding='utf-8')
    (RUNTIME/'provisioning/datasources/prometheus.yml').write_text('apiVersion: 1\ndatasources:\n  - name: Canary Prometheus\n    uid: canary-prom\n    type: prometheus\n    access: proxy\n    url: http://127.0.0.1:9090\n    isDefault: true\n    jsonData:\n      timeInterval: 5s\n',encoding='utf-8')
    # Grafana reads the single canonical dashboard file; no source copies.
    (RUNTIME/'provisioning/dashboards/canary.yml').write_text('apiVersion: 1\nproviders:\n  - name: Local Canary\n    type: file\n    updateIntervalSeconds: 5\n    options:\n      path: '+(HERE/'dashboard.json').as_posix()+'\n',encoding='utf-8')
    ini='[server]\nhttp_addr = 127.0.0.1\nhttp_port = 3000\n[paths]\n'
    for key,part in [('data','grafana-data'),('logs','grafana-logs'),('plugins','grafana-plugins'),('provisioning','provisioning')]:
        ini+=f'{key} = {(RUNTIME/part).as_posix()}\n'
    ini+='[auth.anonymous]\nenabled = true\norg_role = Viewer\n[analytics]\nreporting_enabled = false\ncheck_for_updates = false\n[news]\nnews_feed_enabled = false\n[plugins]\npreinstall_disabled = true\n'
    (RUNTIME/'grafana.ini').write_text(ini,encoding='utf-8')
    return prom,graf,graf_home


def run():
    if PIDFILE.exists(): raise RuntimeError('Previous demo metadata exists. Run python canary/demo.py stop first.')
    for port in (8080,STABLE_PORT,CANARY_PORT,9090,3000):
        with socket.socket() as sock:
            sock.settimeout(1)
            if sock.connect_ex(('127.0.0.1',port))==0: raise RuntimeError(f'Port {port} occupied; no existing process was stopped')
    actual={name:bootstrap.digest(HERE/'artifacts'/name) for name in HASHES}
    if actual!=HASHES: raise RuntimeError('Artifact checksum mismatch')
    prom,graf,graf_home=configure()
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    evidence=OUTPUT/('canary-'+stamp)
    evidence.mkdir(parents=True)
    started=time.time()
    snapshot=process_snapshot()
    state={'schema':1,'owner':str(HERE),'run':str(evidence),'processes':{'controller':{'identity':snapshot[os.getpid()],'children':[]}}}
    save(PIDFILE,state)
    processes={}; handles=[]; load_stop=threading.Event(); load_errors=[]; thread=None
    env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8',
             CANARY_STABLE_PORT=str(STABLE_PORT),CANARY_CANARY_PORT=str(CANARY_PORT))
    def launch(name,args,extra=None):
        logfile=evidence/(name+'.log')
        stream=logfile.open('a',encoding='utf-8'); handles.append(stream)
        proc=subprocess.Popen([str(x) for x in args],cwd=ROOT,env=dict(env,**(extra or {})),stdout=stream,stderr=subprocess.STDOUT,creationflags=FLAGS)
        processes[name]=(name,proc,logfile)
        snap=process_snapshot()
        if proc.pid not in snap:
            raise RuntimeError(f'{name} exited at launch; returncode={proc.poll()}\n{tail(logfile)}')
        record={'identity':snap[proc.pid],'children':[]}
        record['children']=[row for pid,row in owned_tree(record,snap).items() if pid!=proc.pid]
        state['processes'][name]=record
        save(PIDFILE,state)
        return proc
    def model(track,port,artifact):
        return launch(track,[sys.executable,'-m','uvicorn','model_service.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                      {'MODEL_PATH':str(HERE/'artifacts'/artifact),'MODEL_NAME':'iris-'+track})
    def load():
        try:
            with (evidence/'traffic.jsonl').open('w',encoding='utf-8') as f, ThreadPoolExecutor(4) as pool:
                pending=[]
                while not load_stop.is_set():
                    if len(pending)<4: pending.append(pool.submit(predict))
                    remaining=[]
                    for job in pending:
                        if job.done(): f.write(json.dumps(job.result())+'\n')
                        else: remaining.append(job)
                    pending=remaining; f.flush(); load_stop.wait(.05)
                for job in pending: f.write(json.dumps(job.result())+'\n')
        except Exception as exc: load_errors.append(str(exc))
    def targets_ready():
        targets=api('http://127.0.0.1:9090/api/v1/targets')['data']['activeTargets']
        models=[row for row in targets if row['labels'].get('job')=='model-service']
        return len(models)==2 and {row['labels'].get('track') for row in models}=={'stable','canary'} and all(row['health']=='up' for row in models)
    def grafana_data():
        result=api('http://127.0.0.1:3000/api/datasources/proxy/uid/canary-prom/api/v1/query?'+urlencode({'query':'up{job="model-service"}'}))
        if result.get('status')!='success' or len(result['data']['result'])!=2: return False
        return result
    try:
        model('stable',STABLE_PORT,'stable.joblib')
        wait_for(lambda:ready(STABLE_PORT,'/health/ready'),label='stable ready',processes=[processes['stable']])
        model('canary',CANARY_PORT,'canary-v2.joblib')
        wait_for(lambda:ready(CANARY_PORT,'/health/ready'),label='canary ready',processes=[processes['canary']])
        for port in (STABLE_PORT,CANARY_PORT):
            if not ready(port,'/metrics'): raise RuntimeError(f'Metrics unavailable on {port}')
        launch('router',[sys.executable,HERE/'router.py'])
        wait_for(lambda:ready(8080,'/health/ready'),label='router ready',processes=[processes['router']])
        routing=[predict() for _ in range(100)]
        counts=Counter(row['track'] for row in routing)
        if counts!={'stable':90,'canary':10} or not all(row['valid'] for row in routing): raise RuntimeError('Initial 90/10 check failed')
        save(evidence/'initial-routing.json',{'counts':dict(counts),'requests':routing})
        launch('prometheus',[prom,'--config.file='+str(RUNTIME/'prometheus.yml'),'--storage.tsdb.path='+str(RUNTIME/'prometheus-data'),
                             '--web.listen-address=127.0.0.1:9090','--storage.tsdb.retention.time=2d'])
        launch_info={'executable':str(graf),'home':str(graf_home),'config':str(RUNTIME/'grafana.ini'),
                     'data':str(RUNTIME/'grafana-data'),'logs':str(RUNTIME/'grafana-logs')}
        save(evidence/'grafana-startup.json',launch_info)
        print('GRAFANA '+json.dumps(launch_info),flush=True)
        graf_args=[graf]+(['server'] if graf.name.lower()=='grafana.exe' else [])
        launch('grafana',graf_args+['--homepath',graf_home,'--config',RUNTIME/'grafana.ini'])
        watched=[processes[x] for x in ('prometheus','grafana')]
        wait_for(lambda:ready(9090,'/-/ready'),label='Prometheus ready',processes=watched)
        wait_for(lambda:api('http://127.0.0.1:3000/api/health').get('database')=='ok',label='Grafana healthy',processes=watched)
        wait_for(targets_ready,label='stable and canary scrape targets',processes=watched)
        wait_for(lambda:api('http://127.0.0.1:3000/api/dashboards/uid/local-canary')['dashboard']['uid']=='local-canary',label='provisioned Grafana dashboard',processes=watched)
        if not ready(3000,'/d/local-canary'): raise RuntimeError('Dashboard HTTP route failed')
        proxy_data=wait_for(grafana_data,label='live Grafana datasource data',processes=watched)
        save(evidence/'grafana-live-data.json',proxy_data)
        save(evidence/'readiness.json',{'prometheus':{'url':'http://127.0.0.1:9090/-/ready','http_status':200},
             'grafana':api('http://127.0.0.1:3000/api/health'),'dashboard':{'url':'http://127.0.0.1:3000/d/local-canary','http_status':200}})
        save(evidence/'targets-before.json',api('http://127.0.0.1:9090/api/v1/targets'))
        thread=threading.Thread(target=load,daemon=True); thread.start()
        controller=launch('evaluator',[sys.executable,HERE/'evaluator.py','--output',evidence/'events.jsonl'])
        print('RUNNING '+str(evidence),flush=True)
        wait_for(lambda:api('http://127.0.0.1:8080/__admin/state')['canary_weight']==100,240,
                 label='promotion to 100%',processes=list(processes.values()))
        save(evidence/'healthy-metrics.json',{name:query(expr) for name,expr in QUERIES.items()})
        print('PROMOTED 10 -> 25 -> 50 -> 100',flush=True)
        time.sleep(12)
        failed_at=time.time()
        killed=stop_record(state['processes']['canary'])
        save(evidence/'failure-injection.json',{'time':failed_at,'action':'stop verified canary process tree','pids':killed})
        wait_for(lambda:api('http://127.0.0.1:8080/__admin/state')['canary_weight']==0,45,label='automatic rollback to 0')
        controller.wait(timeout=15)
        if controller.returncode!=0: raise RuntimeError('Evaluator failed: '+tail(evidence/'evaluator.log'))
        wait_for(lambda:query(QUERIES['up'])==0,20,label='Prometheus canary up=0')
        def down_alert():
            result=api('http://127.0.0.1:9090/api/v1/alerts')
            return result if any(x['labels'].get('alertname')=='CanaryDown' and x['state']=='firing' for x in result['data']['alerts']) else False
        save(evidence/'alerts-during-failure.json',wait_for(down_alert,20,label='CanaryDown firing'))
        save(evidence/'targets-during-failure.json',api('http://127.0.0.1:9090/api/v1/targets'))
        after=[predict() for _ in range(100)]
        if any(row['track']!='stable' or not row['valid'] for row in after): raise RuntimeError('Rollback traffic isolation failed')
        save(evidence/'rollback-routing.json',after)
        model('canary',CANARY_PORT,'canary-v2.joblib')
        wait_for(lambda:ready(CANARY_PORT,'/health/ready'),label='restored canary ready',processes=[processes['canary']])
        wait_for(lambda:query(QUERIES['up'])==1,label='restored canary scrape up')
        if api('http://127.0.0.1:8080/__admin/state')['canary_weight']!=0: raise RuntimeError('Weight changed after restore')
        time.sleep(6)
        if load_errors: raise RuntimeError('Load generation failed: '+str(load_errors))
        ended=time.time()
        events=[json.loads(line) for line in (evidence/'events.jsonl').read_text().splitlines()]
        if [x['weight'] for x in events if x['action']=='PROMOTE']!=[25,50,100]: raise RuntimeError('Unexpected promotion history')
        rollback=[x for x in events if x['action']=='ROLLBACK' and x['weight']==0]
        if len(rollback)!=1 or rollback[0]['time']<failed_at: raise RuntimeError('Missing rollback after injected failure')
        queries={'weight':'canary_router_weight_percent','up':'up{job="model-service"}',
                 'requests':'sum by (track) (rate(model_service_http_requests_total{route="/api/v1/predict"}[30s]))',
                 'p95':'histogram_quantile(0.95,sum by (le,track)(rate(model_service_http_request_duration_seconds_bucket{route="/api/v1/predict"}[30s])))'}
        for name,expr in queries.items():
            save(evidence/('prometheus-'+name+'.json'),api('http://127.0.0.1:9090/api/v1/query_range?'+urlencode({'query':expr,'start':started,'end':ended,'step':2})))
        save(evidence/'grafana-dashboard.json',api('http://127.0.0.1:3000/api/dashboards/uid/local-canary'))
        save(evidence/'targets-final.json',api('http://127.0.0.1:9090/api/v1/targets'))
        save(evidence/'summary.json',{'status':'PASS','started':started,'ended':ended,'hashes':actual,'initial_routing':dict(counts),
             'promotion':[10,25,50,100],'rollback':0,'rollback_check':'100/100 valid stable predictions','restored_canary_up':query(QUERIES['up']),
             'final_weight':api('http://127.0.0.1:8080/__admin/state')['canary_weight'],
             'grafana_health':api('http://127.0.0.1:3000/api/health'),'grafana_datasource_live':True,
             'grafana_url':f'http://127.0.0.1:3000/d/local-canary?from={int(started*1000)}&to={int(ended*1000)}'})
        print('PASS: promotion 10/25/50/100; failure -> rollback 0; stable 100/100; canary up=1, weight=0.',flush=True)
        print('EVIDENCE '+str(evidence),flush=True)
    except BaseException as exc:
        load_stop.set()
        if thread: thread.join(timeout=10)
        save(evidence/'summary.json',{'status':'FAIL','error':str(exc),'started':started,'ended':time.time()})
        # Failure leaves diagnostic files, not half-started services.
        for name in reversed(list(state['processes'])):
            if name!='controller':
                try: stop_record(state['processes'][name])
                except Exception as cleanup_error: print(f'Cleanup {name}: {cleanup_error}',flush=True)
        raise
    finally:
        load_stop.set()
        if thread: thread.join(timeout=10)
        for stream in handles: stream.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=['run','stop'])
    args=parser.parse_args()
    run() if args.action=='run' else stop()
