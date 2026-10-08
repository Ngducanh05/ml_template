import copy
import json
import time
from pathlib import Path
from unittest.mock import Mock
import pytest
from scripts.e2e_v2.runner import validate_config,admit,observation,execute,Evidence,validate_payload,Live
from scripts.e2e_v2.render import render


def config(mode="observe",**kw):
    return dict(version="7",mode=mode,namespace="production",run_id="release-seven",rps=0 if mode=="observe" else 2,
        concurrency=2,requests=3,duration=10,api_retries=2,allow_synthetic_gate_traffic=mode=="policy-test",**kw)


def state():
    return dict(schema_version=2,model_name="credit-risk-classifier",stable={"model_version":"6"},highest_accepted=6,
        active=None,pending=[],processed=[{"model_version":"6"}],failed=[],route={"weight":0},audit=[])


def complete():
    s=state();s["stable"]={"model_version":"7"};s["processed"].append(s["stable"])
    s["audit"]=[dict(reason="release_started",pair={"model_version":"7"})]+[dict(reason="stage_promoted",previous_weight=x) for x in (10,25,50,100)]+[dict(reason="release_completed",pair={"model_version":"7"})]
    return s


def payload(): return {"inputs":[dict(name="age",datatype="INT64",shape=[1,1],data=[40])]}

def metadata(): return {"inputs":[dict(name="age",datatype="INT64")]}


class Clock:
    def __init__(self): self.now=0
    def __call__(self): return self.now
    def sleep(self,n): self.now+=n;time.sleep(0.0001)


@pytest.mark.parametrize("target",["0","-1","7.0","abc","2147483648"])
def test_invalid_version(target):
    c=config();c["version"]=target
    with pytest.raises(ValueError):validate_config(c)


@pytest.mark.parametrize("case",["processed","failed","stale","conflict"])
def test_replay_and_conflict_rejected(case):
    s=state()
    if case=="processed": s["processed"].append({"model_version":"7"})
    if case=="failed": s["failed"].append({"pair":{"model_version":"7"},"reason":"errors"})
    if case=="stale": s["highest_accepted"]=8
    if case=="conflict": s["active"]={"pair":{"model_version":"8"}}
    with pytest.raises(ValueError):admit(s,"7","observe")


@pytest.mark.parametrize("reason",["insufficient_canary_traffic","errors","invalid_canary_metrics"])
def test_durable_rollback_is_failure(reason):
    s=state();s["failed"]=[dict(pair={"model_version":"7"},reason=reason)]
    assert observation(s,"7")[:2]==("FAIL",reason)


def test_complete_requires_all_stages_and_exact_terminal_state():
    s=complete(); assert observation(s,"7")[0]=="PASS"
    del s["audit"][2];assert observation(s,"7")[0]=="BLOCKED"
    s=complete();s["route"]["weight"]=100;assert observation(s,"7")[0] is None


def test_synthetic_default_off_and_explicit_policy_opt_in():
    c=config();validate_config(c)
    c["rps"]=1
    with pytest.raises(ValueError):validate_config(c)
    c=config("policy-test");c["allow_synthetic_gate_traffic"]=False
    with pytest.raises(ValueError):validate_config(c)


def test_readonly_job_identity_and_limits():
    c=config();image="example/repo@sha256:"+"a"*64
    first=render(c,payload(),image);second=render(c,payload(),image)
    assert first==second
    role=next(x for x in first["items"] if x["kind"]=="Role")
    assert role["rules"][:1]==[dict(apiGroups=[""],resources=["configmaps"],resourceNames=["model-release-state-v2"],verbs=["get"])]
    job=first["items"][-1];assert job["spec"]["backoffLimit"]==0 and job["spec"]["activeDeadlineSeconds"]==40
    c["duration"]=20
    changed=render(c,payload(),image)["items"][-1]
    assert changed["metadata"]["name"]==job["metadata"]["name"]
    assert changed["metadata"]["annotations"]!=job["metadata"]["annotations"]


def client(states):
    c=Mock();c.state.side_effect=states;c.ready.return_value=True;c.metadata.return_value=metadata()
    c.send.return_value=dict(status=200,track="stable",latency_ms=2)
    return c


def test_execute_observes_success_without_sending_traffic(tmp_path):
    c=client([state(),complete()]);clock=Clock()
    result=execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep)
    assert result["outcome"]=="PASS" and result["stages"]==[10,25,50,100]
    c.send.assert_not_called()


def test_transient_api_error_recovers(tmp_path):
    c=client([state(),OSError("private token must not leak"),complete()]);clock=Clock()
    result=execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep)
    assert result["outcome"]=="PASS"
    assert "private token" not in (tmp_path/"events.jsonl").read_text()


