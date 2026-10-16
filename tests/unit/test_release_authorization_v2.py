import copy
import json
from pathlib import Path
from unittest.mock import Mock
import pytest
import yaml
from control_plane.authorization_v2 import *
from control_plane.state_v2 import Store, enqueue
from control_plane.state import Conflict
from control_plane.controller_v2 import Controller
from test_controller_v2 import setup, OLD
from test_state_v2 import event
from approval_helpers import approve
from scripts import release_v2 as release


def expected(store):
    return evidence(store.read(),'3','TICKET-123',dict(repository=REPOSITORY,workflow=WORKFLOW,run_id='103',run_attempt='1',sha='a'*40,ref='refs/tags/v1.0.0'),'sha256:'+'d'*64,0)


def test_ready_never_starts_until_exact_durable_approval():
    b,s,e,clock,c=setup(False)
    for _ in range(3):Controller(Store(b),e,owner=c.owner,clock=lambda:0).tick()
    assert s.read()['active'] is None and s.read()['route']['weight']==0 and e.lanes=={'stable':OLD}
    approve(s);c.tick()
    assert s.read()['active']['phase']=='PREPARING' and s.read()['stable']==OLD
    assert s.read()['active']['authorization']['evidence']['ticket_reference']=='TEST-123'


@pytest.mark.parametrize('field,value',[('model_version','4'),('runtime_digest',OLD['runtime_digest'].replace('a'*64,'f'*64)),('storage_uri',event(4)['artifact_uri'])])
def test_wrong_pair_cannot_use_authorization(field,value):
    _,s,_,_,_=setup(False);ev=expected(s);ev['candidate']['pair'][field]=value
    with pytest.raises(ValueError):authorize(s,ev,now=0)
    assert s.read()['active'] is None and 'authorization' not in s.read()['pending'][0]


def test_wrong_fingerprint_rejected():
    _,s,_,_,_=setup(False);ev=expected(s);ev['candidate']['fingerprint']='sha256:'+'e'*64
    with pytest.raises(ValueError):authorize(s,ev,now=0)


@pytest.mark.parametrize('value',['',' ','a\nb','secret?token=value','<script>','x'*129])
def test_ticket_invalid(value):
    _,s,_,_,_=setup(False)
    with pytest.raises(ValueError):evidence(s.read(),'3',value,expected(s)['source'],'sha256:'+'d'*64,0)


def test_ticket_evidence_does_not_authorize():
    _,s,_,_,c=setup(False);expected(s);c.tick();assert s.read()['active'] is None


def test_idempotent_cas_does_not_extend_or_duplicate():
    b,s,_,_,_=setup(False);ev=expected(s);b.conflicts=2
    first=authorize(s,ev,now=0);again=authorize(Store(b),ev,now=1)
    assert first==again and first['expires_at']==900
    assert len([x for x in s.read()['audit'] if x['reason']=='release_authorized'])==1
    other=copy.deepcopy(ev);other['source']['run_id']='104'
    with pytest.raises(ValueError):authorize(s,other,now=2)


def test_concurrent_newer_ingest_invalidates_cas_authorization():
    b,s,_,_,_=setup(False);ev=expected(s);original=b.write_state;once=[False]
    def write(rv,state):
        if not once[0]:
            once[0]=True;enqueue(Store(b),event(4));raise Conflict()
        return original(rv,state)
    b.write_state=write
    with pytest.raises(ValueError):authorize(s,ev,now=0)
    assert all('authorization' not in x for x in s.read()['pending'])


def test_newer_unresolved_candidate_blocks_older_approval_and_claim():
    _,s,_,_,c=setup(False);ev=expected(s);enqueue(s,event(4))
    with pytest.raises(ValueError):authorize(s,ev,now=0)
    _,s,_,_,c=setup();enqueue(s,event(4));c.tick();assert s.read()['active'] is None


def test_expiry_and_stable_baseline_change_fail_closed():
    _,s,_,clock,c=setup();clock[0]=900;c.tick();assert s.read()['active'] is None
    with pytest.raises(ValueError):authorize(s,s.read()['pending'][0]['authorization']['evidence'],now=900)
    _,s,_,_,c=setup();s.update(lambda x:x.update(stable_fingerprint='sha256:'+'f'*64));c.tick()
    assert s.read()['active'] is None


def test_v9_build_required_and_security_block_never_authorized():
    _,s,_,_,c=setup(False)
    s.update(lambda x:x['pending'][0].update(status='BUILD_REQUIRED'))
    with pytest.raises(ValueError):expected(s)
    c.tick();assert s.read()['active'] is None
    from scripts.runtime_ci import require_clean_scan
    from test_runtime_security import report,REQUEST
    with pytest.raises(ValueError):require_clean_scan(report('CRITICAL','3.15.0'),REQUEST)


def run_identity(src):
    return dict(id=int(src['run_id']),head_sha=src['sha'],head_branch='v1.0.0',path=WORKFLOW,event='workflow_dispatch',
        run_attempt=1,status='in_progress',conclusion=None,repository={'full_name':REPOSITORY},actor={'login':'initiator'})


