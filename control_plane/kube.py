"""Small in-cluster API client; projected token is reread on every request."""
import json
import os
from pathlib import Path
import ssl
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from .state import Conflict,IMAGE_REPO,DIGEST

class Kube:
    def __init__(self):
        self.base='https://'+os.environ['KUBERNETES_SERVICE_HOST']+':'+os.environ.get('KUBERNETES_SERVICE_PORT','443')
        self.ns=os.environ.get('POD_NAMESPACE','production')
        self.name='model-release-state'
        self.token=Path('/var/run/secrets/kubernetes.io/serviceaccount/token')
        self.context=ssl.create_default_context(cafile='/var/run/secrets/kubernetes.io/serviceaccount/ca.crt')
    def request(self,path,method='GET',data=None):
        headers={'Authorization':'Bearer '+self.token.read_text().strip(),'Content-Type':'application/merge-patch+json' if method=='PATCH' else 'application/json'}
        req=Request(self.base+path,method=method,headers=headers,data=None if data is None else json.dumps(data).encode())
        try:
            with urlopen(req,context=self.context,timeout=8) as response: return json.load(response)
        except HTTPError as exc:
            if exc.code==409: raise Conflict() from None
            # Never emit request headers/tokens or uncontrolled response payload.
            raise RuntimeError(f'Kubernetes API {method} failed: HTTP {exc.code}') from None
    def read_state(self):
        cm=self.request(f'/api/v1/namespaces/{self.ns}/configmaps/{self.name}')
        return cm['metadata']['resourceVersion'],json.loads(cm['data']['state.json'])
    def write_state(self,revision,state):
        self.request(f'/api/v1/namespaces/{self.ns}/configmaps/{self.name}',method='PUT',data={'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':self.name,'namespace':self.ns,'resourceVersion':revision},'data':{'state.json':json.dumps(state,separators=(',',':'))}})
    def deployment(self,track):
        if track not in ('stable','canary'): raise ValueError('Unknown track')
        return self.request(f'/apis/apps/v1/namespaces/{self.ns}/deployments/credit-risk-classifier-{track}')
    def ensure(self,track,digest,replicas):
        if not DIGEST.fullmatch(digest): raise ValueError('Immutable digest required')
        obj=self.deployment(track); image=IMAGE_REPO+'@'+digest
        if obj['spec']['template']['spec']['containers'][0]['image']==image and obj['spec']['replicas']==replicas: return obj
        containers=obj['spec']['template']['spec']['containers']
        containers[0]['image']=image
        self.request(f'/apis/apps/v1/namespaces/{self.ns}/deployments/credit-risk-classifier-{track}',method='PATCH',data={'metadata':{'resourceVersion':obj['metadata']['resourceVersion']},'spec':{'replicas':replicas,'template':{'spec':{'containers':containers}}}})
        return None
    def ready(self,track,digest):
        obj=self.deployment(track); status=obj.get('status',{})
        return (obj['spec']['template']['spec']['containers'][0]['image']==IMAGE_REPO+'@'+digest
                and status.get('observedGeneration',0)>=obj['metadata']['generation']
                and status.get('updatedReplicas',0)==obj['spec']['replicas']==1
                and status.get('readyReplicas',0)>=1 and status.get('availableReplicas',0)>=1
                and status.get('replicas',0)==1)
