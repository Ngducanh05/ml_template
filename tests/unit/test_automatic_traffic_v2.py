"""Offline-only automatic traffic lifecycle and actual loopback Router counters."""
import copy
import json
import threading
from unittest.mock import Mock
import pytest
from control_plane.automatic_traffic_v2 import AutomaticTraffic,render_job,binding,proposed_rbac
from control_plane.state import Conflict
from test_controller_v2 import setup,until,OLD
from test_e2e_runner_v2 import Clock,payload,metadata
from scripts.e2e_v2.runner import execute,Evidence,Live
from scripts.e2e_v2.runner import validate_automatic_policy

IMAGE='596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:'+'e'*64


class Jobs:
    def __init__(self):self.items={};self.creates=0
    def request(self,path,method='GET',obj=None):
        if method=='POST':
            self.creates+=1;name=path+'/'+obj['metadata']['name']
            if name in self.items:raise Conflict()
            self.items[name]=copy.deepcopy(obj);return obj
        return self.items.get(path)


def ramp():
    _,s,e,clock,c=setup();until(c,s,'RAMP')
    return s,e,clock,c


def config(state):
    job=render_job(state['active'],IMAGE,'DEMO-APPROVAL-1')
    return json.loads(job['spec']['template']['spec']['containers'][0]['args'][1])


def test_default_disabled_and_unapproved_or_security_block_are_inert():
    api=Jobs();effects=Mock();s,_,_,_=ramp()
    assert AutomaticTraffic(api,effects).tick(s.read())=='DISABLED'
    _,s,_,_,c=setup(False)
    auto=AutomaticTraffic(api,effects,enabled=True,image=IMAGE,approval='TEST-1')
    for status in ('READY_FOR_RELEASE','BUILD_REQUIRED'):
        s.update(lambda state:state['pending'][0].update(status=status));c.tick()
        assert auto.tick(s.read())=='INERT'
    assert api.creates==0;effects.ready.assert_not_called()


def test_authorized_ready_ramp_creates_exactly_one_durable_job_across_restart():
    s,e,_,_=ramp();api=Jobs()
    for _ in range(4):AutomaticTraffic(api,e,enabled=True,image=IMAGE,approval='TEST-1').tick(s.read())
    assert api.creates==1
    job=next(iter(api.items.values()));job['status']={'failed':1}
    AutomaticTraffic(api,e,enabled=True,image=IMAGE,approval='TEST-1').tick(s.read())
    assert api.creates==1 and 'ttlSecondsAfterFinished' not in job['spec']
    assert job['spec']['backoffLimit']==0 and job['spec']['parallelism']==1
    assert job['spec']['activeDeadlineSeconds']==930
    assert job['spec']['template']['spec']['serviceAccountName']=='model-release-traffic-v2'
    assert job['spec']['template']['spec']['automountServiceAccountToken'] is True
    assert binding(s.read()['active'])==config(s.read())['expected_release']


def test_create_conflict_reuses_winner_without_second_job():
    s,e,_,_=ramp();api=Jobs();original=api.request
    def conflict(path,method='GET',obj=None):
        result=original(path,method,obj)
        if method=='POST':raise Conflict()
        return result
    api.request=conflict
    assert AutomaticTraffic(api,e,enabled=True,image=IMAGE,approval='TEST-1').tick(s.read())=='EXISTS'
    assert api.creates==1


def test_identity_collision_and_readiness_fail_closed():
    s,e,_,_=ramp();api=Jobs();auto=AutomaticTraffic(api,e,enabled=True,image=IMAGE,approval='TEST-1')
    e.ready=Mock(return_value=False);assert auto.tick(s.read())=='WAIT_READY';assert api.creates==0
    e.ready.return_value=True;auto.tick(s.read())
    next(iter(api.items.values()))['metadata']['annotations']['ml-template.io/authorization-id']='wrong'
    with pytest.raises(ValueError):auto.tick(s.read())
    assert api.creates==1


