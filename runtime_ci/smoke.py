"""Run inside a disposable, network-disabled candidate container; never in EKS."""
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import Request, urlopen

assert os.getuid() == os.getgid() == 10001
request = json.loads(Path('/opt/runtime/technical-contract.json').read_text())
for pin in request['requirements']:
    name, version = pin.split('==')
    assert importlib.metadata.version(name) == version
assert importlib.metadata.version('mlserver') == request['runtime_profile']['mlserver_version']
assert importlib.metadata.version('mlserver-mlflow') == request['runtime_profile']['mlserver_mlflow_version']
for root in ('/opt/runtime', '/mnt/models'):
    assert not any(x.name == 'MLmodel' or x.suffix in ('.skops','.pkl','.pickle','.joblib') for x in Path(root).rglob('*'))
# This synthetic artifact is generated ONLY in tmpfs after the image is built.
import mlflow.sklearn
import pandas as pd
from sklearn.dummy import DummyClassifier
x = pd.DataFrame({'feature': [0.0, 1.0, 2.0, 3.0]})
model = DummyClassifier(strategy='most_frequent').fit(x, [0, 0, 0, 1])
uri = '/tmp/test-artifact'
mlflow.sklearn.save_model(model, uri, serialization_format='skops', input_example=x.iloc[:1])
settings = Path('/tmp/serving'); settings.mkdir()
(settings/'model-settings.json').write_text(json.dumps({'name':'ci-contract-smoke','implementation':'mlserver_mlflow.MLflowRuntime','parameters':{'uri':uri},'parallel_workers':0}))
log = Path('/tmp/mlserver.log').open('w')
process = subprocess.Popen(['mlserver','start',str(settings)],stdout=log,stderr=subprocess.STDOUT)
try:
    deadline = time.monotonic()+60
    while True:
        try:
            with urlopen('http://127.0.0.1:8080/v2/models/ci-contract-smoke/ready',timeout=2) as r:
                if r.status == 200: break
        except OSError:
            if process.poll() is not None or time.monotonic()>deadline:
                raise RuntimeError(Path('/tmp/mlserver.log').read_text()[-8000:])
            time.sleep(.5)
    body={'inputs':[{'name':'feature','datatype':'FP64','shape':[1,1],'data':[1.0]}]}
    req=Request('http://127.0.0.1:8080/v2/models/ci-contract-smoke/infer',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
    with urlopen(req,timeout=5) as r:
        result=json.load(r);assert r.status==200 and result.get('outputs')
    print(json.dumps({'status':'PASS','scope':'isolated synthetic MLServer V2; not live release','nonroot':True,'read_only':True}))
finally:
    process.terminate()
    try: process.wait(timeout=10)
    except subprocess.TimeoutExpired: process.kill(); process.wait()