def reviews(state='approved'):
    return [dict(state=state,environments=[{'name':'production'}],user={'login':'reviewer'})]


@pytest.mark.parametrize('status',['rejected','pending','canceled'])
def test_native_rejection_never_authorizes(status):
    _,s,_,_,_=setup(False);ev=expected(s)
    with pytest.raises(ValueError):release.verify_human_review(run_identity(ev['source']),reviews(status),ev['source'])
    assert 'authorization' not in s.read()['pending'][0]


@pytest.mark.parametrize('field,value',[('head_sha','b'*40),('path','.github/workflows/demo-human-approval.yml'),('run_attempt',2),('status','completed'),('conclusion','cancelled'),('head_branch','feat/other')])
def test_native_review_bound_to_exact_source(field,value):
    _,s,_,_,_=setup(False);src=expected(s)['source'];run=run_identity(src);run[field]=value
    with pytest.raises(ValueError):release.verify_human_review(run,reviews(),src)


def test_valid_native_review_and_no_self_approval():
    _,s,_,_,_=setup(False);src=expected(s)['source']
    release.verify_human_review(run_identity(src),reviews(),src)
    r=reviews();r[0]['user']['login']='initiator'
    with pytest.raises(ValueError):release.verify_human_review(run_identity(src),r,src)


@pytest.mark.parametrize('change',['no-reviewers','self-review','bypass','branch'])
def test_protection_configuration_fails_closed(monkeypatch,change):
    protection=dict(protection_rules=[dict(type='required_reviewers',reviewers=[{'type':'User'}],prevent_self_review=True)],
        can_admins_bypass=False,deployment_branch_policy=dict(protected_branches=False,custom_branch_policies=True))
    policies=dict(total_count=1,branch_policies=[dict(name='v*',type='tag')])
    if change=='no-reviewers':protection['protection_rules'][0]['reviewers']=[]
    if change=='self-review':protection['protection_rules'][0]['prevent_self_review']=False
    if change=='bypass':protection['can_admins_bypass']=True
    if change=='branch':policies['branch_policies'][0]['type']='branch'
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=[protection,policies]))
    with pytest.raises(ValueError):release.protections()


def test_feature_branch_and_retries_prohibited():
    _,s,_,_,_=setup(False);src=expected(s)['source']
    for field,value in [('ref','refs/heads/feat/canary-monitoring-demo'),('run_attempt','2')]:
        bad=copy.deepcopy(src);bad[field]=value
        with pytest.raises(ValueError):source(bad)


def test_auth_state_bounds_and_no_sensitive_extras():
    _,s,_,_,_=setup(False);ev=expected(s);ev['token']='forbidden'
    with pytest.raises(ValueError):authorize(s,ev,now=0)
    approve(s)
    assert len(json.dumps(s.read()).encode())<500000
    assert 'token' not in json.dumps(s.read()['pending'][0]['authorization'])


def test_writer_no_legacy_state_and_preserves_other_configmap_data():
    from control_plane.authorization_backend import AuthorizationBackend
    with pytest.raises(ValueError):AuthorizationBackend(dict(server='https://api',certificate_authority_data='',token='test',namespace='production',state_name='model-release-state'))
    b=object.__new__(AuthorizationBackend);b.writable=True;b.original=dict(metadata=dict(resourceVersion='7'),data={'other':'preserve','state.json':'old'})
    b.request=Mock();b.write_state('7',{'new':'state'})
    obj=b.request.call_args.args[1]
    assert obj['data']['other']=='preserve' and obj['metadata']['resourceVersion']=='7'
    b.writable=False
    with pytest.raises(ValueError):b.write_state('7',{})


def test_rbac_exact_v2_state_only():
    doc=yaml.safe_load(Path('k8s/release-v2/authorization-rbac.proposed.yaml').read_text())
    roles=[x for x in doc['items'] if x['kind']=='Role']
    assert len(roles)==2
    for role in roles:
        assert len(role['rules'])==1
        rule=role['rules'][0]
        assert rule['resources']==['configmaps'] and rule['resourceNames']==['model-release-state-v2']
        assert set(rule['verbs'])<= {'get','update'}


def test_failed_job_cannot_report_release_success(tmp_path,monkeypatch):
    monkeypatch.setattr(release,'OUT',tmp_path)
    monkeypatch.setenv('NEEDS_JSON',json.dumps({k:{'result':v} for k,v in [('validation','success'),('preflight','success'),('production','failure')]}))
    release.summarize();d=json.loads((tmp_path/'summary.json').read_text())
    assert d['outcome']=='AUTHORIZATION_FAILED' and not d['deployment_success']


def test_terminal_audit_is_identity_bound():
    s={'audit':[dict(reason='release_completed',authorization_id='other')]}
    assert release.terminal(s,'this')=='AUTHORIZED'
    s['audit'].append(dict(reason='rollback_completed',authorization_id='this'))
    assert release.terminal(s,'this')=='ROLLED_BACK'