@pytest.mark.parametrize('case',['pair','authorization','fingerprint','BLOCKED','IDLE'])
def test_runner_stops_on_identity_or_phase_change_without_sending(tmp_path,case):
    s,_,_,_=ramp();state=s.read();cfg=config(state);changed=copy.deepcopy(state)
    if case=='pair':changed['active']['pair']['storage_uri']+='wrong'
    elif case=='authorization':changed['active']['authorization']['evidence_hash']='sha256:'+'f'*64
    elif case=='fingerprint':changed['active']['fingerprint']='sha256:'+'f'*64
    elif case=='IDLE':changed['active']=None
    else:changed['active']['phase']=case
    client=Mock();client.state.side_effect=[state,changed];client.ready.return_value=True
    clock=Clock();result=execute(cfg,payload(),client,Evidence(tmp_path),clock,clock.sleep)
    assert result['outcome']=='BLOCKED';client.send.assert_not_called()


@pytest.mark.parametrize('case',['payload','ready','cancel','timeout'])
def test_runner_bounds_and_contract_fail_closed(tmp_path,case):
    s,_,_,_=ramp();state=s.read();cfg=config(state);cfg['duration']=1
    client=Mock();client.state.return_value=state;client.ready.return_value=True
    client.verify_pair.return_value=case!='ready';client.metadata.return_value=metadata()
    client.send.return_value=dict(status=200,track='canary',latency_ms=1)
    body=payload()
    if case=='payload':body['inputs'][0]['name']='incompatible'
    clock=Clock();result=execute(cfg,body,client,Evidence(tmp_path),clock,clock.sleep,cancelled=lambda:case=='cancel')
    assert result['outcome']=='BLOCKED' and result['requests']<=2
    if case!='timeout':client.send.assert_not_called()


def test_proposed_rbac_is_namespaced_and_generator_is_admission_constrained():
    objects=proposed_rbac(IMAGE)['items'];roles=[o for o in objects if o['kind']=='Role']
    assert len(roles)==2
    assert all(o['metadata']['namespace']=='production' for o in objects if o['kind'] in ('ServiceAccount','Role','RoleBinding'))
    reader=next(o for o in roles if o['metadata']['name']=='model-release-traffic-v2')
    assert all(r['verbs']==['get'] and r['resourceNames'] for r in reader['rules'])
    creator=next(o for o in roles if o is not reader)
    assert creator['rules']==[dict(apiGroups=['batch'],resources=['jobs'],verbs=['get','create'])]
    policy=next(o for o in objects if o['kind']=='ValidatingAdmissionPolicy')
    binding_policy=next(o for o in objects if o['kind']=='ValidatingAdmissionPolicyBinding')
    assert policy['spec']['failurePolicy']=='Fail'
    assert policy['spec']['matchConditions'][0]['expression'].endswith("system:serviceaccount:production:model-release-controller-v2'")
    expressions='\n'.join(v['expression'] for v in policy['spec']['validations'])
    for required in (IMAGE,"model-release-traffic-v2","automountServiceAccountToken == true",
                     "readOnlyRootFilesystem == true","capabilities.drop == ['ALL']",
                     "dyn(object.spec.template.spec.volumes[0].emptyDir).sizeLimit == quantity('32Mi')",
                     "envFrom","imagePullSecrets",
                     "dyn(object.spec.template.spec.containers[0].resources).limits['memory'] == quantity('256Mi')"):
        assert required in expressions
    assert binding_policy['spec']['validationActions']==['Deny']
    assert binding_policy['spec']['matchResources']['namespaceSelector']['matchLabels']=={
        'kubernetes.io/metadata.name':'production'}


@pytest.mark.parametrize('field,value',[('rps',3),('concurrency',2),('requests',1999),('duration',899),
                                           ('api_retries',1),('namespace','other'),('mode','baseline')])
def test_automatic_config_budget_is_immutable(field,value):
    s,_,_,_=ramp();cfg=config(s.read());cfg[field]=value
    with pytest.raises(ValueError,match='automatic_traffic_policy_mismatch'):
        validate_automatic_policy(cfg)


