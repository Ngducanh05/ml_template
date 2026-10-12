import copy
import json
from datetime import datetime,timezone
from unittest.mock import Mock
import pytest
from control_plane.state import (BASE_DIGEST,Conflict,Store,initial_state,event_candidate,enqueue,reconcile_candidates,ingest,HISTORY)
from control_plane.watcher import handle_message,reconcile,check_queues,QUEUE,DLQ,QUEUE_ARN,DLQ_ARN
from control_plane.controller import Controller,assess
from control_plane.router import Routing

DIGEST='sha256:'+'b'*64

class Backend:
    def __init__(self): self.state=initial_state();self.rev=0;self.conflicts=0;self.fail=False;self.commits=0
    def read_state(self): return str(self.rev),copy.deepcopy(self.state)
    def write_state(self,rev,state):
        if self.fail: raise OSError('unavailable')
        if self.conflicts: self.conflicts-=1;raise Conflict()
        if int(rev)!=self.rev: raise Conflict()
        self.state=copy.deepcopy(state);self.rev+=1;self.commits+=1

def event(tag='credit-risk-classifier-v3',digest=DIGEST):
    return {'detail-type':'ECR Image Action','source':'aws.ecr','account':'378457871131','region':'ap-southeast-1','time':'2026-10-06T08:00:00Z','detail':{'action-type':'PUSH','result':'SUCCESS','repository-name':'mlflow-models','image-tag':tag,'image-digest':digest}}

def candidate(version=3,digest=DIGEST): return event_candidate(json.dumps(event(f'credit-risk-classifier-v{version}',digest)))[0]

def test_canonical_event_and_immutable_digest():
    value,reason=event_candidate(json.dumps(event()))
    assert reason is None and value['version']==3 and value['digest']==DIGEST

@pytest.mark.parametrize('field,value,reason',[
    ('image-tag','credit-risk-classifier-v3-7dffc35','noncanonical'),
    ('image-tag','other-model-v3','model_not_allowed'),
    ('repository-name','other','wrong_repository'),('action-type','DELETE','wrong_action'),
    ('result','FAILURE','failed_push'),('image-digest','latest','invalid_digest')])
def test_rejected_events(field,value,reason):
    body=event();body['detail'][field]=value
    assert event_candidate(json.dumps(body))==(None,reason)

@pytest.mark.parametrize('body',['bad json','null','[]','{"detail":null}'])
def test_malformed_event(body): assert event_candidate(body)[0] is None

def test_numeric_version_same_version_rebuild_and_lower_version():
    backend=Backend();backend.state['stable']={'version':3,'digest':BASE_DIGEST,'source_time':0};store=Store(backend)
    assert enqueue(store,candidate(2))=='stale'
    assert enqueue(store,candidate(3))=='accepted'
    assert enqueue(store,candidate(3))=='duplicate'
    assert enqueue(store,candidate(10,'sha256:'+'c'*64))=='accepted'
    assert max(x['version'] for x in store.read()['pending'])==10

def test_old_same_version_event_does_not_reverse_security_rebuild():
    backend=Backend();c=candidate(1);backend.state['stable']['source_time']=c['source_time']+1
    assert enqueue(Store(backend),c)=='stale_rebuild'

def test_configmap_state_survives_process_restart():
    backend=Backend();enqueue(Store(backend),candidate())
    restarted=Store(backend)
    assert restarted.read()['pending'][0]['digest']==DIGEST
    assert enqueue(restarted,candidate())=='duplicate'

def test_optimistic_conflict_retry():
    backend=Backend();backend.conflicts=2
    assert enqueue(Store(backend),candidate())=='accepted'
    assert len(backend.state['pending'])==1 and backend.commits==1

def test_delete_only_after_durable_commit_with_current_handle():
    backend=Backend();sqs=Mock()
    def deleted(**kwargs):
        assert backend.commits==1 and backend.state['pending'][0]['digest']==DIGEST
        assert kwargs['ReceiptHandle']=='newest-receipt'
    sqs.delete_message.side_effect=deleted
    handle_message(sqs,Store(backend),dict(Body=json.dumps(event()),ReceiptHandle='newest-receipt',MessageId='id'))
    sqs.delete_message.assert_called_once()