def test_release_workflow_has_no_approval_or_serving_bypass():
    w=yaml.load(Path('.github/workflows/release.yml').read_text(),Loader=yaml.BaseLoader)
    assert w['name']=='Release' and w['concurrency']['cancel-in-progress']=='false'
    assert w['jobs']['production']['environment']['name']=='production'
    assert w['jobs']['production']['needs']==['validation','preflight']
    for job_name,job in w['jobs'].items():
        for step in job['steps']:
            if 'RELEASE_STATE_WRITER_B64' in str(step):assert job_name=='production'
            assert 'kubectl' not in step.get('run','')
            assert 'scripts/release.py deploy' not in step.get('run','')
            assert 'docker/build-push-action' not in step.get('uses','')
    preflight=json.dumps(w['jobs']['preflight'])
    assert 'RELEASE_STATE_READER_B64' not in preflight
    reader_role='arn:aws:iam::596432515999:role/ml-template-v2-k8s-reader'
    assert reader_role in preflight and 'RELEASE_CATALOG_READ_ROLE_ARN' in preflight
    assert preflight.index(reader_role) < preflight.index('RELEASE_CATALOG_READ_ROLE_ARN')
    assert 'control_plane.oidc_eks_reader' in preflight
    production=json.dumps(w['jobs']['production'])
    assert 'RELEASE_STATE_WRITER_B64' in production and reader_role not in production
    assert 'scripts.release_v2 source' in str(w['jobs']['validation'])
    assert 'scripts.release_v2 preflight' in str(w['jobs']['preflight'])
    assert 'scripts.release_v2 authorize' in str(w['jobs']['production'])


def runtime_fixture(monkeypatch,s):
    from test_runtime_ci import CONFIG
    state=s.read();item=state['pending'][0];request=item['build_request'];image=item['pair']['runtime_digest']
    scan={'ArtifactType':'container_image','Metadata':{'ImageID':CONFIG},'Results':[{'Class':'lang-pkgs','Type':'python-pkg','Target':'Python','Vulnerabilities':[]}]}
    run=dict(path='.github/workflows/runtime-build.yml',conclusion='success',status='completed',repository={'full_name':REPOSITORY},head_sha='a'*40,run_attempt=1)
    monkeypatch.setattr(release.legacy,'github_json',Mock(return_value=run));monkeypatch.setattr(release.legacy,'command',Mock(return_value=''))
    def files(run_id,name,names):
        return {'request.json':request} if name=='runtime-context' else {'trivy.json':scan,'security-decision.json':dict(decision='PASS',fingerprint=request['fingerprint'],image_id=CONFIG)}
    monkeypatch.setattr(release,'artifact_files',files)
    c=Mock();c.lookup.return_value=image
    c.ecr.batch_get_image.return_value={'images':[{'imageId':{'imageDigest':image.split('@')[1]},'imageManifest':json.dumps({'config':{'digest':CONFIG}})}]}
    return state,c,scan,run


def test_reuse_preflight_consumes_security_evidence_without_build(monkeypatch):
    _,s,_,_,_=setup(False);state,c,_,_=runtime_fixture(monkeypatch,s)
    proof,decision=release.verify_runtime(state,'3','500',c)
    assert DIGEST.fullmatch(proof) and decision['decision']=='PASS'
    c.publish.assert_not_called();c.ecr.put_image.assert_not_called()


@pytest.mark.parametrize('case',['miss','digest','blocked-scan','failed-build','contract','missing-evidence'])
def test_preflight_rejects_missing_or_ineligible_runtime(monkeypatch,case):
    _,s,_,_,_=setup(False);state,c,scan,run=runtime_fixture(monkeypatch,s)
    if case=='miss':c.lookup.return_value=None
    if case=='digest':c.ecr.batch_get_image.return_value['images'][0]['imageManifest']=json.dumps({'config':{'digest':'wrong'}})
    if case=='blocked-scan':scan['Results'][0]['Vulnerabilities']=[dict(VulnerabilityID='CVE-test',PkgName='mlflow',InstalledVersion='3.14.0',FixedVersion='3.15.0',Severity='CRITICAL')]
    if case=='failed-build':run['conclusion']='failure'
    if case=='contract':state['pending'][0]['build_request']['fingerprint']='sha256:'+'0'*64
    if case=='missing-evidence':monkeypatch.setattr(release,'artifact_files',Mock(side_effect=ValueError('missing')))
    with pytest.raises(ValueError):release.verify_runtime(state,'3','500',c)
    c.publish.assert_not_called()


def test_real_v9_like_build_required_not_eligible():
    from control_plane.processor_v2 import Processor
    from test_processor_v2 import CONTRACT
    _,s,_,_,c=setup(False);enqueue(s,event(9))
    src=Mock();src.inspect.return_value=CONTRACT;cat=Mock();cat.lookup.return_value=None
    Processor(s,src,cat,clock=lambda:0).tick()
    assert s.read()['pending'][-1]['status']=='BUILD_REQUIRED'
    with pytest.raises(ValueError):snapshot(s.read(),'9')
    c.tick();assert s.read()['active'] is None and s.read()['stable']==OLD


