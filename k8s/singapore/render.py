"""Render the existing serving and local Canary stack for Singapore; never deploy."""
import argparse
import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import yaml

ROOT = Path(__file__).resolve().parents[2]
NS = 'production'

def resource(kind, name, **fields):
    return dict(apiVersion='v1', kind=kind, metadata=dict(name=name, namespace=NS), **fields)

def config(name, data):
    digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    return resource('ConfigMap', name+'-'+digest, data=data, immutable=True)

def cmvol(cm, path, name='config'):
    return ({'name':name, 'configMap':{'name':cm['metadata']['name']}},
            {'name':name, 'mountPath':path, 'readOnly':True})

def deployment(name, image, port, *, command=None, env=None, cpu='25m', memory='64Mi', limit='192Mi', uid=10001):
    labels = {'app.kubernetes.io/name':name}
    container = dict(name=name, image=image, imagePullPolicy='IfNotPresent',
                     resources={'requests':{'cpu':cpu,'memory':memory},'limits':{'memory':limit}},
                     securityContext={'allowPrivilegeEscalation':False,'capabilities':{'drop':['ALL']}})
    if port:
        container['ports']=[{'name':'http','containerPort':port}]
    if command: container['command']=command
    if env: container['env']=[dict(name=k,value=v) for k,v in env.items()]
    pod = dict(automountServiceAccountToken=False, securityContext=dict(runAsNonRoot=True,runAsUser=uid,runAsGroup=uid,fsGroup=uid,seccompProfile={'type':'RuntimeDefault'}), containers=[container])
    return dict(apiVersion='apps/v1',kind='Deployment',metadata=dict(name=name,namespace=NS),spec=dict(replicas=1,selector={'matchLabels':labels},template={'metadata':{'labels':labels},'spec':pod}))

def service(name, port, selector=None, headless=False):
    spec=dict(selector=selector or {'app.kubernetes.io/name':name},ports=[dict(name='http',port=port,targetPort='http')])
    if headless: spec['clusterIP']='None'
    return resource('Service',name,spec=spec)

def mount(dep, cm, path, name='config'):
    volume, mnt=cmvol(cm,path,name)
    pod=dep['spec']['template']['spec']
    pod.setdefault('volumes',[]).append(volume)
    pod['containers'][0].setdefault('volumeMounts',[]).append(mnt)

