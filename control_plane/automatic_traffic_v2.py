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
    pod['automountServiceAccountToken']=True
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


def proposed_rbac(image):
    """Proposal only: least-privilege RBAC plus a fail-closed Job content boundary."""
    from scripts.render_control_plane_v2 import NAMES
    runtime_image(image)
    ns='production';controller=NAMES['controller']
    reader=dict(apiVersion='rbac.authorization.k8s.io/v1',kind='Role',metadata=dict(name=SA,namespace=ns),rules=[
        dict(apiGroups=[''],resources=['configmaps'],resourceNames=['model-release-state-v2'],verbs=['get']),
        dict(apiGroups=['apps'],resources=['deployments'],resourceNames=['credit-risk-stable-v2-predictor','credit-risk-canary-v2-predictor'],verbs=['get'])])
    creator=dict(apiVersion='rbac.authorization.k8s.io/v1',kind='Role',metadata=dict(name=SA+'-creator',namespace=ns),
        rules=[dict(apiGroups=['batch'],resources=['jobs'],verbs=['get','create'])])
    policy_name=SA+'-jobs'
    policy=dict(apiVersion='admissionregistration.k8s.io/v1',kind='ValidatingAdmissionPolicy',
        metadata=dict(name=policy_name),spec=dict(failurePolicy='Fail',matchConstraints=dict(resourceRules=[dict(
            apiGroups=['batch'],apiVersions=['v1'],operations=['CREATE'],resources=['jobs'],scope='Namespaced')]),
        matchConditions=[dict(name='exact-controller',expression=(
            "request.namespace == 'production' && request.userInfo.username == "
            "'system:serviceaccount:production:%s'" % controller))],validations=[
            dict(expression=("object.metadata.name.startsWith('v2-e2e-auto-') && "
                "object.metadata.labels['app.kubernetes.io/name'] == 'v2-e2e-runner' && "
                "object.metadata.annotations['ml-template.io/synthetic-policy-test'] == 'true' && "
                "object.metadata.annotations['ml-template.io/authorization-id'].matches('^sha256:[0-9a-f]{64}$')"),
                message='automatic traffic Job identity is immutable'),
            dict(expression=("object.spec.backoffLimit == 0 && object.spec.parallelism == 1 && "
                "object.spec.completions == 1 && object.spec.activeDeadlineSeconds == 930 && "
                "!has(object.spec.ttlSecondsAfterFinished)"),message='automatic traffic Job lifecycle is bounded'),
            dict(expression=("object.spec.template.spec.serviceAccountName == 'model-release-traffic-v2' && "
                "object.spec.template.spec.automountServiceAccountToken == true && "
                "object.spec.template.spec.restartPolicy == 'Never' && "
                "!has(object.spec.template.spec.hostNetwork) && !has(object.spec.template.spec.hostPID) && "
                "!has(object.spec.template.spec.hostIPC) && "
                "(!has(object.spec.template.spec.initContainers) || size(object.spec.template.spec.initContainers) == 0)"),
                message='automatic traffic Pod identity is constrained'),
            dict(expression=("size(object.spec.template.spec.containers) == 1 && "
                "object.spec.template.spec.containers[0].name == 'runner' && "
                "object.spec.template.spec.containers[0].image == '%s' && "
                "object.spec.template.spec.containers[0].command == ['python','-m','scripts.e2e_v2.runner'] && "
                "size(object.spec.template.spec.containers[0].args) == 4 && "
                "object.spec.template.spec.containers[0].args[0] == '--config-json' && "
                "object.spec.template.spec.containers[0].args[2] == '--payload' && "
                "object.spec.template.spec.containers[0].args[3] == '/app/scripts/e2e_v2/synthetic_credit_risk.json'" % image),
                message='automatic traffic executable and image are immutable'),
            dict(expression=("object.spec.template.spec.securityContext.runAsNonRoot == true && "
                "object.spec.template.spec.securityContext.runAsUser == 10001 && "
                "object.spec.template.spec.securityContext.runAsGroup == 10001 && "
                "object.spec.template.spec.securityContext.fsGroup == 10001 && "
                "object.spec.template.spec.securityContext.seccompProfile.type == 'RuntimeDefault' && "
                "object.spec.template.spec.containers[0].securityContext.allowPrivilegeEscalation == false && "
                "object.spec.template.spec.containers[0].securityContext.readOnlyRootFilesystem == true && "
                "object.spec.template.spec.containers[0].securityContext.capabilities.drop == ['ALL']"),
                message='automatic traffic security context is required'),
            dict(expression=("size(object.spec.template.spec.containers[0].env) == 1 && "
                "object.spec.template.spec.containers[0].env[0].name == 'AWS_EC2_METADATA_DISABLED' && "
                "object.spec.template.spec.containers[0].env[0].value == 'true' && "
                "(!has(object.spec.template.spec.containers[0].envFrom) || "
                "size(object.spec.template.spec.containers[0].envFrom) == 0) && "
                "(!has(object.spec.template.spec.imagePullSecrets) || size(object.spec.template.spec.imagePullSecrets) == 0) && "
                "dyn(object.spec.template.spec.containers[0].resources).requests['cpu'] == quantity('50m') && "
                "dyn(object.spec.template.spec.containers[0].resources).requests['memory'] == quantity('64Mi') && "
                "dyn(object.spec.template.spec.containers[0].resources).limits['cpu'] == quantity('500m') && "
                "dyn(object.spec.template.spec.containers[0].resources).limits['memory'] == quantity('256Mi')"),
                message='automatic traffic environment and resources are constrained'),
            dict(expression=("size(object.spec.template.spec.volumes) == 1 && "
                "object.spec.template.spec.volumes[0].name == 'evidence' && "
                "dyn(object.spec.template.spec.volumes[0].emptyDir).sizeLimit == quantity('32Mi') && "
                "size(object.spec.template.spec.containers[0].volumeMounts) == 1 && "
                "object.spec.template.spec.containers[0].volumeMounts[0].name == 'evidence' && "
                "object.spec.template.spec.containers[0].volumeMounts[0].mountPath == '/evidence'"),
                message='automatic traffic volumes are constrained')]))
    binding_policy=dict(apiVersion='admissionregistration.k8s.io/v1',kind='ValidatingAdmissionPolicyBinding',
        metadata=dict(name=policy_name),spec=dict(policyName=policy_name,validationActions=['Deny'],
            matchResources=dict(namespaceSelector=dict(matchLabels={'kubernetes.io/metadata.name':ns}))))
    objects=[dict(apiVersion='v1',kind='ServiceAccount',metadata=dict(name=SA,namespace=ns)),reader,creator,
             policy,binding_policy]
    for role,account in ((SA,SA),(SA+'-creator',controller)):
        objects.append(dict(apiVersion='rbac.authorization.k8s.io/v1',kind='RoleBinding',metadata=dict(name=role,namespace=ns),
            roleRef=dict(apiGroup='rbac.authorization.k8s.io',kind='Role',name=role),subjects=[dict(kind='ServiceAccount',name=account,namespace=ns)]))
    return dict(apiVersion='v1',kind='List',items=objects)