def test_rejected_audit_requires_native_evidence(tmp_path,monkeypatch):
    monkeypatch.setattr(release,'OUT',tmp_path);monkeypatch.setenv('GITHUB_RUN_ID','103')
    monkeypatch.setenv('NEEDS_JSON',json.dumps({k:{'result':v} for k,v in [('validation','success'),('preflight','success'),('production','failure')]}))
    monkeypatch.setattr(release.legacy,'github_json',Mock(return_value=reviews('rejected')))
    release.summarize()
    assert json.loads((tmp_path/'summary.json').read_text())['outcome']=='APPROVAL_REJECTED'


def test_protection_api_unavailable_blocks(monkeypatch):
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=OSError('unavailable')))
    with pytest.raises(OSError):release.protections()


def test_authorization_committed_then_observation_failed_is_not_deployment_success(tmp_path,monkeypatch):
    _,s,_,_,_=setup(False);ev=expected(s);auth=authorize(s,ev,now=0)
    (tmp_path/'candidate.json').write_text(json.dumps(ev))
    (tmp_path/'authorization.json').write_text(json.dumps(dict(outcome='AUTHORIZED',authorization=auth)))
    monkeypatch.setattr(release,'OUT',tmp_path)
    monkeypatch.setenv('GITHUB_RUN_ID','103');monkeypatch.setenv('GITHUB_SHA','a'*40)
    monkeypatch.setenv('NEEDS_JSON',json.dumps({k:{'result':v} for k,v in [('validation','success'),('preflight','success'),('production','failure')]}))
    monkeypatch.setattr(release.legacy,'github_json',Mock(return_value=reviews()))
    release.summarize();result=json.loads((tmp_path/'summary.json').read_text())
    assert result['outcome']=='AUTHORIZED' and result['deployment_success'] is False


# Review blockers: these three tests must fail against the previous staged patch.
def test_documented_environment_response_without_optional_fields(monkeypatch):
    protection=dict(id=161088068,name='production',protection_rules=[dict(id=3755,type='required_reviewers',
        prevent_self_review=True,reviewers=[dict(type='User',reviewer=dict(id=1,login='reviewer'))])],
        deployment_branch_policy=dict(protected_branches=False,custom_branch_policies=True))
    policies=dict(total_count=1,branch_policies=[dict(id=361471,node_id='policy-node',name='v*')])
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=[protection,policies]))
    result=release.protections()
    assert result['admin_bypass']=='REQUIRES_SETTINGS_VERIFICATION'


def test_expired_unclaimed_can_receive_fresh_authorization():
    _,s,_,_,_=setup(False);first=authorize(s,expected(s),now=0)
    src=expected(s)['source'];src['run_id']='104'
    fresh=evidence(s.read(),'3','TICKET-124',src,'sha256:'+'d'*64,900)
    second=authorize(s,fresh,now=901)
    assert second['expires_at']==1801 and second['evidence_hash']!=first['evidence_hash']
    assert s.read()['audit'][-1]['reason']=='release_reauthorized'


def test_retained_provenance_selection_does_not_require_build_run(monkeypatch):
    monkeypatch.setenv('RUNTIME_EVIDENCE_SOURCE','retained_provenance')
    monkeypatch.setenv('RUNTIME_EVIDENCE_RUN_ID','')
    monkeypatch.setenv('RUNTIME_PROVENANCE_DIGEST','sha256:'+'e'*64)
    assert release.evidence_selection()==('retained_provenance','sha256:'+'e'*64)


# All approvals/records below are synthetic unit fixtures, never live evidence.
def retained_fixture(monkeypatch,s):
    state,cat,scan,_=runtime_fixture(monkeypatch,s)
    item=state['pending'][0]
    record=dict(schema_version=1,request=item['build_request'],image=item['pair']['runtime_digest'],scan=scan,
        security_decision=dict(decision='PASS',fingerprint=item['fingerprint'],image_id=scan['Metadata']['ImageID']),
        build_evidence='sha256:'+'b'*64,smoke_evidence='sha256:'+'c'*64)
    approval=dict(record_digest=digest(record),authority='test-security-reviewer',approval_reference='TEST-REVIEW-1',
        approved_at='2026-01-01T00:00:00Z',expires_at='2027-01-01T00:00:00Z',
        risk_acceptance_expires_at='2026-12-01T00:00:00Z',status='approved')
    index=dict(schema_version=1,records=[approval])
    monkeypatch.setattr(release,'provenance_file',lambda path,limit:index if path.name=='approvals.json' else record)
    monkeypatch.setattr(release.time,'time',lambda:1791504000)  # 2026-10-09 UTC
    return state,cat,record,approval,index