def test_automatic_config_rejects_unreviewed_fields():
    s,_,_,_=ramp();cfg=config(s.read());cfg['extra']='not-reviewed'
    with pytest.raises(ValueError,match='automatic_traffic_policy_mismatch'):
        validate_automatic_policy(cfg)


@pytest.mark.parametrize('http_status',[200,500])
def test_real_loopback_router_traffic_drives_unchanged_stage_policy(tmp_path,http_status):
    from control_plane.router import Routing,BoundedServer,requests,latency
    from control_plane.router_v2 import handler
    from control_plane.controller_v2 import Controller
    _,s,e,clock,c=setup();until(c,s,'RAMP')
    routing=Routing(clock=lambda:clock[0]);routing.set(s.read())
    headers=[]
    parent=handler(routing,lambda:{'stable':'http://test','canary':'http://test'},
        lambda *_:(http_status,b'{"outputs":[{"data":[0]}]}','application/json'))
    class HTTP(parent):
        def do_POST(self):
            headers.append((self.headers.get('X-Traffic-Origin'),self.headers.get('X-E2E-Run')))
            super().do_POST()
    server=BoundedServer(('127.0.0.1',0),HTTP);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=object.__new__(Live);client.router='http://127.0.0.1:'+str(server.server_port)
    client.state=s.read;client.ready=lambda:True;client.verify_pair=lambda *a,**kw:True;client.metadata=lambda *_:metadata()
    good_start=requests.labels('canary','200')._value.get()
    bad_start=requests.labels('canary','500')._value.get()
    start=good_start+bad_start
    base_count=latency.labels('canary','200')._buckets[0].get()
    def metrics():
        good=requests.labels('canary','200')._value.get()-good_start
        bad=requests.labels('canary','500')._value.get()-bad_start
        count=good+bad
        # Actual measured router latency, using its histogram's finite upper bound.
        samples=latency.labels('canary',str(http_status)).collect()[0].samples
        buckets=[x for x in samples if x.name.endswith('_bucket') and x.labels['le']!='+Inf']
        total=requests.labels('canary',str(http_status))._value.get()
        p95=next(float(x.labels['le']) for x in buckets if x.value>=total*.95) if count else float('nan')
        return dict(up=1,age=0,traffic=count,success=good,errors=bad/count if count else float('nan'),p95=p95,stable_errors=0)
    e.metrics=metrics
    cfg=config(s.read());cfg.update(requests=100,duration=30,rps=20)
    fake=Clock();weights=[]
    def step(amount):
        fake.sleep(amount)
        if fake()>=getattr(step,'next',0):
            step.next=fake()+0.5;c.tick();routing.set(s.read());weights.append(s.read()['route']['weight'])
    try:
        # No real requests yet: minimum observations prevents promotion.
        c.tick();assert s.read()['active']['phase']=='RAMP' and s.read()['route']['weight']==10
        result=execute(cfg,payload(),client,Evidence(tmp_path),fake,step)
        for _ in range(8):c.tick();routing.set(s.read())
        assert requests.labels('canary','200')._value.get()+requests.labels('canary','500')._value.get()>start
        if http_status==200:
            assert latency.labels('canary','200')._buckets[0].get()>base_count
            assert {10,25,50,100}<=set(weights)
            assert s.read()['stable']['model_version']=='3' and s.read()['active'] is None
            assert result['outcome']=='PASS' and result['stages']==[10,25,50,100]
        else:
            assert s.read()['stable']==OLD and s.read()['active'] is None
            assert 'canary_errors' in s.read()['failed'][-1]['reason'].split(',')
            assert result['outcome']=='FAIL' and result['reason']==s.read()['failed'][-1]['reason']
        assert headers and all(origin=='synthetic' and run==cfg['run_id'] for origin,run in headers)
        assert result['requests']<=100
    finally:server.shutdown();server.server_close();thread.join(2)