def emptydir(dep,path,name,size):
    pod=dep['spec']['template']['spec']
    pod.setdefault('volumes',[]).append({'name':name,'emptyDir':{'sizeLimit':size}})
    pod['containers'][0].setdefault('volumeMounts',[]).append({'name':name,'mountPath':path})

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--images',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    images=json.loads(args.images.read_text(encoding='utf-8-sig'))
    for key in ('runtime','artifact','prometheus','grafana'):
        if not re.fullmatch(r'[^\s@]+@sha256:[0-9a-f]{64}',images[key]):
            raise ValueError('Exact digest required: '+key)
    for key in ('runtime','artifact'):
        if not images[key].startswith('596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/'):
            raise ValueError('Only the authorized Singapore ECR baseline is accepted')
    args.output.mkdir(parents=True,exist_ok=True)
    def save(name, items):
        (args.output/(name+'.json')).write_text(json.dumps(dict(apiVersion='v1',kind='List',items=items),indent=2)+'\n',encoding='utf-8')

    baseline=[]
    for filename in ('deployment.yaml','service.yaml','configmap.yaml'):
        obj=yaml.safe_load((ROOT/'k8s'/filename).read_text())
        obj['metadata']['namespace']=NS
        baseline.append(obj)
    model=baseline[0]
    pod=model['spec']['template']['spec']
    pod.pop('imagePullSecrets',None)
    pod['automountServiceAccountToken']=False
    pod['initContainers'][0]['image']=images['artifact']
    pod['containers'][0]['image']=images['runtime']
    model['spec']['template']['metadata']['labels']['track']='stable'
    save('baseline',baseline)
    hpa=yaml.safe_load((ROOT/'k8s/hpa.yaml').read_text())
    hpa['metadata']['namespace']=NS
    save('hpa',[hpa])

    # Preserve the trusted local canary artifact; no retraining or new runtime image.
    artifact=(ROOT/'canary/artifacts/canary-v2.joblib').read_bytes()
    artifact_hash=hashlib.sha256(artifact).hexdigest()
    model_cm=resource('ConfigMap','canary-artifact-'+artifact_hash[:12],binaryData={'model.joblib':base64.b64encode(artifact).decode()},immutable=True)
    canary=copy.deepcopy(model)
    canary['metadata']['name']='model-service-canary'
    canary['spec']['replicas']=1
    canary['spec']['selector']['matchLabels']={'app.kubernetes.io/name':'model-service-canary'}
    canary['spec']['template']['metadata']['labels']={'app.kubernetes.io/name':'model-service-canary','track':'canary'}
    cp=canary['spec']['template']['spec']
    cp.pop('initContainers')
    cp['volumes']=[{'name':'model-volume','configMap':{'name':model_cm['metadata']['name']}}]
    cp['containers'][0]['env']=[dict(name='MODEL_NAME',value='iris-canary-v2')]
    save('canary',[model_cm,canary,service('model-service-canary',8000,headless=True),service('model-service-stable',8000,{'app.kubernetes.io/name':'model-service'},True)])

    code=config('canary-code',{f:(ROOT/'canary'/f).read_text() for f in ('router.py','evaluator.py','controller.py')})
    router=deployment('canary-router',images['runtime'],8080,command=['python','/canary/router.py'],env={'CANARY_BIND_HOST':'0.0.0.0','CANARY_STABLE_URL':'http://model-service-stable:8000','CANARY_CANARY_URL':'http://model-service-canary:8000'},cpu='50m')
    router['spec']['strategy']={'type':'Recreate'}
    mount(router,code,'/canary')
    for probe in ('livenessProbe','readinessProbe'):
        router['spec']['template']['spec']['containers'][0][probe]={'httpGet':{'path':'/health/ready','port':'http'}}
    evaluator=deployment('canary-evaluator',images['runtime'],None,command=['python','/canary/controller.py'],env={'CANARY_PROMETHEUS_URL':'http://prometheus:9090','CANARY_ROUTER_URL':'http://canary-router:8080'},cpu='10m',memory='32Mi',limit='96Mi')
    evaluator['spec']['replicas']=0  # Arm only after targets and real traffic have passed.
    mount(evaluator,code,'/canary')
    save('router',[code,router,service('canary-router',8080)])
    save('evaluator',[evaluator])

    prom=yaml.safe_load((ROOT/'canary/prometheus.yml').read_text())
    prom['rule_files']=['/etc/prometheus/alerts.yml']
    prom['scrape_configs']=[
        {'job_name':'model-service','dns_sd_configs':[{'names':['model-service-stable.production.svc.cluster.local'],'type':'A','port':8000,'refresh_interval':'5s'},{'names':['model-service-canary.production.svc.cluster.local'],'type':'A','port':8000,'refresh_interval':'5s'}],
         'relabel_configs':[{'source_labels':['__meta_dns_name'],'regex':'model-service-(stable|canary)\\..*','target_label':'track','replacement':'$1'}]},
        {'job_name':'canary-router','static_configs':[{'targets':['canary-router:8080']}]}]
    pcm=config('prometheus-config',{'prometheus.yml':yaml.safe_dump(prom,sort_keys=False),'alerts.yml':(ROOT/'canary/alerts.yml').read_text()})
    pd=deployment('prometheus',images['prometheus'],9090,command=['/bin/prometheus','--config.file=/etc/prometheus/prometheus.yml','--storage.tsdb.path=/prometheus','--storage.tsdb.retention.time=24h','--storage.tsdb.retention.size=512MB'],cpu='100m',memory='192Mi',limit='384Mi',uid=65534)
    mount(pd,pcm,'/etc/prometheus')
    emptydir(pd,'/prometheus','data','1Gi')
    for probe in ('livenessProbe','readinessProbe'):
        pd['spec']['template']['spec']['containers'][0][probe]={'httpGet':{'path':'/-/ready','port':'http'},'initialDelaySeconds':5}
    save('prometheus',[pcm,pd,service('prometheus',9090)])

    datasource={'apiVersion':1,'datasources':[{'name':'Prometheus','uid':'canary-prom','type':'prometheus','access':'proxy','url':'http://prometheus:9090','isDefault':True,'editable':False,'jsonData':{'timeInterval':'5s'}}]}
    provider={'apiVersion':1,'providers':[{'name':'canary','orgId':1,'type':'file','disableDeletion':False,'updateIntervalSeconds':10,'options':{'path':'/dashboards'}}]}
    dscm=config('grafana-datasource',{'prometheus.yml':yaml.safe_dump(datasource)})
    prcm=config('grafana-provider',{'canary.yml':yaml.safe_dump(provider)})
    dashboard=config('grafana-dashboard',{'dashboard.json':(ROOT/'canary/dashboard.json').read_text()})
    gd=deployment('grafana',images['grafana'],3000,cpu='100m',memory='128Mi',limit='256Mi',uid=472,env={'GF_AUTH_ANONYMOUS_ENABLED':'true','GF_AUTH_ANONYMOUS_ORG_ROLE':'Viewer','GF_USERS_ALLOW_SIGN_UP':'false','GF_AUTH_DISABLE_LOGIN_FORM':'false','GF_ANALYTICS_REPORTING_ENABLED':'false','GF_ANALYTICS_CHECK_FOR_UPDATES':'false'})
    gc=gd['spec']['template']['spec']['containers'][0]
    gc['env'].append({'name':'GF_SECURITY_ADMIN_PASSWORD','valueFrom':{'secretKeyRef':{'name':'grafana-admin','key':'admin-password'}}})
    mount(gd,dscm,'/etc/grafana/provisioning/datasources','datasource')
    mount(gd,prcm,'/etc/grafana/provisioning/dashboards','provider')
    mount(gd,dashboard,'/dashboards','dashboard')
    emptydir(gd,'/var/lib/grafana','data','512Mi')
    gc['readinessProbe']={'httpGet':{'path':'/api/health','port':'http'},'initialDelaySeconds':10}
    save('grafana',[dscm,prcm,dashboard,gd,service('grafana',3000)])
    (args.output/'artifact-identity.json').write_text(json.dumps({'canary_sha256':artifact_hash,'source':'canary/artifacts/canary-v2.joblib'},indent=2))
    print('Rendered digest-pinned Singapore baseline, HPA and existing Canary stack.')

if __name__=='__main__': main()