V11_IMAGE='596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:f1ae0d6c95d19b73adbfa72ae183ef6f207a2dc647bc2d4a1a36386a02ba5ae0'
V11_FP='sha256:700096f6d55f8aa9b56c9049e1c4171633f403a8d3e2a2de2df1ff544eab63fc'
V11_RELEASE='E2E-V11-2026-10'


def v11_retained_exception_fixture(monkeypatch,version='11'):
    _,s,_,_,_=setup(False)
    request=dict(schema_version=1,fingerprint=V11_FP,python_version='3.12.14',
        requirements=['mlflow==3.14.0','numpy==2.5.3','pandas==2.3.3','scikit-learn==1.7.2',
                      'scipy==1.18.1','skops==0.16.0'],
        runtime_profile=dict(mlserver_version='1.7.1',mlserver_mlflow_version='1.7.1',
            platform='linux/amd64',build_profile='runtime-x-hardened-v1',
            serving_profile='mlserver-v2-parallel-workers-0'))
    def candidate(state):
        item=state['pending'][0];artifact=event(int(version))['artifact_uri']
        item['event'].update(model_version=version,artifact_uri=artifact,run_id=f'run-{version}')
        item.update(fingerprint=V11_FP,build_request=request,
            pair=dict(model_version=version,runtime_digest=V11_IMAGE,storage_uri=artifact))
        state['highest_accepted']=int(version)
        state['identities']=[dict(model_name='credit-risk-classifier',model_version=version,
                                  artifact_uri=artifact,run_id=f'run-{version}')]
    s.update(candidate)
    state,cat,scan,_=runtime_fixture(monkeypatch,s)
    scan['Results'][0]['Vulnerabilities']=[dict(VulnerabilityID='CVE-2026-64849',PkgName='mlflow',
        InstalledVersion='3.14.0',FixedVersion='3.15.0',Severity='CRITICAL')]
    item=state['pending'][0]
    record=dict(schema_version=1,request=item['build_request'],image=item['pair']['runtime_digest'],scan=scan,
        security_decision=dict(decision='BLOCK',fingerprint=item['fingerprint'],image_id=scan['Metadata']['ImageID']),
        build_evidence='sha256:'+'b'*64,smoke_evidence='sha256:'+'c'*64)
    approval=dict(record_digest=digest(record),authority='security-reviewer',approval_reference='SEC-E2E-11',
        approved_at='2026-10-01T00:00:00Z',expires_at='2026-10-31T00:00:00Z',
        risk_acceptance_expires_at='2026-10-20T00:00:00Z',status='approved',
        e2e_exception=dict(kind='E2E_ONLY_SECURITY_EXCEPTION',model_version='11',runtime_digest=V11_IMAGE,
            fingerprint=V11_FP,release_reference=V11_RELEASE,status='approved'))
    index=dict(schema_version=1,records=[approval])
    monkeypatch.setattr(release,'provenance_file',lambda path,limit:index if path.name=='approvals.json' else record)
    monkeypatch.setattr(release.time,'time',lambda:1792137600)  # 2026-10-16T00:00:00Z
    monkeypatch.setenv('TICKET_REFERENCE',V11_RELEASE)
    cat.lookup.return_value=V11_IMAGE
    cat.ecr.batch_get_image.return_value={'images':[{'imageId':{'imageDigest':V11_IMAGE.split('@')[1]},
        'imageManifest':json.dumps({'config':{'digest':scan['Metadata']['ImageID']}})}]}
    return s,state,cat,record,approval,index


def test_v11_exact_retained_block_can_use_bounded_e2e_exception(monkeypatch):
    s,state,cat,record,approval,_=v11_retained_exception_fixture(monkeypatch)
    proof,decision=release.verify_runtime(state,'11',approval['record_digest'],cat,
                                          evidence_source='retained_provenance')
    assert DIGEST.fullmatch(proof)
    assert decision['decision']=='BLOCK' and decision['security_pass'] is False
    assert decision['enforcement']=='E2E_ONLY_SECURITY_EXCEPTION'
    assert decision['counts']['raw']['CRITICAL']==1 and len(decision['blocking_findings'])==1
    assert decision['security_exception']['model_version']=='11'
    assert decision['security_exception']['approval_reference']=='SEC-E2E-11'
    assert decision['security_exception']['risk_acceptance_expires_at']=='2026-10-20T00:00:00Z'
    rechecked,_=release.verify_runtime(state,'11',approval['record_digest'],cat,
                                        evidence_source='retained_provenance')
    assert rechecked==proof  # Preflight/Authorize recheck binds the same stable evidence.
    assert s.read()['active'] is None and 'authorization' not in s.read()['pending'][0]


@pytest.mark.parametrize('field,value',[('runtime_digest',V11_IMAGE.replace('f1ae0','f1ae1')),
                                         ('fingerprint','sha256:'+'f'*64)])