def test_transient_state_failure_does_not_delete():
    backend=Backend();backend.fail=True;sqs=Mock()
    with pytest.raises(OSError): handle_message(sqs,Store(backend),dict(Body=json.dumps(event()),ReceiptHandle='r'))
    sqs.delete_message.assert_not_called()

def test_invalid_message_recorded_before_delete():
    backend=Backend();sqs=Mock()
    handle_message(sqs,Store(backend),dict(Body='invalid',ReceiptHandle='r'))
    assert backend.state['audit'][-1]['reason']=='invalid_event'
    sqs.delete_message.assert_called_once()

def test_redelivery_after_delete_failure_is_deduplicated():
    backend=Backend();sqs=Mock();sqs.delete_message.side_effect=OSError()
    message=dict(Body=json.dumps(event()),ReceiptHandle='r')
    with pytest.raises(OSError): handle_message(sqs,Store(backend),message)
    sqs.delete_message.side_effect=None
    assert handle_message(sqs,Store(backend),dict(message,ReceiptHandle='r2'))=='duplicate'
    assert len(backend.state['pending'])==1

def test_dlq_count_surfaced_and_never_received():
    sqs=Mock();sqs.get_queue_attributes.side_effect=[{'Attributes':{'QueueArn':QUEUE_ARN,'MessageRetentionPeriod':'1209600','RedrivePolicy':json.dumps({'deadLetterTargetArn':DLQ_ARN,'maxReceiveCount':5})}}, {'Attributes':{'QueueArn':DLQ_ARN,'MessageRetentionPeriod':'1209600','ApproximateNumberOfMessages':'2'}}]
    assert check_queues(sqs)==2
    sqs.receive_message.assert_not_called();sqs.delete_message.assert_not_called()

def test_bad_redrive_policy_blocks_consumption():
    sqs=Mock();sqs.get_queue_attributes.return_value={'Attributes':{'QueueArn':QUEUE_ARN,'MessageRetentionPeriod':'1209600','RedrivePolicy':'{}'}}
    with pytest.raises(ValueError): check_queues(sqs)

def test_reconcile_missed_event_uses_same_dedup_path():
    backend=Backend();ecr=Mock()
    ecr.get_paginator.return_value.paginate.return_value=[{'imageDetails':[
        {'imageTags':['credit-risk-classifier-v3'],'imageDigest':'sha256:'+'c'*64,'imagePushedAt':1},
        {'imageTags':['credit-risk-classifier-v10','credit-risk-classifier-v10-abcd'],'imageDigest':DIGEST,'imagePushedAt':2}]}]
    assert reconcile(ecr,Store(backend))[0]['reason']=='accepted'
    assert backend.state['pending'][0]['version']==10
    assert enqueue(Store(backend),candidate(10))=='duplicate'

def test_bounded_audit():
    state=initial_state()
    for _ in range(HISTORY+10): ingest(state,None,'invalid_event')
    assert len(state['audit'])==HISTORY

class Clock:
    def __init__(self): self.now=1000
    def __call__(self): return self.now

class Effects:
    def __init__(self,store):
        self.store=store;self.images={'stable':BASE_DIGEST,'canary':BASE_DIGEST};self.replicas={'stable':1,'canary':0};self.actions=[];self.bad=False;self.fail_stable=False
    def ensure(self,track,digest,replicas):
        self.images[track]=digest;self.replicas[track]=replicas;self.actions.append((track,digest,replicas))
    def ready(self,track,c,*,wait_transport=False):
        if self.fail_stable and track=='stable' and c['digest']==DIGEST: return False
        return self.images[track]==c['digest'] and self.replicas[track]==1
    def router(self):
        route=self.store.read()['route'];return dict(weight=route['weight'],revision=route['revision'],ready=True)
    def metrics(self): return dict(up=1,age=1,traffic=20,success=20,errors=.1 if self.bad else 0,p95=.02,stable_errors=0)

def setup_controller():
    backend=Backend();store=Store(backend);enqueue(store,candidate())
    clock=Clock();effects=Effects(store)
    controller=Controller(store,effects,owner='worker',clock=clock,dwell=0,timeout=3)
    return store,clock,effects,controller

