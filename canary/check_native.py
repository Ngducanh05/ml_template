"""Real HTTP smoke test; manual routing checks, NOT Prometheus lifecycle evidence."""
from collections import Counter
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import router

os.environ['CANARY_STABLE_PORT']='18081'
os.environ['CANARY_CANARY_PORT']='18082'

HERE=Path(__file__).resolve().parent
ROOT=HERE.parent
OUT=HERE/'output'/('canary-native-'+str(time.time_ns()))
OUT.mkdir(parents=True,exist_ok=True)
processes=[]
logs=[]
server=None

def get(url,data=None):
    request=Request(url,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    try:
        with urlopen(request,timeout=4) as response:
            return response.status,response.headers,response.read()
    except HTTPError as exc: return exc.code,exc.headers,exc.read()

def predict():
    payload={'instances':[{'sepal_length':5.1,'sepal_width':3.5,'petal_length':1.4,'petal_width':.2}]}
    status,headers,body=get('http://127.0.0.1:8080/api/v1/predict',payload)
    return {'status':status,'track':headers.get('X-Lab-Route'),'body':json.loads(body)}

try:
    for port in (8080,18081,18082):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0: raise RuntimeError(f'Port {port} already occupied')
    for track,port,name in [('stable',18081,'stable.joblib'),('canary',18082,'canary-v2.joblib')]:
        log=(OUT/(track+'.log')).open('w',encoding='utf-8'); logs.append(log)
        env=dict(os.environ,PYTHONPATH=str(ROOT/'src'),PYTHONDONTWRITEBYTECODE='1',MODEL_PATH=str(ROOT/'canary'/'artifacts'/name),MODEL_NAME='iris-'+track)
        proc=subprocess.Popen([sys.executable,'-m','uvicorn','model_service.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],
                              env=env,cwd=HERE,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(proc)
        deadline=time.monotonic()+45
        while True:
            if proc.poll() is not None: raise RuntimeError(track+' failed to start; inspect log')
            try:
                if get(f'http://127.0.0.1:{port}/health/ready')[0]==200: break
            except OSError: pass
            if time.monotonic()>deadline: raise RuntimeError(track+' readiness timeout')
            time.sleep(.25)
        assert get(f'http://127.0.0.1:{port}/metrics')[0]==200
    server=router.ThreadingHTTPServer(('127.0.0.1',8080),router.Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    initial=[predict() for _ in range(100)]
    counts=Counter(row['track'] for row in initial)
    assert counts=={'stable':90,'canary':10},counts
    assert all(row['status']==200 and row['body']=={'predictions':['setosa']} for row in initial)
    # Deliberately set 100% for a deterministic upstream-failure test.
    assert get('http://127.0.0.1:8080/__admin/weight',{'canary_weight':100})[0]==200
    processes[1].terminate(); processes[1].wait(timeout=10)
    failure=predict()
    assert failure['status']==502 and failure['track']=='canary',failure
    assert get('http://127.0.0.1:8080/__admin/weight',{'canary_weight':0})[0]==200
    after=[predict() for _ in range(100)]
    assert all(row['status']==200 and row['track']=='stable' for row in after)
    result={'status':'PASS','scope':'Real HTTP services + router; manual weight updates, not automatic Prometheus promotion/rollback',
            'initial_counts':dict(counts),'initial':initial,'canary_failure':failure,'after_weight_zero':after}
    (OUT/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('REAL_HTTP PASS: 90 stable / 10 canary; stopped canary returns 502; manual weight 0 gives 100 stable / 0 canary',flush=True)
finally:
    if server:
        server.shutdown(); server.server_close()
    for proc in processes:
        if proc.poll() is None:
            proc.terminate(); proc.wait(timeout=10)
    for log in logs: log.close()