def lifecycle(rollback=False):
    s,e,_,c=ramp(); states=[s.read()]
    if rollback:
        c.rollback('canary_errors');states.append(s.read());c.tick();states.append(s.read())
    else:
        until(c,s,'FINALIZING');states.append(s.read());c.tick();states.append(s.read());c.tick();states.append(s.read())
    assert states[-1]['active'] is None
    return states


@pytest.mark.parametrize('start,rollback',[(0,False),(1,False),(2,False),(3,False),(0,True),(1,True),(2,True)])
def test_automatic_observes_exact_lifecycle_including_slow_start(tmp_path,start,rollback):
    states=lifecycle(rollback);cfg=config(states[0]);clock=Clock();client=Mock()
    cfg['duration']=10
    def current():return copy.deepcopy(states[min(start+int(clock()),len(states)-1)])
    client.state.side_effect=current;client.ready.return_value=True;client.verify_pair.return_value=True
    client.metadata.return_value=metadata()
    def send(*_):
        assert current()['active']['phase']=='RAMP'
        return dict(status=500 if rollback else 200,track='canary',latency_ms=1)
    client.send.side_effect=send
    result=execute(cfg,payload(),client,Evidence(tmp_path),clock,clock.sleep)
    assert result['outcome']==('FAIL' if rollback else 'PASS')
    assert result['reason']==('canary_errors' if rollback else 'release_completed')
    if not rollback:assert result['stages']==[10,25,50,100]
    if start:client.send.assert_not_called();client.ready.assert_not_called()
    else:assert client.send.call_count>0


@pytest.mark.parametrize('rollback',[False,True])
@pytest.mark.parametrize('mutation',['start_auth','terminal_auth','start_pair','stable','missing_start','other_release','missing_stage_or_failed','terminal_pair_or_previous'])
def test_automatic_terminal_requires_exact_authorization_and_audit(tmp_path,rollback,mutation):
    states=lifecycle(rollback);cfg=config(states[0]);terminal=states[-1]
    start=next(row for row in terminal['audit'] if row['reason']=='release_started')
    end=terminal['audit'][-1]
    if mutation=='start_auth':start['authorization_id']='sha256:'+'f'*64
    if mutation=='terminal_auth':end['authorization_id']='sha256:'+'f'*64
    if mutation=='start_pair':start['pair']['storage_uri']+='wrong'
    if mutation=='stable':terminal['stable']['storage_uri']+='wrong'
    if mutation=='missing_start':terminal['audit'].remove(start)
    if mutation=='other_release':terminal['audit'].insert(terminal['audit'].index(start)+1,dict(reason='release_started',pair=start['pair'],authorization_id='other'))
    if mutation=='missing_stage_or_failed':
        if rollback:terminal['failed']=[]
        else:terminal['audit']=[row for row in terminal['audit'] if row.get('previous_weight')!=25]
    if mutation=='terminal_pair_or_previous':end['previous_stable' if rollback else 'pair']['storage_uri']+='wrong'
    client=Mock();client.state.return_value=terminal;clock=Clock()
    result=execute(cfg,payload(),client,Evidence(tmp_path),clock,clock.sleep)
    assert result['outcome']=='BLOCKED';client.send.assert_not_called()


@pytest.mark.parametrize('at_read',[3,4])
def test_ramp_transition_during_presend_checks_observes_without_sending(tmp_path,at_read):
    states=lifecycle();cfg=config(states[0]);client=Mock();clock=Clock();reads=[0]
    def current():
        reads[0]+=1
        return copy.deepcopy(states[0] if reads[0]<at_read else states[min(1+int(clock()),3)])
    client.state.side_effect=current;client.ready.return_value=True;client.verify_pair.return_value=True
    client.metadata.return_value=metadata()
    result=execute(cfg,payload(),client,Evidence(tmp_path),clock,clock.sleep)
    assert result['outcome']=='PASS';client.send.assert_not_called()