def drive(store,clock,controller,count=30):
    for _ in range(count):
        controller.tick();clock.now+=1
        if store.read()['active'] is None and not store.read()['pending']: return
    raise AssertionError(store.read())

def test_success_finalizes_stable_and_returns_zero():
    store,clock,effects,controller=setup_controller();drive(store,clock,controller)
    state=store.read()
    assert state['stable']['digest']==DIGEST==effects.images['stable']
    assert state['route']['weight']==0 and state['active'] is None
    assert effects.replicas['canary']==0 and DIGEST in state['processed']
    assert [x['weight'] for x in state['audit'] if x['reason']=='stage_pass']==[25,50,100]

def test_rollback_preserves_stable():
    store,clock,effects,controller=setup_controller();effects.bad=True;drive(store,clock,controller)
    assert effects.images['stable']==BASE_DIGEST==store.read()['stable']['digest']
    assert not any(x[0]=='stable' for x in effects.actions)
    assert store.read()['route']['weight']==0 and store.read()['failed'][0]['digest']==DIGEST

def test_finalization_failure_restores_previous_stable():
    store,clock,effects,controller=setup_controller();effects.fail_stable=True;drive(store,clock,controller,50)
    assert effects.images['stable']==BASE_DIGEST==store.read()['stable']['digest']
    assert store.read()['route']['weight']==0 and store.read()['failed']

@pytest.mark.parametrize('phase',['PREPARING','RAMP','FINALIZING','DRAINING'])
def test_controller_restart_resumes_durable_phase(phase):
    store,clock,effects,controller=setup_controller()
    controller.timeout=300  # Restart is within the durable stage deadline.
    for _ in range(25):
        controller.tick();clock.now+=1
        if (store.read()['active'] or {}).get('phase')==phase: break
    else: raise AssertionError('phase not reached')
    clock.now+=61
    restarted=Controller(Store(store.backend),effects,owner='replacement',clock=clock,dwell=0,timeout=300)
    drive(store,clock,restarted)
    assert store.read()['stable']['digest']==DIGEST

def test_controller_lease_prevents_second_writer():
    store,clock,effects,controller=setup_controller();controller.tick()
    second=Controller(store,effects,owner='second',clock=clock,dwell=0)
    before=copy.deepcopy(store.read()['active']);second.tick()
    assert store.read()['active']==before and not effects.actions

def test_no_successful_requests_fails_closed():
    assert 'no_successful_canary_predictions' in assess(dict(up=1,age=1,traffic=10,success=0,errors=0,p95=.01,stable_errors=0))

def test_router_starts_zero_and_stale_active_lease_fails_closed():
    clock=Clock();router=Routing(clock)
    assert router.status()==dict(ready=False,weight=0,revision=-1)
    state=initial_state();router.set(state)
    assert router.status()['weight']==0 and router.choose()=='stable'
    state['active']=candidate();state['route']={'weight':50,'revision':1};state['lease']={'owner':'x','until':clock.now+2};router.set(state)
    assert router.status()['weight']==50
    clock.now+=3
    assert router.choose() is None and not router.status()['ready']

def test_pending_old_same_version_cannot_reverse_newer_promotion():
    store,clock,effects,controller=setup_controller()
    current=store.read()['pending'][0]
    store.update(lambda s:s.update(stable={'version':current['version'],'digest':'sha256:'+'e'*64,'source_time':current['source_time']+1}))
    controller.tick()
    assert store.read()['active'] is None and store.read()['pending']==[]
    assert effects.actions==[]


def ramp_with_metrics(values,minimum=1):
    store,clock,effects,controller=setup_controller()
    controller.timeout=300;controller.minimum=minimum
    for _ in range(3): controller.tick();clock.now+=1
    assert store.read()['active']['phase']=='RAMP'
    effects.metrics=lambda:values
    return store,clock,effects,controller


