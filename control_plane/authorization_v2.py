"""Exact, bounded release authorization. Only the protected Release writer calls authorize."""
import copy
import hashlib
import json
import math
import re
from .state_v2 import ServingPair, DIGEST, audit, identity

REPOSITORY = 'Ngducanh05/ml_template'
WORKFLOW = '.github/workflows/release.yml'
TTL = 900  # Approval must be claimed within 15 minutes; never extended by retries.
EVIDENCE_TTL = 86400


def require(ok, message):
    if not ok:
        raise ValueError(message)


def ticket(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}', value), 'invalid_ticket_reference')
    return value


def source(value):
    require(isinstance(value, dict) and set(value) == {'repository','workflow','run_id','run_attempt','sha','ref'}, 'invalid_release_source')
    require(value['repository'] == REPOSITORY and value['workflow'] == WORKFLOW, 'unexpected_release_workflow')
    require(isinstance(value['run_id'], str) and re.fullmatch(r'[1-9][0-9]{0,19}', value['run_id']), 'invalid_release_run')
    require(value['run_attempt'] == '1', 'release_rerun_prohibited')
    require(isinstance(value['sha'], str) and re.fullmatch(r'[0-9a-f]{40}', value['sha']), 'invalid_source_sha')
    require(isinstance(value['ref'], str) and re.fullmatch(r'refs/tags/v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', value['ref']), 'release_requires_semver_tag')
    return value


def candidate_identity(item):
    pair = ServingPair(**item['pair']).record()
    require(pair['model_version'] == item['event']['model_version'] and pair['storage_uri'] == item['event']['artifact_uri'], 'candidate_event_mismatch')
    require(isinstance(item['fingerprint'], str) and DIGEST.fullmatch(item['fingerprint']), 'invalid_fingerprint')
    return dict(pair=pair, fingerprint=item['fingerprint'], event=identity(item['event']))


def snapshot(state, version):
    require(state['active'] is None and state['route']['weight'] == 0 and state['stable'] is not None, 'release_baseline_not_idle')
    require(int(version) == state['highest_accepted'] and int(version) > int(state['stable']['model_version']), 'stale_release_candidate')
    matches = [x for x in state['pending'] if x['event']['model_version'] == version]
    require(len(matches) == 1 and matches[0]['status'] in ('READY_FOR_RELEASE','APPROVED_FOR_RELEASE'), 'candidate_not_eligible')
    item = matches[0]
    from .build_contract import validate_request
    validate_request(item['build_request'])
    require(item['build_request']['fingerprint']==item['fingerprint'], 'candidate_contract_mismatch')
    return dict(candidate=candidate_identity(item), stable=copy.deepcopy(state['stable']),
                stable_fingerprint=state['stable_fingerprint'], highest_accepted=state['highest_accepted'])


def evidence(state, version, ticket_reference, release_source, security_hash, now):
    ticket(ticket_reference); source(release_source)
    require(isinstance(security_hash, str) and DIGEST.fullmatch(security_hash), 'security_evidence_required')
    require(type(now) in (int,float) and math.isfinite(now), 'invalid_evidence_time')
    current=snapshot(state,version)
    previous=next(x for x in state['pending'] if x['event']['model_version']==version).get('authorization')
    return dict(schema_version=1, **current, supersedes=previous['evidence_hash'] if previous else None, ticket_reference=ticket_reference,
                source=copy.deepcopy(release_source), security_hash=security_hash, created_at=now)


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_authorization(auth, item):
    require(isinstance(auth,dict) and set(auth)=={'evidence','evidence_hash','authorized_at','expires_at'}, 'invalid_authorization')
    ev=auth['evidence']
    require(isinstance(ev,dict) and set(ev)=={'schema_version','candidate','stable','stable_fingerprint','highest_accepted','ticket_reference','source','security_hash','created_at','supersedes'}, 'invalid_approval_evidence')
    require(ev['schema_version']==1 and ev['candidate']==candidate_identity(item), 'approval_candidate_mismatch')
    require(ev['supersedes'] is None or (isinstance(ev['supersedes'],str) and DIGEST.fullmatch(ev['supersedes'])), 'invalid_previous_authorization')
    ticket(ev['ticket_reference']); source(ev['source']); ServingPair(**ev['stable'])
    require(DIGEST.fullmatch(ev['stable_fingerprint']) and DIGEST.fullmatch(ev['security_hash']), 'invalid_approval_digest')
    require(ev['highest_accepted']==int(ev['candidate']['pair']['model_version']), 'approval_version_mismatch')
    for t in (ev['created_at'],auth['authorized_at'],auth['expires_at']):
        require(type(t) in (int,float) and math.isfinite(t), 'invalid_authorization_time')
    require(0 <= auth['authorized_at']-ev['created_at'] <= EVIDENCE_TTL and auth['expires_at']==auth['authorized_at']+TTL, 'invalid_authorization_lifetime')
    require(auth['evidence_hash']==digest(ev), 'authorization_evidence_mismatch')


def claimable(state, item, now):
    if item.get('status') != 'APPROVED_FOR_RELEASE':
        return False
    try:
        validate_authorization(item.get('authorization'), item)
        auth=item['authorization'];ev=auth['evidence']
        return (auth['authorized_at'] <= now < auth['expires_at'] and
                snapshot(state,item['pair']['model_version']) == {k:ev[k] for k in ('candidate','stable','stable_fingerprint','highest_accepted')})
    except (ValueError,KeyError,TypeError):
        return False


def authorize(store, expected, *, now):
    """Trusted writer boundary: caller has verified native GitHub review and eligibility.

    No boolean input represents human approval. Unprivileged processors never call
    this function; the writer credential is available only in the protected job.
    """
    def mutate(state):
        actual=snapshot(state,expected['candidate']['pair']['model_version'])
        require(actual=={k:expected[k] for k in actual}, 'stale_candidate_evidence')
        item=next(x for x in state['pending'] if x['event']['model_version']==expected['candidate']['pair']['model_version'])
        previous=item.get('authorization')
        if previous:
            if previous['evidence']==expected:
                require(claimable(state,item,now), 'authorization_expired_or_stale')
                return copy.deepcopy(previous)  # Never renew an idempotent retry.
            require(now >= previous['expires_at'], 'already_authorized_by_another_run')
            require(expected.get('supersedes')==previous['evidence_hash'], 'stale_previous_authorization')
            require(expected['source']['run_id']!=previous['evidence']['source']['run_id'], 'fresh_release_run_required')
            require(expected['created_at'] >= previous['expires_at'], 'fresh_preflight_required')
            require(actual=={k:previous['evidence'][k] for k in actual}, 'original_baseline_changed')
        else:
            require(expected.get('supersedes') is None, 'previous_authorization_missing')
        auth=dict(evidence=copy.deepcopy(expected),evidence_hash=digest(expected),authorized_at=now,expires_at=now+TTL)
        validate_authorization(auth,item)
        item.update(status='APPROVED_FOR_RELEASE',authorization=auth)
        audit(state,'release_reauthorized' if previous else 'release_authorized',now,
              previous_authorization_id=previous['evidence_hash'] if previous else None,model_version=item['pair']['model_version'],authorization_id=auth['evidence_hash'],source=expected['source'],ticket_reference=expected['ticket_reference'])
        return copy.deepcopy(auth)
    return store.update(mutate)
