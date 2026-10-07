"""Add credit-risk scrapes to an exported live ConfigMap; preserve old jobs/rules."""
import argparse,hashlib,json
from pathlib import Path

def extend(original):
    data=dict(original['data'])
    text=data['prometheus.yml']
    for job,port in [('credit-risk-router',8080),('model-image-watcher',9091),('model-release-controller',9092)]:
        if f'job_name: {job}' in text: continue
        text+=f'\n- job_name: {job}\n  scrape_interval: 5s\n  static_configs:\n  - targets: ["{job}:{port}"]\n'
    data['prometheus.yml']=text
    name='prometheus-config-'+hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()[:12]
    cm=dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name=name,namespace='production'),immutable=True,data=data)
    patch=[{'op':'test','path':'/spec/template/spec/volumes/0/name','value':'config'},
           {'op':'test','path':'/spec/template/spec/volumes/0/configMap/name','value':original['metadata']['name']},
           {'op':'replace','path':'/spec/template/spec/volumes/0/configMap/name','value':name}]
    return cm,patch

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--existing',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    cm,patch=extend(json.loads(a.existing.read_text(encoding='utf-8-sig')))
    a.output.mkdir(parents=True,exist_ok=True)
    (a.output/'prometheus-credit-risk.json').write_text(json.dumps(cm,indent=2))
    (a.output/'prometheus-patch.json').write_text(json.dumps(patch,indent=2))
    for key,value in cm['data'].items(): (a.output/key).write_text(value)