def test_zero_observations_wait_without_failure_or_promotion(capsys):
    values=dict(up=1,age=4.739,traffic=0,success=0,errors=float('nan'),p95=float('nan'),stable_errors=0)
    store,clock,effects,controller=ramp_with_metrics(values)
    before=copy.deepcopy(store.read()['stable']);controller.tick()
    assert store.read()['active']['phase']=='RAMP' and store.read()['route']['weight']==10
    assert store.read()['failed']==[] and store.read()['stable']==before
    assert 'RAMP_WAIT_TRAFFIC' in capsys.readouterr().out
    assert not any(a[0]=='stable' for a in effects.actions)


def test_insufficient_observations_deadline_rolls_back_and_preserves_stable():
    values=dict(up=1,age=1,traffic=0,success=0,errors=float('nan'),p95=float('nan'),stable_errors=0)
    store,clock,effects,controller=ramp_with_metrics(values)
    clock.now=store.read()['active']['deadline'];controller.tick()
    assert store.read()['active']['reason']=='insufficient_canary_traffic'
    controller.tick()
    assert store.read()['failed'][-1]['reason']=='insufficient_canary_traffic'
    assert store.read()['stable']['digest']==BASE_DIGEST==effects.images['stable']
    assert store.read()['route']['weight']==0


def test_restart_does_not_reset_insufficient_traffic_deadline():
    values=dict(up=1,age=1,traffic=0,success=0,errors=float('nan'),p95=float('nan'),stable_errors=0)
    store,clock,effects,controller=ramp_with_metrics(values)
    clock.now=store.read()['active']['deadline']+1
    restarted=Controller(store,effects,owner='new-worker',clock=clock,dwell=0)
    restarted.tick()
    assert store.read()['active']['reason']=='insufficient_canary_traffic'


def test_below_configured_minimum_waits_even_with_finite_metrics():
    values=dict(up=1,age=1,traffic=4,success=4,errors=0,p95=.02,stable_errors=0)
    store,clock,effects,controller=ramp_with_metrics(values,minimum=5)
    controller.tick();assert store.read()['route']['weight']==10
    values.update(traffic=5,success=5)
    controller.tick();assert store.read()['route']['weight']==25


@pytest.mark.parametrize('metric,value',[('errors',float('nan')),('p95',float('inf')),('p95',None)])
def test_invalid_quality_after_minimum_fails_safely(metric,value):
    values=dict(up=1,age=1,traffic=10,success=10,errors=0,p95=.02,stable_errors=0)
    if value is None: del values[metric]
    else: values[metric]=value
    store,clock,effects,controller=ramp_with_metrics(values)
    controller.tick();assert store.read()['active']['reason']=='invalid_canary_metrics'
    controller.tick()
    assert store.read()['stable']['digest']==BASE_DIGEST==effects.images['stable']
    assert store.read()['failed'][-1]['reason']=='invalid_canary_metrics'


def test_effects_nonfinite_and_missing_samples_are_values_not_exceptions(monkeypatch):
    from control_plane.controller import Effects as LiveEffects
    values=iter(['1','4.739','0','0','NaN','NaN','0'])
    monkeypatch.setattr('control_plane.controller.api',lambda url:{'status':'success','data':{'result':[{'value':[0,next(values)]}]}})
    import math
    result=LiveEffects(None).metrics()
    assert result['traffic']==0 and math.isnan(result['p95']) and math.isnan(result['errors'])
    monkeypatch.setattr('control_plane.controller.api',lambda url:{'status':'success','data':{'result':[]}})
    assert math.isnan(LiveEffects(None).metrics()['p95'])


def test_zero_safe_error_query_and_finite_latency_with_traffic(monkeypatch):
    from control_plane.controller import Effects as LiveEffects
    from urllib.parse import urlparse,parse_qs
    values=iter(['1','1','10','10','0','.02','0']);queries=[]
    def response(url):
        queries.append(parse_qs(urlparse(url).query)['query'][0])
        return {'status':'success','data':{'result':[{'value':[0,next(values)]}]}}
    monkeypatch.setattr('control_plane.controller.api',response)
    metrics=LiveEffects(None).metrics()
    assert 'clamp_min(' in queries[4] and '1e-12)' in queries[4]
    assert metrics['errors']==0 and metrics['p95']==.02 and assess(metrics)==[]


def failed_store():
    backend=Backend()
    backend.state['failed']=[{'digest':DIGEST,'version':3,'reason':'ValueError','time':1}]
    return Store(backend)


