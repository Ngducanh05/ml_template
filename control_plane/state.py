"""Bounded durable state and pure candidate ingestion, shared by SQS and ECR."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import re
import time

MODEL = 'credit-risk-classifier'
REPOSITORY = 'mlflow-models'
REGISTRY = '378457871131'
REGION = 'ap-southeast-1'
IMAGE_REPO = f'{REGISTRY}.dkr.ecr.{REGION}.amazonaws.com/{REPOSITORY}'
BASE_DIGEST = 'sha256:45dcaf3b993483344e55f71d7486cc83ab76fd9cff8a6690318449df48d4a3a5'
DIGEST = re.compile(r'^sha256:[0-9a-f]{64}$')
TAG = re.compile(r'^(?P<model>.+)-v(?P<version>\d+)$')
HISTORY = 100
MAX_PENDING = 20

class Conflict(Exception): pass
class Busy(Exception): pass

def initial_state():
    return {'schema':1,'model':MODEL,'stable':{'version':1,'digest':BASE_DIGEST,'source_time':0},
            'active':None,'pending':[],'processed':[BASE_DIGEST],'failed':[],
            'audit':[],'route':{'weight':0,'revision':0},'lease':None}

def audit(state,reason,**fields):
    state['audit']=(state.get('audit',[])+[{'time':time.time(),'reason':reason,**fields}])[-HISTORY:]

def parse_candidate(tag,digest,source_time):
    match=TAG.fullmatch(tag) if isinstance(tag,str) else None
    if not match: return None,'noncanonical'
    if match['model']!=MODEL: return None,'model_not_allowed'
    if not isinstance(digest,str) or not DIGEST.fullmatch(digest): return None,'invalid_digest'
    version=int(match['version'])
    if version<1 or version>2**31-1: return None,'invalid_version'
    return dict(model=MODEL,version=version,digest=digest,source_time=float(source_time)),None

def event_candidate(body):
    try:
        event=json.loads(body)
        if not isinstance(event,dict): return None,'invalid_event'
        detail=event.get('detail')
        if not isinstance(detail,dict): return None,'invalid_event'
        if event.get('detail-type')!='ECR Image Action': return None,'wrong_event_type'
        if event.get('source','aws.ecr')!='aws.ecr': return None,'wrong_source'
        if event.get('account',REGISTRY)!=REGISTRY or event.get('region',REGION)!=REGION: return None,'wrong_origin'
        if detail.get('action-type')!='PUSH': return None,'wrong_action'
        if detail.get('result')!='SUCCESS': return None,'failed_push'
        if detail.get('repository-name')!=REPOSITORY: return None,'wrong_repository'
        # EventBridge normally supplies time. Missing time is accepted but cannot
        # displace an already ordered same-version release with a newer timestamp.
        raw=event.get('time')
        stamp=datetime.fromisoformat(raw.replace('Z','+00:00')).timestamp() if raw else 0
        return parse_candidate(detail.get('image-tag'),detail.get('image-digest'),stamp)
    except (ValueError,TypeError,OverflowError): return None,'invalid_event'

def ingest(state,candidate,reason=None,event_id=None):
    """Mutates a snapshot only; caller must CAS-persist even ignored decisions."""
    if candidate:
        digest=candidate['digest']; version=candidate['version']
        known=set(state['processed'])|{x['digest'] for x in state['failed']}|{x['digest'] for x in state['pending']}|{state['stable']['digest']}
        if state['active']: known.add(state['active']['digest'])
        stable=state['stable']
        if digest in known: reason='duplicate'
        elif version<stable['version']: reason='stale'
        elif version==stable['version'] and stable.get('source_time',0)>0 and candidate['source_time']<=stable['source_time']: reason='stale_rebuild'
        else:
            if len(state['pending'])>=MAX_PENDING: raise Busy('Pending release queue full')
            state['pending'].append(copy.deepcopy(candidate)); reason='accepted'
    audit(state,reason or 'invalid_event',event_id=event_id,digest=candidate['digest'] if candidate else None)
    return reason or 'invalid_event'

def reconcile_candidates(details):
    candidates=[]
    for image in details:
        stamp=image.get('imagePushedAt',0)
        if isinstance(stamp,datetime): stamp=stamp.timestamp()
        for tag in image.get('imageTags',[]):
            candidate,error=parse_candidate(tag,image.get('imageDigest'),stamp)
            if not error: candidates.append(candidate)
    if not candidates: return []
    highest=max(x['version'] for x in candidates)
    choices=[x for x in candidates if x['version']==highest]
    # ECR may contain both v01 and v1; deterministically prefer newest pushed image.
    return [max(choices,key=lambda x:(x['source_time'],x['digest']))]

class Store:
    def __init__(self,backend,retries=8): self.backend=backend; self.retries=retries
    def read(self): return self.backend.read_state()[1]
    def update(self,mutator):
        for attempt in range(self.retries):
            revision,state=self.backend.read_state()
            state=copy.deepcopy(state)
            result=mutator(state)
            if len(json.dumps(state).encode())>500_000: raise Busy('State size bound exceeded')
            try:
                self.backend.write_state(revision,state)
                return result
            except Conflict:
                time.sleep(min(.02*2**attempt,.5))
        raise Busy('State optimistic concurrency retries exhausted')

def enqueue(store,candidate,reason=None,event_id=None):
    return store.update(lambda state:ingest(state,candidate,reason,event_id))


def operator_retry(store,digest,version,operator,note):
    """Explicit CAS requeue; retain failed history so normal ingestion dedups."""
    if not DIGEST.fullmatch(digest) or not operator.strip() or not note.strip():
        raise ValueError('Digest, operator and explanation are required')
    if len(operator)>128 or len(note)>1024: raise ValueError('Audit fields too long')
    def retry(state):
        if state['active']: raise Busy('An active release conflicts with retry')
        if any(c['digest']==digest for c in state['pending']): raise Busy('Digest already pending')
        matches=[f for f in state['failed'] if f['digest']==digest and f['version']==version]
        if not matches: raise ValueError('Failed digest/version not found')
        if digest==state['stable']['digest'] or digest in state['processed']:
            raise Busy('Digest is already stable or processed')
        previous=matches[-1]
        candidate={'model':MODEL,'digest':digest,'version':version,'source_time':previous.get('source_time',0)}
        stable=state['stable']
        if version<stable['version'] or (version==stable['version'] and stable.get('source_time',0)>0 and candidate['source_time']<=stable['source_time']):
            raise Busy('Retry would regress current Stable')
        if len(state['pending'])>=MAX_PENDING: raise Busy('Pending release queue full')
        state['pending'].append(candidate)
        audit(state,'operator_retry',digest=digest,version=version,operator=operator,note=note,
              previous_failure=previous['reason'])
        return copy.deepcopy(candidate)
    return store.update(retry)