def test_budget_exhaustion_observes_terminal_and_wrong_baseline_blocks(tmp_path):
    states=lifecycle();cfg=config(states[0]);cfg['requests']=1;clock=Clock();client=Mock()
    client.state.side_effect=lambda:copy.deepcopy(states[min(int(clock()),3)])
    client.ready.return_value=True;client.verify_pair.return_value=True;client.metadata.return_value=metadata()
    client.send.return_value=dict(status=200,track='canary',latency_ms=1)
    result=execute(cfg,payload(),client,Evidence(tmp_path/'budget'),clock,clock.sleep)
    assert result['outcome']=='PASS' and result['requests']==1
    altered=states[0];altered['active']['previous_stable']['storage_uri']+='wrong'
    client.state.side_effect=None;client.state.return_value=altered;client.send.reset_mock();clock=Clock()
    result=execute(cfg,payload(),client,Evidence(tmp_path/'baseline'),clock,clock.sleep)
    assert result['outcome']=='BLOCKED';client.send.assert_not_called()

def test_admission_policy_casts_quantity_bearing_structs_to_dynamic_access():
    objects=proposed_rbac(IMAGE)['items']
    policy=next(o for o in objects if o['kind']=='ValidatingAdmissionPolicy')
    expressions=' '.join(v['expression'] for v in policy['spec']['validations'])
    for required in ("dyn(object.spec.template.spec.containers[0].resources).requests['cpu'] == quantity('50m')",
                     "dyn(object.spec.template.spec.containers[0].resources).requests['memory'] == quantity('64Mi')",
                     "dyn(object.spec.template.spec.containers[0].resources).limits['cpu'] == quantity('500m')",
                     "dyn(object.spec.template.spec.containers[0].resources).limits['memory'] == quantity('256Mi')",
                     "dyn(object.spec.template.spec.volumes[0].emptyDir).sizeLimit == quantity('32Mi')"):
        assert required in expressions
    assert '.?resources' not in expressions and '.?emptyDir' not in expressions
    assert 'has(object.spec.template.spec.containers[0].resources.requests)' not in expressions
    assert 'has(object.spec.template.spec.volumes[0].emptyDir.sizeLimit)' not in expressions


def resource_volume_fixture_allowed(job):
    try:
        pod=job['spec']['template']['spec'];container=pod['containers'][0]
        resources=container['resources'];requests=resources['requests'];limits=resources['limits']
        volume=pod['volumes'][0]
        return (requests=={'cpu':'50m','memory':'64Mi'} and
                limits=={'cpu':'500m','memory':'256Mi'} and
                volume['name']=='evidence' and volume['emptyDir']['sizeLimit']=='32Mi')
    except (KeyError,IndexError,TypeError):
        return False


@pytest.mark.parametrize('mutation',[
    'missing_resources','missing_requests','missing_limits','wrong_request_cpu','wrong_request_memory',
    'wrong_limit_cpu','wrong_limit_memory','missing_empty_dir','missing_size_limit','wrong_size_limit'])
def test_admission_resource_volume_fixtures_fail_closed(mutation):
    s,_,_,_=ramp();job=render_job(s.read()['active'],IMAGE,'DEMO-APPROVAL-1')
    assert resource_volume_fixture_allowed(job)
    pod=job['spec']['template']['spec'];container=pod['containers'][0];volume=pod['volumes'][0]
    if mutation=='missing_resources':container.pop('resources')
    elif mutation=='missing_requests':container['resources'].pop('requests')
    elif mutation=='missing_limits':container['resources'].pop('limits')
    elif mutation=='wrong_request_cpu':container['resources']['requests']['cpu']='51m'
    elif mutation=='wrong_request_memory':container['resources']['requests']['memory']='65Mi'
    elif mutation=='wrong_limit_cpu':container['resources']['limits']['cpu']='501m'
    elif mutation=='wrong_limit_memory':container['resources']['limits']['memory']='257Mi'
    elif mutation=='missing_empty_dir':volume.pop('emptyDir')
    elif mutation=='missing_size_limit':volume['emptyDir'].pop('sizeLimit')
    else:volume['emptyDir']['sizeLimit']='33Mi'
    assert not resource_volume_fixture_allowed(job)
