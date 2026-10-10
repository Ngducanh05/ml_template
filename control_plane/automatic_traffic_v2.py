"""Opt-in demo traffic adapter; no state writes or rollout-policy decisions."""
import json
import os
from pathlib import Path
from .authorization_v2 import validate_authorization, ticket
from .state_v2 import runtime_image
from .state import Conflict
from .telemetry import log

SA = 'model-release-traffic-v2'
JOB_PATH = '/apis/batch/v1/namespaces/production/jobs'


def binding(active):
    auth=active['authorization']
    validate_authorization(auth,dict(pair=active['pair'],fingerprint=active['fingerprint'],
                                   event=auth['evidence']['candidate']['event']))
    if (active['previous_stable']!=auth['evidence']['stable']
            or active['previous_fingerprint']!=auth['evidence']['stable_fingerprint']):
        raise ValueError('Authorization baseline mismatch')
    return dict(pair=active['pair'],fingerprint=active['fingerprint'],authorization_id=auth['evidence_hash'],
                previous_stable=active['previous_stable'])


def render_job(active,image,approval):
    from scripts.e2e_v2.render import render
    runtime_image(image);ticket(approval)
    exact=binding(active)
    run_id='auto-'+exact['authorization_id'][7:39]
    config=dict(version=active['pair']['model_version'],mode='policy-test',run_id=run_id,namespace='production',
        rps=2,concurrency=1,requests=2000,duration=900,api_retries=0,
        allow_synthetic_gate_traffic=True,expected_release=exact,operator_approval=approval)
    payload=json.loads(Path(__file__).resolve().parents[1].joinpath('scripts/e2e_v2/synthetic_credit_risk.json').read_text())
    job=render(config,payload,image)['items'][-1]
    # Retain the Job as a durable once-per-authorization receipt, including failure/cancellation.
    # Do not delete it during RAMP. No TTL or retries that reset the request budget.
    job['spec'].pop('ttlSecondsAfterFinished')
    job['spec'].update(parallelism=1,completions=1)
    pod=job['spec']['template']['spec'];pod['serviceAccountName']=SA
    runner=pod['containers'][0]
    runner['command']=['python','-m','scripts.e2e_v2.runner']
    runner['args']=['--config-json',json.dumps(config,sort_keys=True),'--payload','/app/scripts/e2e_v2/synthetic_credit_risk.json']
    runner['volumeMounts']=[x for x in runner['volumeMounts'] if x['name']=='evidence']
    pod['volumes']=[x for x in pod['volumes'] if x['name']=='evidence']
    job['metadata']['annotations']['ml-template.io/authorization-id']=exact['authorization_id']
    job['metadata']['annotations']['ml-template.io/operator-approval']=approval
    return job


class AutomaticTraffic:
    def __init__(self,api,effects,*,enabled=False,image=None,approval=None):
        self.api,self.effects,self.enabled=api,effects,enabled
        self.image,self.approval=image,approval
        if enabled:runtime_image(image);ticket(approval)

    @classmethod
    def from_env(cls,api,effects):
        enabled=os.getenv('V2_AUTOMATIC_TRAFFIC','false')
        if enabled not in ('true','false'):raise ValueError('Invalid automatic traffic opt-in')
        return cls(api,effects,enabled=enabled=='true',image=os.getenv('V2_TRAFFIC_IMAGE'),
                   approval=os.getenv('V2_SYNTHETIC_GATE_APPROVAL'))

    def tick(self,state):
        if not self.enabled:return 'DISABLED'
        active=state.get('active')
        if not active or active.get('phase')!='RAMP' or state['route']['weight'] not in (10,25,50,100):return 'INERT'
        exact=binding(active)
        if state['stable']!=exact['previous_stable']:raise ValueError('Stable baseline mismatch')
        job=render_job(active,self.image,self.approval)
        path=JOB_PATH+'/'+job['metadata']['name']
        existing=self.api.request(path)
        if existing is None:
            if not self.effects.ready('canary',active['pair'],active['fingerprint']):return 'WAIT_READY'
            if not self.effects.ready('stable',active['previous_stable'],active['previous_fingerprint']):return 'WAIT_READY'
            try:self.api.request(JOB_PATH,'POST',job)
            except Conflict:pass
            existing=self.api.request(path)
        # Name collision, code/config drift or changed approval never starts a second run.
        if not existing or any(existing['metadata'].get('annotations',{}).get(k)!=v for k,v in job['metadata']['annotations'].items()):
            raise ValueError('Automatic traffic Job identity conflict')
        log('AUTOMATIC_TRAFFIC_JOB',job=job['metadata']['name'],authorization_id=exact['authorization_id'],
            synthetic_can_affect_quality_gate=True)
        return 'EXISTS'


def proposed_rbac():
    """Proposal only. Job create cannot be restricted by resourceNames in Kubernetes."""
    from scripts.render_control_plane_v2 import NAMES
    ns='production';controller=NAMES['controller']
    reader=dict(apiVersion='rbac.authorization.k8s.io/v1',kind='Role',metadata=dict(name=SA,namespace=ns),rules=[
        dict(apiGroups=[''],resources=['configmaps'],resourceNames=['model-release-state-v2'],verbs=['get']),
        dict(apiGroups=['apps'],resources=['deployments'],resourceNames=['credit-risk-stable-v2-predictor','credit-risk-canary-v2-predictor'],verbs=['get'])])
    creator=dict(apiVersion='rbac.authorization.k8s.io/v1',kind='Role',metadata=dict(name=SA+'-creator',namespace=ns),
        rules=[dict(apiGroups=['batch'],resources=['jobs'],verbs=['get','create'])])
    objects=[dict(apiVersion='v1',kind='ServiceAccount',metadata=dict(name=SA,namespace=ns)),reader,creator]
    for role,account in ((SA,SA),(SA+'-creator',controller)):
        objects.append(dict(apiVersion='rbac.authorization.k8s.io/v1',kind='RoleBinding',metadata=dict(name=role,namespace=ns),
            roleRef=dict(apiGroup='rbac.authorization.k8s.io',kind='Role',name=role),subjects=[dict(kind='ServiceAccount',name=account,namespace=ns)]))
    return dict(apiVersion='v1',kind='List',items=objects)
