"""Render production control plane. Initial state is a CREATE-ONLY artifact."""
import argparse
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from control_plane.state import initial_state
from control_plane.policy import DEFAULTS,Policy

NS='production'
def res(kind,name,**fields): return dict(apiVersion='v1',kind=kind,metadata={'name':name,'namespace':NS},**fields)

def build(image,policy=None):
    if policy and set(policy)-set(DEFAULTS): raise ValueError('Unknown release policy key')
    policy={**DEFAULTS,**{k:str(v) for k,v in (policy or {}).items()}}
    Policy.from_env(policy)
    if not re.fullmatch(r'596432515999\.dkr\.ecr\.ap-southeast-1\.amazonaws\.com/model-service@sha256:[0-9a-f]{64}',image):
        raise ValueError('Pin the separate control-plane image by Singapore ECR digest')
    rbac=[];workloads=[]
    for name,role,port in [('credit-risk-router','router',8080),('model-image-watcher','watcher',9091),('model-release-controller','controller',9092)]:
        rbac.append(res('ServiceAccount',name,automountServiceAccountToken=False))
        verbs=['get'] if role=='router' else ['get','update']
        rules=[dict(apiGroups=[''],resources=['configmaps'],resourceNames=['model-release-state'],verbs=verbs)]
        if role=='controller': rules.append(dict(apiGroups=['apps'],resources=['deployments'],resourceNames=['credit-risk-classifier-stable','credit-risk-classifier-canary'],verbs=['get','patch']))
        rbac.append(dict(apiVersion='rbac.authorization.k8s.io/v1',kind='Role',metadata={'name':name,'namespace':NS},rules=rules))
        rbac.append(dict(apiVersion='rbac.authorization.k8s.io/v1',kind='RoleBinding',metadata={'name':name,'namespace':NS},subjects=[dict(kind='ServiceAccount',name=name,namespace=NS)],roleRef=dict(apiGroup='rbac.authorization.k8s.io',kind='Role',name=name)))
        labels={'app.kubernetes.io/name':name,'app.kubernetes.io/part-of':'credit-risk-serving'}
        # Controller is armed explicitly only after queue/RBAC/monitoring and
        # capacity checks. A normal re-render must not silently start rollouts.
        replicas=0 if role=='controller' else 1
        container=dict(name=role,image=image,imagePullPolicy='IfNotPresent',command=['python','-m','control_plane.'+role],
            ports=[dict(name='http',containerPort=port)],
            env=[dict(name='POD_NAMESPACE',value=NS),dict(name='AWS_REGION',value='ap-southeast-1'),dict(name='AWS_EC2_METADATA_DISABLED',value='true')]+[dict(name=k,value=v) for k,v in policy.items()],
            resources={'requests':{'cpu':'50m','memory':'64Mi'},'limits':{'memory':'192Mi'}},
            securityContext={'allowPrivilegeEscalation':False,'readOnlyRootFilesystem':True,'capabilities':{'drop':['ALL']}},
            livenessProbe={'httpGet':{'path':'/health/live','port':'http'},'periodSeconds':15,'initialDelaySeconds':10},
            readinessProbe={'httpGet':{'path':'/health/ready','port':'http'},'periodSeconds':5,'initialDelaySeconds':5},
            volumeMounts=[{'name':'tmp','mountPath':'/tmp'}])
        pod=dict(serviceAccountName=name,automountServiceAccountToken=True,securityContext=dict(runAsNonRoot=True,runAsUser=10001,runAsGroup=10001,seccompProfile={'type':'RuntimeDefault'}),containers=[container],volumes=[{'name':'tmp','emptyDir':{'medium':'Memory','sizeLimit':'16Mi'}}],terminationGracePeriodSeconds=35)
        workloads.append(dict(apiVersion='apps/v1',kind='Deployment',metadata={'name':name,'namespace':NS},spec=dict(replicas=replicas,revisionHistoryLimit=3,strategy={'type':'Recreate'} if role in ('watcher','controller') else {'type':'RollingUpdate','rollingUpdate':{'maxSurge':1,'maxUnavailable':0}},selector={'matchLabels':labels},template={'metadata':{'labels':labels},'spec':pod})))
        workloads.append(res('Service',name,spec=dict(type='ClusterIP',selector=labels,ports=[dict(name='http',port=port,targetPort='http')])))
    state=res('ConfigMap','model-release-state',data={'state.json':json.dumps(initial_state(),separators=(',',':'))})
    return rbac,workloads,state

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--image',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--policy-file',type=Path);args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    rbac,workloads,state=build(args.image,json.loads(args.policy_file.read_text()) if args.policy_file else None)
    for name,items in [('rbac',rbac),('workloads',workloads)]:
        (args.output/(name+'.json')).write_text(json.dumps(dict(apiVersion='v1',kind='List',items=items),indent=2))
    (args.output/'state-create-only.json').write_text(json.dumps(state,indent=2))