def test_failed_digest_deduped_by_sqs_and_reconciliation():
    store=failed_store()
    assert enqueue(store,candidate())=='duplicate'
    ecr=Mock();ecr.get_paginator.return_value.paginate.return_value=[{'imageDetails':[
        {'imageTags':['credit-risk-classifier-v3'],'imageDigest':DIGEST,'imagePushedAt':2}]}]
    assert reconcile(ecr,store)[0]['reason']=='duplicate'
    assert store.read()['pending']==[]


def test_operator_retry_cas_requeues_once_and_keeps_failure_history():
    from control_plane.state import operator_retry,Busy
    store=failed_store();store.backend.conflicts=2
    operator_retry(store,DIGEST,3,'test-operator','reviewed traffic-gate fix')
    state=store.read()
    assert len(state['pending'])==1 and state['pending'][0]['version']==3
    assert state['failed'][0]['digest']==DIGEST and state['stable']['digest']==BASE_DIGEST
    assert state['audit'][-1]['reason']=='operator_retry' and store.backend.commits==1
    with pytest.raises(Busy): operator_retry(store,DIGEST,3,'test-operator','duplicate request')
    assert store.backend.commits==1 and len(store.read()['pending'])==1
    assert enqueue(store,candidate())=='duplicate'


def test_retry_conflict_detects_concurrent_pending_writer():
    from control_plane.state import operator_retry,Busy
    store=failed_store();original=store.backend.write_state
    def concurrent(rev,state):
        store.backend.state['pending']=[candidate()];store.backend.rev+=1
        store.backend.write_state=original
        raise Conflict()
    store.backend.write_state=concurrent
    with pytest.raises(Busy): operator_retry(store,DIGEST,3,'operator','reviewed')
    assert len(store.read()['pending'])==1 and store.read()['audit']==[]


def test_operator_retry_rejects_active_and_unknown_failed_version():
    from control_plane.state import operator_retry,Busy
    store=failed_store();store.backend.state['active']=candidate()
    with pytest.raises(Busy): operator_retry(store,DIGEST,3,'operator','reviewed')
    store.backend.state['active']=None
    with pytest.raises(ValueError): operator_retry(store,DIGEST,4,'operator','reviewed')
    assert store.backend.commits==0 and store.read()['pending']==[]