def test_v11_e2e_exception_wrong_runtime_identity_blocks(monkeypatch,field,value):
    _,state,cat,_,approval,_=v11_retained_exception_fixture(monkeypatch)
    approval['e2e_exception'][field]=value
    with pytest.raises(ValueError,match='scope mismatch'):
        release.verify_runtime(state,'11',approval['record_digest'],cat,evidence_source='retained_provenance')


def test_v12_cannot_reuse_v11_e2e_exception(monkeypatch):
    _,state,cat,_,approval,_=v11_retained_exception_fixture(monkeypatch,version='12')
    with pytest.raises(ValueError,match='limited to exact model v11 runtime'):
        release.verify_runtime(state,'12',approval['record_digest'],cat,evidence_source='retained_provenance')


@pytest.mark.parametrize('case',['expired','not-approved'])
def test_v11_e2e_exception_expired_or_unapproved_blocks(monkeypatch,case):
    _,state,cat,_,approval,_=v11_retained_exception_fixture(monkeypatch)
    if case=='expired':approval['risk_acceptance_expires_at']='2026-10-15T00:00:00Z'
    else:approval['e2e_exception']['status']='pending'
    with pytest.raises(ValueError):
        release.verify_runtime(state,'11',approval['record_digest'],cat,evidence_source='retained_provenance')


def test_retained_reuse_valid_contract_scan_authority_no_build(monkeypatch):
    _,s,_,_,_=setup(False);state,cat,record,approval,_=retained_fixture(monkeypatch,s)
    release.legacy.command.reset_mock();release.legacy.github_json.reset_mock()
    proof,decision=release.verify_runtime(state,'3',approval['record_digest'],cat,evidence_source='retained_provenance')
    assert DIGEST.fullmatch(proof) and decision['decision']=='PASS'
    release.legacy.command.assert_not_called();release.legacy.github_json.assert_not_called()
    cat.publish.assert_not_called();cat.ecr.put_image.assert_not_called()
    assert s.read()['active'] is None and s.read()['stable']==OLD


@pytest.mark.parametrize('case',['missing','revoked','expired','risk-expired','future','duplicate','authority',
    'tampered','fingerprint','digest','contract','miss','blocked-scan','old-block','smoke','registry'])
def test_retained_provenance_fails_closed(monkeypatch,case):
    _,s,_,_,_=setup(False);state,cat,record,approval,index=retained_fixture(monkeypatch,s)
    if case=='missing':index['records']=[]
    if case=='revoked':approval['status']='revoked'
    if case=='expired':approval['expires_at']='2026-01-02T00:00:00Z'
    if case=='risk-expired':approval['risk_acceptance_expires_at']='2026-01-02T00:00:00Z'
    if case=='future':approval['approved_at']='2027-01-01T00:00:00Z'
    if case=='duplicate':index['records'].append(copy.deepcopy(approval))
    if case=='authority':approval['authority']=''
    if case=='tampered':record['build_evidence']='sha256:'+'f'*64
    if case=='fingerprint':record['request']=dict(record['request'],fingerprint='sha256:'+'f'*64)
    if case=='digest':record['image']=record['image'].replace('@sha256:','@sha256:f')
    if case=='contract':record['request']=dict(record['request'],python_version='3.12.15')
    if case=='miss':cat.lookup.return_value=None
    if case=='blocked-scan':record['scan']['Results'][0]['Vulnerabilities']=[dict(VulnerabilityID='CVE-test',PkgName='mlflow',InstalledVersion='3.14.0',FixedVersion='3.15.0',Severity='CRITICAL')]
    if case=='old-block':record['security_decision']['decision']='BLOCK'
    if case=='smoke':record['smoke_evidence']=''
    if case=='registry':cat.ecr.batch_get_image.return_value['images'][0]['imageManifest']=json.dumps({'config':{'digest':'wrong'}})
    if case!='tampered':approval['record_digest']=digest(record)  # Even an approved record cannot bypass current policy/identity.
    with pytest.raises(ValueError):release.verify_runtime(state,'3',approval['record_digest'],cat,evidence_source='retained_provenance')
    assert s.read()['active'] is None and s.read()['stable']==OLD
    cat.publish.assert_not_called()


@pytest.mark.parametrize('kind,run,record',[
    ('workflow_run','',''),('retained_provenance','',''),('unknown','500',''),
    ('workflow_run','500','sha256:'+'e'*64),('retained_provenance','500','sha256:'+'e'*64),
    ('retained_provenance','','../../untrusted.json')])
def test_evidence_selection_never_falls_back(monkeypatch,kind,run,record):
    monkeypatch.setenv('RUNTIME_EVIDENCE_SOURCE',kind);monkeypatch.setenv('RUNTIME_EVIDENCE_RUN_ID',run)
    monkeypatch.setenv('RUNTIME_PROVENANCE_DIGEST',record)
    with pytest.raises(ValueError):release.evidence_selection()