def test_exhausted_api_retry_blocks(tmp_path):
    c=client([state()]+[OSError("secret")]*3);clock=Clock()
    assert execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep)["reason"]=="kubernetes_unavailable"


@pytest.mark.parametrize("status",[200,422,500])
def test_baseline_bounded_http_results(tmp_path,status):
    s=state();c=client([]);c.state.side_effect=None;c.state.return_value=s
    c.send.return_value=dict(status=status,track="stable",latency_ms=2)
    cfg=config("baseline");cfg["version"]="6";clock=Clock()
    result=execute(cfg,payload(),c,Evidence(tmp_path),clock,clock.sleep)
    assert result["requests"]==3
    assert result["outcome"]==("PASS" if status==200 else "FAIL")
    assert "inputs" not in (tmp_path/"events.jsonl").read_text()


def test_router_not_ready_and_timeout_never_pass(tmp_path):
    c=client([state()]);c.ready.return_value=False;clock=Clock()
    assert execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep)["outcome"]=="BLOCKED"
    c=client([]);c.state.side_effect=None;c.state.return_value=state();clock=Clock()
    assert execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep)["reason"]=="timeout"


def test_baseline_stops_if_release_starts(tmp_path):
    start=state();active=state();active["active"]={"pair":{"model_version":"7"},"phase":"RAMP"}
    c=client([start,active]);cfg=config("baseline");cfg["version"]="6";clock=Clock()
    assert execute(cfg,payload(),c,Evidence(tmp_path),clock,clock.sleep)["outcome"]=="BLOCKED"
    c.send.assert_not_called()


def test_bad_feature_signature_is_rejected():
    bad=payload();bad["inputs"][0]["name"]="missing"
    with pytest.raises(ValueError):validate_payload(bad,metadata())


def test_cancellation_is_not_pass(tmp_path):
    c=client([state()]);clock=Clock()
    assert execute(config(),{},c,Evidence(tmp_path),clock,clock.sleep,cancelled=lambda:True)["reason"]=="cancelled"


def test_backend_pending_rollout_never_proves_baseline(tmp_path):
    c=client([]); c.state.side_effect=None; c.state.return_value=state(); c.verify_pair.return_value=False
    cfg=config('baseline'); cfg['version']='6'; clock=Clock()
    assert execute(cfg,payload(),c,Evidence(tmp_path),clock,clock.sleep)['outcome']=='BLOCKED'
    c.send.assert_not_called()


@pytest.mark.parametrize('shape', [[2,1],[0,1],[-1,1],[True,1]])
def test_bad_payload_shape_rejected(shape):
    body=payload(); body['inputs'][0]['shape']=shape
    with pytest.raises(ValueError): validate_payload(body,metadata())


@pytest.mark.parametrize('mutation', ['surge','old_generation','old_version','old_artifact','wrong_image','unavailable'])
def test_exact_backend_rollout_rejects_old_or_incomplete_deployment(mutation):
    from scripts.e2e_v2.runner import deployment_matches
    pair=dict(model_version='6',storage_uri='s3://bucket/model',runtime_digest='repo@sha256:'+'a'*64)
    obj=dict(metadata=dict(generation=2),spec=dict(replicas=1,template=dict(metadata=dict(annotations={
        'ml-template.io/model-version':'6','internal.serving.kserve.io/storage-initializer-sourceuri':pair['storage_uri']}),
        spec=dict(containers=[dict(name='kserve-container',image=pair['runtime_digest'])]))),
        status=dict(observedGeneration=2,updatedReplicas=1,replicas=1,readyReplicas=1,availableReplicas=1))
    assert deployment_matches(obj,pair)
    if mutation=='surge': obj['status']['replicas']=2
    if mutation=='old_generation': obj['status']['observedGeneration']=1
    if mutation=='old_version': obj['spec']['template']['metadata']['annotations']['ml-template.io/model-version']='3'
    if mutation=='old_artifact': obj['spec']['template']['metadata']['annotations']['internal.serving.kserve.io/storage-initializer-sourceuri']='s3://bucket/old'
    if mutation=='wrong_image': obj['spec']['template']['spec']['containers'][0]['image']='repo:latest'
    if mutation=='unavailable': obj['status']['availableReplicas']=0
    assert not deployment_matches(obj,pair)


def test_policy_test_uses_candidate_metadata(tmp_path):
    current=state(); current['active']=dict(pair={'model_version':'7'},phase='RAMP')
    c=client([]); c.state.side_effect=None; c.state.return_value=current
    clock=Clock(); result=execute(config('policy-test'),payload(),c,Evidence(tmp_path),clock,clock.sleep)
    assert result['requests']==3 and result['outcome']=='BLOCKED'
    assert {call.args for call in c.metadata.call_args_list}=={('canary',),('stable',)}