def test_release_policy_defaults_and_rendered_overrides():
    import importlib.util
    from pathlib import Path
    from control_plane.policy import Policy
    p=Policy.from_env({})
    assert (p.minimum,p.dwell,p.timeout,p.error_limit,p.p95_limit)==(1,35,300,.05,.1)
    spec=importlib.util.spec_from_file_location('credit_render',Path(__file__).resolve().parents[2]/'k8s/credit-risk/render.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    _,workloads,_=module.build('596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@'+DIGEST,{'MIN_CANARY_REQUESTS':'25','CANARY_DWELL_SECONDS':'40','RELEASE_STAGE_TIMEOUT_SECONDS':'400','MAX_ERROR_RATIO':'.02','MAX_P95_SECONDS':'.2'})
    deployment=next(w for w in workloads if w['kind']=='Deployment' and w['metadata']['name']=='model-release-controller')
    env={e['name']:e['value'] for e in deployment['spec']['template']['spec']['containers'][0]['env']}
    assert Policy.from_env(env)==Policy(25,40,400,.02,.2)
    assert deployment['spec']['replicas']==0


@pytest.mark.parametrize('values',[{'MIN_CANARY_REQUESTS':'0'},{'CANARY_DWELL_SECONDS':'nan'},{'RELEASE_STAGE_TIMEOUT_SECONDS':'30'}])
def test_invalid_release_policy_rejected(values):
    from control_plane.policy import Policy
    with pytest.raises(ValueError): Policy.from_env(values)


def finalizing_controller(monkeypatch,probe):
    from control_plane.controller import Effects as LiveEffects
    store,clock,effects,controller=setup_controller();controller.timeout=300
    for _ in range(30):
        controller.tick();clock.now+=1
        if (store.read()['active'] or {}).get('phase')=='FINALIZING': break
    else: raise AssertionError('FINALIZING not reached')
    kube=Mock();kube.ready.return_value=True
    live=LiveEffects(kube);original=effects.ready
    def ready(track,c,*,wait_transport=False):
        if track=='stable' and c['digest']==DIGEST:
            return live.ready(track,c,wait_transport=wait_transport)
        return original(track,c,wait_transport=wait_transport)
    effects.ready=ready
    monkeypatch.setattr('control_plane.controller.api',probe)
    return store,clock,effects,controller,kube


@pytest.mark.parametrize('endpoint',['/health','/metadata'])
@pytest.mark.parametrize('error',['url','timeout','reset'])
def test_finalize_transport_wait_then_success(monkeypatch,capsys,endpoint,error):
    from urllib.error import URLError
    exc={'url':URLError('not yet reachable'),'timeout':TimeoutError(),'reset':ConnectionResetError()}[error]
    attempts=[]
    def probe(url):
        if url.endswith(endpoint) and not attempts:
            attempts.append(url);raise exc
        return {'model_name':'credit-risk-classifier','model_version':'3'}
    store,clock,effects,controller,kube=finalizing_controller(monkeypatch,probe)
    deadline=store.read()['active']['deadline'];controller.tick()
    assert store.read()['active']['phase']=='FINALIZING'
    assert store.read()['active']['deadline']==deadline
    assert store.read()['stable']['digest']==BASE_DIGEST and store.read()['failed']==[]
    assert store.read()['route']['weight']==100
    assert 'FINALIZE_WAIT_BACKEND' in capsys.readouterr().out
    controller.tick()
    assert store.read()['active']['phase']=='DRAINING' and store.read()['stable']['digest']==DIGEST
    controller.tick();assert store.read()['active'] is None and store.read()['route']['weight']==0


def test_finalize_repeated_urlerror_deadline_restores_previous_stable(monkeypatch):
    from urllib.error import URLError
    def unavailable(url): raise URLError('connection refused')
    store,clock,effects,controller,kube=finalizing_controller(monkeypatch,unavailable)
    controller.tick();assert store.read()['active']['phase']=='FINALIZING'
    clock.now=store.read()['active']['deadline'];controller.tick()
    assert store.read()['active']['reason']=='stable_backend_unavailable'
    drive(store,clock,controller)
    assert store.read()['stable']['digest']==BASE_DIGEST==effects.images['stable']
    assert store.read()['failed'][-1]['reason']=='stable_backend_unavailable'


def test_finalize_metadata_mismatch_is_immediate_failure(monkeypatch):
    store,clock,effects,controller,kube=finalizing_controller(monkeypatch,lambda url:{'model_name':'wrong-model','model_version':'3'})
    controller.tick()
    assert store.read()['active']['phase']=='ROLLBACK' and store.read()['active']['reason']=='ValueError'
    assert store.read()['stable']['digest']==BASE_DIGEST


def test_finalize_requires_kube_readiness_before_http(monkeypatch):
    probe=Mock()
    store,clock,effects,controller,kube=finalizing_controller(monkeypatch,probe)
    kube.ready.return_value=False;controller.tick();probe.assert_not_called()
    assert store.read()['active']['phase']=='FINALIZING' and store.read()['stable']['digest']==BASE_DIGEST
    clock.now=store.read()['active']['deadline'];controller.tick()
    assert store.read()['active']['reason']=='stable_readiness_timeout'


def test_transport_outside_finalize_retains_original_exception(monkeypatch):
    from urllib.error import URLError
    from control_plane.controller import Effects as LiveEffects
    kube=Mock();kube.ready.return_value=True
    monkeypatch.setattr('control_plane.controller.api',Mock(side_effect=URLError('reset')))
    with pytest.raises(URLError): LiveEffects(kube).ready('canary',candidate())


def test_finalize_http_error_response_is_not_transport_wait(monkeypatch):
    from urllib.error import HTTPError
    probe=Mock(side_effect=HTTPError('http://stable/health',503,'unhealthy',{},None))
    store,clock,effects,controller,kube=finalizing_controller(monkeypatch,probe)
    controller.tick()
    assert store.read()['active']['phase']=='ROLLBACK' and store.read()['active']['reason']=='HTTPError'
