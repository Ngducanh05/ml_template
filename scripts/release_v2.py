"""V2 stages of the original Release workflow; no serving or traffic mutations."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import stat
import zipfile
from urllib.request import Request, build_opener, HTTPRedirectHandler, urlopen
from urllib.error import HTTPError
from . import release as legacy
from .runtime_ci import require_clean_scan, verify_registry_image
from control_plane.authorization_v2 import (source, ticket, snapshot, evidence, digest, authorize, REPOSITORY, WORKFLOW)
from control_plane.authorization_backend import AuthorizationBackend
from control_plane.state_v2 import Store
from control_plane.runtime_catalog import Catalog
from control_plane.build_contract import validate_request

OUT=Path('evidence')


def write(name,data):
    OUT.mkdir(exist_ok=True)
    (OUT/name).write_text(json.dumps(data,sort_keys=True,indent=2)+'\n',encoding='utf-8')


def release_source():
    value=dict(repository=legacy.env('GITHUB_REPOSITORY'),workflow=WORKFLOW,
        run_id=legacy.env('GITHUB_RUN_ID'),run_attempt=legacy.env('GITHUB_RUN_ATTEMPT'),
        sha=legacy.env('GITHUB_SHA'),ref=legacy.env('GITHUB_REF'))
    source(value);ticket(legacy.env('TICKET_REFERENCE'))
    legacy.require(legacy.env('GITHUB_EVENT_NAME')=='workflow_dispatch','Disallowed release trigger')
    legacy.require(legacy.env('GITHUB_WORKFLOW_REF')==REPOSITORY+'/'+WORKFLOW+'@'+value['ref'],'Unexpected workflow ref')
    legacy.require(legacy.command('git','rev-parse','HEAD')==value['sha'],'Checkout/source SHA mismatch')
    legacy.command('git','fetch','--no-tags','origin','main')
    legacy.command('git','merge-base','--is-ancestor',value['sha'],'origin/main')
    rows=legacy.command('git','ls-remote','origin',value['ref'],value['ref']+'^{}').splitlines()
    tags=dict(line.split()[::-1] for line in rows)
    legacy.require(tags.get(value['ref']+'^{}',tags.get(value['ref']))==value['sha'],'Release tag moved or missing')
    return value


def protections():
    protection=legacy.github_json('environments/production')
    policies=legacy.github_json('environments/production/deployment-branch-policies')
    legacy.verify_protection(protection,policies)
    # These optional fields are not guaranteed by documented GET responses.
    if 'can_admins_bypass' in protection:
        legacy.require(protection['can_admins_bypass'] is False,'Admin bypass must be disabled')
    if 'type' in policies['branch_policies'][0]:
        legacy.require(policies['branch_policies'][0]['type']=='tag','Only release tags may deploy')
    return dict(admin_bypass='REQUIRES_SETTINGS_VERIFICATION',
                policy_type='REQUIRES_SETTINGS_VERIFICATION', native_review='REQUIRED')


def verify_human_review(run,reviews,expected):
    source(expected)
    legacy.require(str(run.get('id'))==expected['run_id'] and run.get('head_sha')==expected['sha']
        and run.get('head_branch')==expected['ref'].removeprefix('refs/tags/')
        and run.get('path')==WORKFLOW and run.get('event')=='workflow_dispatch'
        and run.get('run_attempt')==1 and run.get('status')=='in_progress'
        and run.get('conclusion') is None and run.get('repository',{}).get('full_name')==REPOSITORY,
        'Release run is stale, canceled or mismatched')
    selected=[r for r in reviews if any(e.get('name')=='production' for e in r.get('environments',[]))]
    legacy.require(bool(selected) and all(r.get('state')=='approved' for r in selected), 'Native production review missing or rejected')
    actor=run.get('actor',{}).get('login')
    legacy.require(bool(actor) and all(r.get('user',{}).get('login') and r['user']['login']!=actor for r in selected), 'Independent human review required')


def state_store(writable=False):
    key='RELEASE_STATE_WRITER_B64' if writable else 'RELEASE_STATE_READER_B64'
    config=json.loads(base64.b64decode(legacy.env(key),validate=True))
    return Store(AuthorizationBackend(config,writable=writable))


def catalog():
    import boto3
    # OIDC-only scoped ECR reader; never IAM/ECR writes.
    legacy.require(bool(os.getenv('AWS_SESSION_TOKEN')), 'Temporary catalog reader credentials required')
    return Catalog(boto3.client('ecr',region_name='ap-southeast-1'))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def artifact_files(run_id,name,filenames):
    rows=legacy.github_json(f'actions/runs/{run_id}/artifacts?per_page=100')
    legacy.require(rows.get('total_count',0)<=100,'Artifact list exceeds bound')
    hits=[r for r in rows.get('artifacts',[]) if r['name']==name and not r['expired']]
    legacy.require(len(hits)==1,'Required runtime evidence absent or ambiguous')
    obj=hits[0]
    legacy.require(obj['size_in_bytes']<=32*1024*1024,'Evidence archive too large')
    req=Request(f'https://api.github.com/repos/{REPOSITORY}/actions/artifacts/{obj["id"]}/zip',
        headers={'Authorization':'Bearer '+legacy.env('GH_TOKEN'),'Accept':'application/vnd.github+json'})
    try:
        with build_opener(NoRedirect).open(req,timeout=30) as response:raw=response.read(32*1024*1024+1)
    except HTTPError as exc:
        legacy.require(exc.code in (301,302,303,307,308),'Evidence download failed')
        location=exc.headers.get('Location','')
        legacy.require(location.startswith('https://'),'Evidence redirect requires TLS')
        # Never forward GitHub authorization to the artifact storage host.
        with urlopen(location,timeout=30) as response:raw=response.read(32*1024*1024+1)
    legacy.require(len(raw)<=32*1024*1024 and obj.get('digest')=='sha256:'+hashlib.sha256(raw).hexdigest(),'Evidence archive identity mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        result={}
        for name in filenames:
            matches=[i for i in archive.infolist() if i.filename==name]
            legacy.require(len(matches)==1 and matches[0].file_size<=16*1024*1024,'Invalid evidence member')
            result[name]=json.loads(archive.read(matches[0]))
        return result



# Operator-provisioned, read-only authority store, OUTSIDE checkout/artifact inputs.
# No records are installed by this implementation. See the runbook trust boundary.
PROVENANCE_ROOT=Path('/etc/mlops/release/runtime-provenance')


def provenance_file(path,limit):
    legacy.require(os.name=='posix' and os.geteuid()!=0,'Provenance requires a non-root isolated Linux runner')
    for part in (path,*path.parents):
        info=part.lstat()
        legacy.require(info.st_uid==0 and not info.st_mode & 0o022 and not stat.S_ISLNK(info.st_mode),
                       'Provenance authority path must be root-owned and not group/world writable')
    legacy.require(stat.S_ISREG(path.stat().st_mode) and path.stat().st_size<=limit,'Invalid provenance file')
    from control_plane.runtime_security import load_json
    return load_json(path)


def retained_provenance(record_digest,now):
    from control_plane.state_v2 import DIGEST
    from control_plane.runtime_security import _time
    legacy.require(isinstance(record_digest,str) and DIGEST.fullmatch(record_digest),'Invalid provenance digest')
    index=provenance_file(PROVENANCE_ROOT/'approvals.json',65536)
    legacy.require(set(index)=={'schema_version','records'} and index['schema_version']==1
        and isinstance(index['records'],list) and len(index['records'])<=100,'Invalid provenance authority index')
    hits=[r for r in index['records'] if r.get('record_digest')==record_digest]
    legacy.require(len(hits)==1,'Runtime provenance absent or ambiguous')
    approval=hits[0]
    legacy.require(set(approval)=={'record_digest','authority','approval_reference','approved_at','expires_at',
                                  'risk_acceptance_expires_at','status'},'Invalid provenance approval')
    legacy.require(approval['status']=='approved','Runtime provenance revoked or unapproved')
    ticket(approval['authority']);ticket(approval['approval_reference'])
    legacy.require(_time(approval['approved_at']).timestamp()<=now<_time(approval['expires_at']).timestamp(),
                   'Runtime provenance not yet valid or expired')
    if approval['risk_acceptance_expires_at'] is not None:
        legacy.require(now<_time(approval['risk_acceptance_expires_at']).timestamp(),'Runtime risk acceptance expired')
    record=provenance_file(PROVENANCE_ROOT/(record_digest[7:]+'.json'),16*1024*1024)
    legacy.require(digest(record)==record_digest,'Runtime provenance content mismatch')
    return record,approval


def evidence_selection():
    kind=legacy.env('RUNTIME_EVIDENCE_SOURCE')
    run=os.getenv('RUNTIME_EVIDENCE_RUN_ID','');record=os.getenv('RUNTIME_PROVENANCE_DIGEST','')
    legacy.require(kind in ('workflow_run','retained_provenance'),'Select an explicit runtime evidence source')
    if kind=='workflow_run':
        legacy.require(bool(re.fullmatch(r'[1-9][0-9]{0,19}',run)) and not record,'Workflow evidence requires only a run ID')
        return kind,run
    legacy.require(not run and bool(re.fullmatch(r'sha256:[0-9a-f]{64}',record)),'Retained evidence requires only a provenance digest')
    return kind,record


def verify_retained_runtime(request,image,record_digest,cat):
    record,approval=retained_provenance(record_digest,time.time())
    legacy.require(set(record)=={'schema_version','request','image','scan','security_decision','build_evidence','smoke_evidence'}
        and record['schema_version']==1,'Invalid retained runtime record')
    legacy.require(record['request']==request and record['image']==image,'Retained runtime contract/digest mismatch')
    for key in ('build_evidence','smoke_evidence'):
        legacy.require(isinstance(record[key],str) and re.fullmatch(r'sha256:[0-9a-f]{64}',record[key]),'Missing reviewed build/smoke evidence')
    scan=record['scan'];old=record['security_decision']
    legacy.require(old.get('decision')=='PASS' and old.get('fingerprint')==request['fingerprint']
        and old.get('image_id')==scan.get('Metadata',{}).get('ImageID'),'Retained security decision blocked/mismatched')
    # Retention is not a security exception. Existing policy/waivers still apply now.
    decision=require_clean_scan(scan,request)
    legacy.require(cat.lookup(request['fingerprint'])==image,'Runtime catalog MISS/mismatch')
    verify_registry_image(image,scan,cat)
    return digest(dict(record_digest=record_digest,approval=approval)),decision


def verify_runtime(state,version,run_id,cat,*,evidence_source='workflow_run'):
    current=snapshot(state,version)
    item=next(x for x in state['pending'] if x['event']['model_version']==version)
    request=item['build_request'];validate_request(request)
    legacy.require(request['fingerprint']==current['candidate']['fingerprint'],'Candidate technical request mismatch')
    if evidence_source=='retained_provenance':
        return verify_retained_runtime(request,current['candidate']['pair']['runtime_digest'],run_id,cat)
    legacy.require(evidence_source=='workflow_run','Unknown runtime evidence source')
    legacy.require(re.fullmatch(r'[1-9][0-9]{0,19}',run_id),'Invalid runtime workflow run')
    run=legacy.github_json(f'actions/runs/{run_id}')
    legacy.require(run.get('path')=='.github/workflows/runtime-build.yml' and run.get('conclusion')=='success'
        and run.get('status')=='completed' and run.get('repository',{}).get('full_name')==REPOSITORY,
        'Runtime build/test/security workflow not successful')
    sha=run.get('head_sha','');legacy.require(re.fullmatch(r'[0-9a-f]{40}',sha),'Invalid runtime source')
    legacy.command('git','merge-base','--is-ancestor',sha,'origin/main')
    technical=artifact_files(run_id,'runtime-context',['request.json'])['request.json']
    legacy.require(technical==request,'Runtime build contract mismatch')
    reports=artifact_files(run_id,f'runtime-security-{run_id}-{run["run_attempt"]}',['trivy.json','security-decision.json'])
    report=reports['trivy.json'];old=reports['security-decision.json']
    legacy.require(old.get('decision')=='PASS' and old.get('fingerprint')==request['fingerprint']
        and old.get('image_id')==report.get('Metadata',{}).get('ImageID'),'Runtime security evidence blocked/mismatched')
    decision=require_clean_scan(report,request)
    image=current['candidate']['pair']['runtime_digest']
    legacy.require(cat.lookup(request['fingerprint'])==image,'Runtime catalog MISS/mismatch')
    verify_registry_image(image,report,cat)
    # Bind stable evidence, not the re-evaluation clock, across approval wait.
    binding=dict(runtime_run=run_id,source_sha=sha,request=request,scan_hash=digest(report),image=image)
    return digest(binding),decision


def preflight():
    src=release_source();settings=protections()
    state=state_store().read();version=legacy.env('MODEL_VERSION')
    kind,reference=evidence_selection()
    proof,decision=verify_runtime(state,version,reference,catalog(),evidence_source=kind)
    candidate=evidence(state,version,legacy.env('TICKET_REFERENCE'),src,proof,time.time())
    write('candidate.json',candidate)
    write('security-decision.json',decision)
    write('preflight.json',dict(outcome='AWAITING_HUMAN_APPROVAL',ticket_validation='reference_only_human_must_verify',
        staging_inference='NOT_PERFORMED',settings=settings,runtime_evidence_source=kind,checks='canonical candidate, idle durable stable baseline, runtime build/smoke/security evidence, immutable catalog identity',evidence_hash=digest(candidate)))


def authorize_release():
    src=release_source();protections()
    expected=json.loads((OUT/'candidate.json').read_text())
    legacy.require(expected['source']==src and expected['ticket_reference']==legacy.env('TICKET_REFERENCE')
        and expected['candidate']['pair']['model_version']==legacy.env('MODEL_VERSION'),'Pre-approval evidence mismatch')
    store=state_store(writable=True)
    current=store.read();kind,reference=evidence_selection()
    proof,_=verify_runtime(current,legacy.env('MODEL_VERSION'),reference,catalog(),evidence_source=kind)
    legacy.require(proof==expected['security_hash'],'Runtime eligibility evidence changed')
    # Review last, after potentially slow evidence checks, immediately before CAS.
    run=legacy.github_json(f'actions/runs/{src["run_id"]}')
    verify_human_review(run,legacy.github_json(f'actions/runs/{src["run_id"]}/approvals'),src)
    if expected.get('supersedes'):
        from control_plane.runtime_security import _time
        previous=next(x for x in current['pending'] if x['event']['model_version']==legacy.env('MODEL_VERSION'))['authorization']
        legacy.require(previous['evidence']==expected or _time(run.get('created_at')).timestamp()>=previous['expires_at'],
                       'Reauthorization requires a new run created after expiry')
    auth=authorize(store,expected,now=time.time())
    write('authorization.json',dict(outcome='AUTHORIZED',authorization=auth,rollout='PENDING_UNVERIFIED'))


def terminal(state,authorization_id):
    active=state.get('active')
    if active and active.get('authorization',{}).get('evidence_hash')==authorization_id:
        return 'RELEASE_BLOCKED' if active['phase']=='BLOCKED' else 'ROLLOUT_STARTED'
    for record in reversed(state['audit']):
        if record.get('authorization_id')!=authorization_id:continue
        outcome={'release_completed':'PROMOTED','rollback_completed':'ROLLED_BACK','release_started':'ROLLOUT_STARTED'}.get(record['reason'])
        if outcome:return outcome
    return 'AUTHORIZED'


def observe():
    auth=json.loads((OUT/'authorization.json').read_text())['authorization']
    state=state_store(writable=True).read()
    write('outcome.json',dict(outcome=terminal(state,auth['evidence_hash']),authorization_id=auth['evidence_hash'],
        note='Only durable identity-bound audit proves rollout; bounded history may leave outcome unverified'))


def summarize():
    needs=json.loads(legacy.env('NEEDS_JSON'))
    result={k:v['result'] for k,v in needs.items()}
    outcome='VALIDATION_FAILED'
    if result.get('validation')=='success':outcome='NOT_ELIGIBLE'
    if result.get('preflight')=='success':outcome='AUTHORIZATION_FAILED'
    if result.get('production')=='cancelled':outcome='CANCELED'
    if result.get('production')=='failure':
        try:
            reviews=legacy.github_json(f'actions/runs/{legacy.env("GITHUB_RUN_ID")}/approvals')
            if any(r.get('state')=='rejected' and any(e.get('name')=='production' for e in r.get('environments',[])) for r in reviews):
                outcome='APPROVAL_REJECTED'
        except Exception:
            pass  # Native review unavailable: retain failure, never fabricate rejection.

    # GitHub reports rejected jobs as failure; do not invent rejection without review evidence.
    if result.get('production') in ('success','failure','cancelled') and (OUT/'authorization.json').exists():
        auth=json.loads((OUT/'authorization.json').read_text())
        if auth.get('outcome')=='AUTHORIZED':
            expected=json.loads((OUT/'candidate.json').read_text())
            legacy.require(auth['authorization']['evidence']==expected and expected['source']['run_id']==legacy.env('GITHUB_RUN_ID')
                and expected['source']['sha']==legacy.env('GITHUB_SHA'),'Audit identity mismatch')
            outcome='AUTHORIZED'
            if (OUT/'outcome.json').exists():
                observed=json.loads((OUT/'outcome.json').read_text())
                if observed.get('authorization_id')==auth['authorization']['evidence_hash'] and observed.get('outcome') in ('PROMOTED','ROLLED_BACK','ROLLOUT_STARTED','RELEASE_BLOCKED'):
                    outcome=observed['outcome']
    write('summary.json',dict(outcome=outcome,jobs=result,human_review='See native GitHub production deployment review',deployment_success=outcome=='PROMOTED'))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['source','preflight','authorize','observe','summary']);args=p.parse_args()
    try:
        if args.action=='source':write('source.json',release_source())
        else:{'preflight':preflight,'authorize':authorize_release,'observe':observe,'summary':summarize}[args.action]()
    except Exception as exc:
        write(args.action+'-failure.json',dict(outcome='AUTHORIZATION_FAILED' if args.action=='authorize' else 'NOT_ELIGIBLE' if args.action=='preflight' else 'VALIDATION_FAILED',error_type=type(exc).__name__))
        raise SystemExit('Release blocked; see sanitized failure evidence') from None


if __name__=='__main__':main()
