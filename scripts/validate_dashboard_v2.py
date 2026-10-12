"""Read-only validation of rendered dashboard queries against existing Prometheus."""
import argparse
import json
import re
from pathlib import Path
from scripts.bootstrap_v2 import KubectlV2


def concrete(query):
    for key,value in {'${node_hourly_usd:raw}':'0.0528','${service:regex}':'.*','${model:regex}':'credit-risk','${model}':'credit-risk','$namespace':'production','$track':'stable|canary','$node':'.*','$__rate_interval':'1m'}.items():
        query=query.replace(key,value)
    if '$' in query: raise ValueError('Unresolved dashboard variable')
    return query


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    dashboard=json.loads(Path('k8s/monitoring/grafana/dashboards/mlops_v2_infra.json').read_text(encoding='utf-8'))
    queries=[{'panel':p['title'],'ref':q['refId'],'expr':concrete(q['expr'])} for p in dashboard['panels'] for q in p['targets']]
    queries += [{'panel':'Annotation: '+x['name'],'ref':'annotation','expr':concrete(x['expr'])} for x in dashboard['annotations']['list']]
    metrics=sorted(set(re.findall(r'\b(?:kube_|container_|credit_risk_|mlops_v2_)[a-zA-Z0-9_]+',json.dumps(dashboard))) | {'up'})
    code="""import json,sys,datetime
from urllib.request import urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError
request=json.load(sys.stdin);out=[]
for row in request['queries']:
 try:
  with urlopen('http://prometheus.production.svc:9090/api/v1/query?'+urlencode({'query':row['expr']}),timeout=10) as response: data=json.load(response)
 except HTTPError as exc: data=json.load(exc)
 except Exception as exc: data={'status':'error','errorType':type(exc).__name__}
 out.append(dict(row,response=data))
with urlopen('http://prometheus.production.svc:9090/api/v1/label/__name__/values',timeout=10) as response: names=set(json.load(response)['data'])
print(json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(),'queries':out,'metrics':{name:name in names for name in request['metrics']}}))
"""
    api=KubectlV2(); result=json.loads(api.command('exec','-i','deployment/credit-risk-router-v2','--','python','-c',code,payload=json.dumps({'queries':queries,'metrics':metrics})))
    args.output.mkdir(parents=True,exist_ok=True)
    (args.output/'dashboard-v2-metric-presence.json').write_text(json.dumps({'timestamp':result['timestamp'],'metrics':result['metrics']},indent=2))
    (args.output/'promql-results.json').write_text(json.dumps(result,indent=2))
    lines=[];errors=[]
    for row in result['queries']:
        data=row['response'];status='ERROR' if data['status']!='success' else 'DATA' if data['data']['result'] else 'EMPTY'
        lines.append(status+' | '+row['panel']+' | '+row['ref']+' | '+row['expr'])
        if status=='ERROR':errors.append(row)
    (args.output/'dashboard-v2-promql.txt').write_text('\n'.join(lines)+'\n')
    print(json.dumps({'queries':len(lines),'errors':len(errors),'present_metrics':sum(result['metrics'].values()),'required_metrics':len(result['metrics'])}))
    for error in errors:print(json.dumps(error))
    raise SystemExit(1 if errors else 0)

if __name__=='__main__':main()