def test_expired_ci_artifact_requires_retained_evidence_not_rebuild(monkeypatch):
    monkeypatch.setattr(release.legacy,'github_json',Mock(return_value={'total_count':1,'artifacts':[{'name':'runtime-context','expired':True}]}))
    with pytest.raises(ValueError,match='absent'):release.artifact_files('500','runtime-context',['request.json'])


@pytest.mark.parametrize('unsafe',['root-runner','writable','symlink','owner'])
def test_provenance_authority_files_cannot_be_job_supplied(monkeypatch,unsafe):
    import stat
    from types import SimpleNamespace
    monkeypatch.setattr(release.os,'name','posix')
    monkeypatch.setattr(release.os,'geteuid',lambda:0 if unsafe=='root-runner' else 1000,raising=False)
    class File:
        parents=()
        def lstat(self):
            return SimpleNamespace(st_uid=1000 if unsafe=='owner' else 0,
                st_mode=stat.S_IFLNK if unsafe=='symlink' else stat.S_IFREG | (0o666 if unsafe=='writable' else 0o644))
    with pytest.raises(ValueError):release.provenance_file(File(),100)


def fresh_reapproval(s,run='104',now=900):
    src=expected(s)['source'];src['run_id']=run
    return evidence(s.read(),'3','TEST-REAPPROVAL',src,'sha256:'+'d'*64,now)


def test_same_old_run_cannot_refresh_even_with_new_evidence():
    _,s,_,_,_=setup(False);old=authorize(s,expected(s),now=0)
    with pytest.raises(ValueError,match='fresh_release_run'):authorize(s,fresh_reapproval(s,run='103'),now=900)
    assert s.read()['pending'][0]['authorization']==old


def test_old_evidence_cannot_replay_after_reauthorization():
    _,s,_,_,_=setup(False);old=expected(s);authorize(s,old,now=0)
    new=authorize(s,fresh_reapproval(s),now=901)
    with pytest.raises(ValueError):authorize(s,old,now=1801)
    assert s.read()['pending'][0]['authorization']==new


@pytest.mark.parametrize('case',['stable','fingerprint','new-model','claim','other-run'])
def test_reauthorization_cas_races_preserve_winner(monkeypatch,case):
    b,s,_,clock,c=setup(False);authorize(s,expected(s),now=0);fresh=fresh_reapproval(s)
    write=b.write_state;once=[False]
    def conflict(rv,state):
        if not once[0]:
            once[0]=True
            if case=='stable':s.update(lambda x:x['stable'].update(storage_uri=event(2)['artifact_uri']))
            if case=='fingerprint':s.update(lambda x:x.update(stable_fingerprint='sha256:'+'f'*64))
            if case=='new-model':enqueue(s,event(4))
            if case=='claim':clock[0]=899;c.tick()  # Winning Controller snapshot just before expiry.
            if case=='other-run':authorize(s,fresh_reapproval(s,run='105'),now=901)
            raise Conflict()
        return write(rv,state)
    b.write_state=conflict
    with pytest.raises(ValueError):authorize(s,fresh,now=901)
    state=s.read()
    if case=='claim':assert state['active']['phase']=='PREPARING' and state['pending']==[]
    if case=='other-run':assert state['pending'][0]['authorization']['evidence']['source']['run_id']=='105'
    assert state['route']['weight']==0


def test_changed_baseline_before_fresh_preflight_still_rejects():
    _,s,_,_,_=setup(False);authorize(s,expected(s),now=0)
    s.update(lambda x:x.update(stable_fingerprint='sha256:'+'f'*64))
    with pytest.raises(ValueError,match='original_baseline'):authorize(s,fresh_reapproval(s),now=901)


def test_active_release_survives_claim_expiration():
    from test_controller_v2 import until
    _,s,_,clock,c=setup();clock[0]=899;until(c,s,'FINALIZING')
    clock[0]=901;c.tick();c.tick()
    assert s.read()['stable']['model_version']=='3' and s.read()['active'] is None


def test_reauthorization_is_bounded_and_idempotent():
    _,s,_,_,_=setup(False);authorize(s,expected(s),now=0)
    for n in range(105):
        now=(n+1)*900
        ev=fresh_reapproval(s,run=str(104+n),now=now)
        auth=authorize(s,ev,now=now)
        assert authorize(s,ev,now=now+1)==auth
    state=s.read();assert len(state['audit'])==100 and len(json.dumps(state).encode())<500000
    row=state['audit'][-1]
    assert row['reason']=='release_reauthorized' and row['previous_authorization_id']!=row['authorization_id']
    assert state['stable']==OLD and state['route']['weight']==0


@pytest.mark.parametrize('case',['no-rule','empty','self-disabled','unrestricted'])
def test_documented_environment_negative_cases(monkeypatch,case):
    protection=dict(protection_rules=[dict(type='required_reviewers',prevent_self_review=True,
        reviewers=[dict(type='User',reviewer=dict(id=1,login='reviewer'))])],
        deployment_branch_policy=dict(protected_branches=False,custom_branch_policies=True))
    if case=='no-rule':protection['protection_rules']=[]
    if case=='empty':protection['protection_rules'][0]['reviewers']=[]
    if case=='self-disabled':protection['protection_rules'][0]['prevent_self_review']=False
    if case=='unrestricted':protection['deployment_branch_policy']=None
    policies=dict(total_count=1,branch_policies=[dict(id=1,name='v*')])
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=[protection,policies]))
    with pytest.raises(ValueError):release.protections()


@pytest.mark.parametrize('error',[PermissionError('403'),FileNotFoundError('404'),TimeoutError('timeout')])
def test_environment_read_errors_block(monkeypatch,error):
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=error))
    with pytest.raises(type(error)):release.protections()


@pytest.mark.parametrize('case',['bypass-no-review','wrong-environment','self','rejected','canceled','wrong-run','wrong-repo'])
def test_actual_writer_native_review_failure_never_mutates(tmp_path,monkeypatch,case):
    _,s,_,_,_=setup(False);ev=expected(s);(tmp_path/'candidate.json').write_text(json.dumps(ev))
    monkeypatch.setattr(release,'OUT',tmp_path);monkeypatch.setattr(release,'release_source',lambda:ev['source'])
    monkeypatch.setattr(release,'protections',lambda:None);monkeypatch.setattr(release,'state_store',lambda **kwargs:s)
    monkeypatch.setattr(release,'evidence_selection',lambda:('workflow_run','500'))
    monkeypatch.setattr(release,'catalog',lambda:None);monkeypatch.setattr(release,'verify_runtime',lambda *a,**kw:(ev['security_hash'],{}))
    monkeypatch.setenv('MODEL_VERSION','3');monkeypatch.setenv('TICKET_REFERENCE',ev['ticket_reference'])
    run=run_identity(ev['source']);review=reviews()
    if case=='bypass-no-review':review=[]
    if case=='wrong-environment':review[0]['environments']=[{'id':2,'name':'runtime-build-publish'}]
    if case=='self':review[0]['user']['login']='initiator'
    if case=='rejected':review=reviews('rejected')
    if case=='canceled':run.update(status='completed',conclusion='cancelled')
    if case=='wrong-run':run['id']=999
    if case=='wrong-repo':run['repository']['full_name']='another/repo'
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=[run,review]))
    before=s.read()
    with pytest.raises(ValueError):release.authorize_release()
    assert s.read()==before


@pytest.mark.parametrize('fresh_run',[False,True])
def test_writer_reapproval_requires_new_run_created_after_expiration(tmp_path,monkeypatch,fresh_run):
    _,s,_,_,_=setup(False);authorize(s,expected(s),now=0);ev=fresh_reapproval(s)
    (tmp_path/'candidate.json').write_text(json.dumps(ev));monkeypatch.setattr(release,'OUT',tmp_path)
    monkeypatch.setattr(release,'release_source',lambda:ev['source']);monkeypatch.setattr(release,'protections',lambda:None)
    monkeypatch.setattr(release,'state_store',lambda **kwargs:s);monkeypatch.setattr(release,'catalog',lambda:None)
    monkeypatch.setattr(release,'evidence_selection',lambda:('workflow_run','500'))
    monkeypatch.setattr(release,'verify_runtime',lambda *a,**kw:(ev['security_hash'],{}))
    monkeypatch.setenv('MODEL_VERSION','3');monkeypatch.setenv('TICKET_REFERENCE',ev['ticket_reference'])
    monkeypatch.setattr(release.time,'time',lambda:901)
    run=run_identity(ev['source']);run['created_at']='1970-01-01T00:15:00Z' if fresh_run else '1970-01-01T00:00:00Z'
    monkeypatch.setattr(release.legacy,'github_json',Mock(side_effect=[run,reviews()]))
    if fresh_run:
        release.authorize_release();assert s.read()['audit'][-1]['reason']=='release_reauthorized'
    else:
        with pytest.raises(ValueError,match='new run created'):release.authorize_release()
        assert s.read()['pending'][0]['authorization']['authorized_at']==0



def test_generic_ci_profile_complete_evidence_passes(monkeypatch):
    from control_plane.build_contract import build_request,validate_request
    from control_plane.runtime_resolver import RUNTIME_CI_V2
    _,s,_,_,_=setup(False)
    contract,_=validate_request(s.read()['pending'][0]['build_request'])
    request=build_request(contract,RUNTIME_CI_V2)
    s.update(lambda state:state['pending'][0].update(build_request=request,fingerprint=request['fingerprint']))
    state,cat,_,_=runtime_fixture(monkeypatch,s)
    proof,decision=release.verify_runtime(state,'3','500',cat)
    assert DIGEST.fullmatch(proof) and decision['decision']=='PASS'
    cat.publish.assert_not_called()


def test_absent_candidate_evidence_classified_not_eligible():
    _,s,_,_,_=setup(False)
    with pytest.raises(ValueError,match='stale_release_candidate'):
        evidence(s.read(),'999','TEST-1',expected(s)['source'],'sha256:'+'e'*64,0)
