"""Restart-resumable release state machine. Kubernetes effects are idempotent."""
import json
import math
import os
import time
import uuid
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import HTTPError,URLError
from prometheus_client import Counter,Gauge
from .state import MODEL,Busy,audit,HISTORY
from .policy import Policy
from .telemetry import log,serve

started=Counter('release_started_total','Candidates claimed')
promoted=Counter('release_promoted_total','Finalized stable promotions')
rolled_back=Counter('release_rolled_back_total','Completed rollbacks')
failed=Counter('release_failed_total','Failed state machine operations')
phase_metric=Gauge('release_phase','Active phase, 0 idle 1 preparing 2 ramp 3 finalizing 4 draining 5 rollback 6 blocked')
waiting=Counter('release_insufficient_observations_total','RAMP checks waiting for Canary observations')

def api(url):
    with urlopen(url,timeout=4) as response: return json.load(response)

class BackendTransportUnavailable(Exception):
    """Only Stable HTTP probe transport failures eligible for finalization retry."""
    pass

class Effects:
    def __init__(self,kube): self.kube=kube
    def ensure(self,track,digest,replicas): return self.kube.ensure(track,digest,replicas)
    def ready(self,track,candidate,*,wait_transport=False):
        if not self.kube.ready(track,candidate['digest']): return False
        base=f'http://credit-risk-classifier-{track}:8080'
        try:
            api(base+'/health')
            metadata=api(base+'/metadata')
        except HTTPError:
            # An HTTP error response is not a transient connection failure.
            raise
        except (URLError,TimeoutError,ConnectionError) as exc:
            if wait_transport: raise BackendTransportUnavailable(type(exc).__name__) from exc
            raise
        if metadata.get('model_name')!=MODEL or str(metadata.get('model_version'))!=str(candidate['version']):
            raise ValueError('Backend metadata contract mismatch')
        return True
    def router(self): return api('http://credit-risk-router:8080/state')
    def metrics(self):
        prom=os.environ.get('PROMETHEUS_URL','http://prometheus:9090')
        queries={
            'up':'min(up{job="credit-risk-router"})',
            'age':'max(time()-timestamp(up{job="credit-risk-router"}))',
            'traffic':'sum(increase(credit_risk_router_requests_total{track="canary"}[30s]))',
            'success':'sum(increase(credit_risk_router_requests_total{track="canary",status=~"2.."}[30s]))',
            'errors':'sum(rate(credit_risk_router_requests_total{track="canary",status=~"5.."}[30s])) / clamp_min(sum(rate(credit_risk_router_requests_total{track="canary"}[30s])),1e-12)',
            'p95':'histogram_quantile(0.95,sum by(le)(rate(credit_risk_router_request_duration_seconds_bucket{track="canary"}[30s])))',
            'stable_errors':'sum(rate(credit_risk_router_requests_total{track="stable",status=~"5.."}[30s])) / clamp_min(sum(rate(credit_risk_router_requests_total{track="stable"}[30s])),0.001)',
        }
        values={}
        for key,query in queries.items():
            response=api(prom+'/api/v1/query?'+urlencode({'query':query}))
            rows=response.get('data',{}).get('result',[])
            # Missing/nonfinite samples are policy inputs, not internal errors.
            # In particular p95 is undefined until traffic exists. Never zero it.
            value=float('nan')
            if response.get('status')=='success' and len(rows)==1:
                try: value=float(rows[0]['value'][1])
                except (KeyError,IndexError,TypeError,ValueError): pass
            values[key]=value
        return values

def assess(values,error_limit=.05,p95_limit=.1):
    required=('up','age','traffic','success','errors','p95','stable_errors')
    if any(k not in values or not math.isfinite(values[k]) for k in required): return ['invalid_canary_metrics']
    reasons=[]
    if values['up']!=1 or not 0<=values['age']<=15: reasons.append('router_unavailable_or_stale')
    if values['traffic']<=0: reasons.append('no_real_canary_traffic')
    if values['success']<=0: reasons.append('no_successful_canary_predictions')
    if not 0<=values['errors']<error_limit: reasons.append('canary_errors')
    if not 0<=values['stable_errors']<error_limit: reasons.append('stable_errors')
    if not 0<=values['p95']<p95_limit: reasons.append('canary_latency')
    return reasons

def route(state,weight):
    if state['route']['weight']!=weight:
        state['route']={'weight':weight,'revision':state['route']['revision']+1}

class Controller:
    def __init__(self,store,effects,owner=None,clock=time.time,dwell=35,timeout=300,minimum=1,error_limit=.05,p95_limit=.1):
        self.store=store;self.effects=effects;self.owner=owner or str(uuid.uuid4());self.clock=clock;self.dwell=dwell;self.timeout=timeout
        self.minimum=minimum;self.error_limit=error_limit;self.p95_limit=p95_limit
    def acquire(self):
        def update(state):
            lease=state.get('lease')
            if lease and lease['owner']!=self.owner and lease['until']>self.clock(): return False
            state['lease']={'owner':self.owner,'until':self.clock()+60};return True
        return self.store.update(update)
    def guard(self):
        state=self.store.read();lease=state.get('lease')
        if not lease or lease['owner']!=self.owner or lease['until']<=self.clock()+10: raise Busy('Controller lease unavailable')
        return state
    def change(self,fn):
        def update(state):
            lease=state.get('lease')
            if not lease or lease['owner']!=self.owner or lease['until']<=self.clock(): raise Busy('Controller lease lost')
            return fn(state)
        return self.store.update(update)
    def ensure(self,track,digest,replicas):
        self.guard();return self.effects.ensure(track,digest,replicas)
    def begin_rollback(self,reason):
        def update(state):
            active=state['active']
            if not active or active['phase'] in ('DRAINING','ROLLBACK','BLOCKED'): return
            previous=active['phase']
            active.update(phase='ROLLBACK',reason=reason,deadline=self.clock()+self.timeout,restore_stable=previous=='FINALIZING')
            # Before finalization, stable has never changed, so route away now.
            if previous!='FINALIZING': route(state,0)
            audit(state,'rollback_requested',digest=active['digest'],reason_detail=reason)
        self.change(update);log('ROLLBACK_REQUESTED',reason=reason)
    def tick(self):
        if not self.acquire(): return
        state=self.guard();active=state['active']
        if not active:
            phase_metric.set(0)
            def claim(s):
                eligible=[c for c in s['pending'] if c['digest']!=s['stable']['digest'] and
                          (c['version']>s['stable']['version'] or
                           (c['version']==s['stable']['version'] and
                            (not s['stable'].get('source_time') or c['source_time']>s['stable']['source_time'])))]
                s['pending']=eligible
                if not eligible: return None
                candidate=max(eligible,key=lambda c:(c['version'],c['source_time']))
                s['pending'].remove(candidate)
                s['active']={**candidate,'phase':'PREPARING','deadline':self.clock()+self.timeout,'stage_since':None,'previous_stable':dict(s['stable'])}
                route(s,0);audit(s,'release_started',digest=candidate['digest'],version=candidate['version'])
                return candidate
            candidate=self.change(claim)
            if candidate: started.inc();log('RELEASE_STARTED',**candidate)
            return
        phase=active['phase'];digest=active['digest']
        phase_metric.set({'PREPARING':1,'RAMP':2,'FINALIZING':3,'DRAINING':4,'ROLLBACK':5,'BLOCKED':6}[phase])
        try:
            if phase=='PREPARING':
                self.ensure('canary',digest,1)
                if self.effects.ready('canary',active):
                    def arm(s): s['active'].update(phase='RAMP',stage_since=None,deadline=self.clock()+self.timeout);route(s,10)
                    self.change(arm);log('CANARY_READY',digest=digest,weight=10)
                elif self.clock()>active['deadline']: self.begin_rollback('candidate_readiness_timeout')
            elif phase=='RAMP':
                if not self.effects.ready('canary',active) or not self.effects.ready('stable',state['stable']):
                    self.begin_rollback('backend_unhealthy');return
                observed=self.effects.router()
                if observed.get('weight')!=state['route']['weight'] or observed.get('revision')!=state['route']['revision'] or not observed.get('ready'):
                    if self.clock()>active['deadline']: self.begin_rollback('router_ack_timeout')
                    return
                if active.get('stage_since') is None:
                    self.change(lambda s:s['active'].update(stage_since=self.clock()));return
                metrics=self.effects.metrics()
                if any(not math.isfinite(metrics.get(k,float('nan'))) for k in ('up','age','traffic')) or metrics['traffic']<0:
                    self.begin_rollback('invalid_canary_metrics');return
                if metrics['up']!=1 or not 0<=metrics['age']<=15:
                    self.begin_rollback('router_unavailable_or_stale');return
                if metrics['traffic']<self.minimum:
                    if self.clock()>=active['deadline']:
                        self.begin_rollback('insufficient_canary_traffic');return
                    waiting.inc()
                    log('RAMP_WAIT_TRAFFIC',reason='insufficient_observations',model=MODEL,
                        version=active['version'],digest=digest,phase=phase,weight=state['route']['weight'],
                        observations=metrics['traffic'],minimum=self.minimum,deadline=active['deadline'])
                    return
                if self.clock()-active['stage_since']<self.dwell:
                    if self.clock()>=active['deadline']: self.begin_rollback('stage_deadline_exceeded')
                    return
                reasons=assess(metrics,self.error_limit,self.p95_limit)
                if reasons: self.begin_rollback(','.join(reasons));return
                if self.clock()>=active['deadline']:
                    self.begin_rollback('stage_deadline_exceeded');return
                weight=state['route']['weight']
                if weight<100:
                    new={10:25,25:50,50:100}[weight]
                    def advance(s):
                        route(s,new);s['active'].update(stage_since=None,deadline=self.clock()+self.timeout)
                        audit(s,'stage_pass',digest=digest,previous_weight=weight,weight=new,metrics=metrics)
                    self.change(advance);log('PROMOTE',digest=digest,previous_weight=weight,weight=new,metrics=metrics)
                else:
                    self.change(lambda s:s['active'].update(phase='FINALIZING',deadline=self.clock()+self.timeout))
                    log('FINALIZING',digest=digest,metrics=metrics)
            elif phase=='FINALIZING':
                self.ensure('stable',digest,1)
                try:
                    ready=self.effects.ready('stable',active,wait_transport=True)
                except BackendTransportUnavailable as exc:
                    log('FINALIZE_WAIT_BACKEND',model=MODEL,version=active['version'],digest=digest,
                        phase=phase,reason='stable_backend_unavailable',error_type=str(exc),deadline=active['deadline'])
                    if self.clock()>=active['deadline']: self.begin_rollback('stable_backend_unavailable')
                    return
                if self.clock()>=active['deadline']:
                    self.begin_rollback('stable_readiness_timeout');return
                if ready:
                    def finalize(s):
                        s['stable']={k:active[k] for k in ('version','digest','source_time')}
                        route(s,0);s['active'].update(phase='DRAINING',deadline=self.clock()+self.timeout)
                        audit(s,'stable_committed',digest=digest,version=active['version'])
                    self.change(finalize)
            elif phase=='DRAINING':
                observed=self.effects.router()
                if not observed.get('ready') or observed.get('weight')!=0 or observed.get('revision')!=state['route']['revision']:
                    if self.clock()>active['deadline']: log('DRAIN_PENDING',severity='ERROR',digest=digest)
                    return
                self.ensure('canary',digest,0)
                def complete(s):
                    s['processed']=(s['processed']+[digest])[-HISTORY:];s['active']=None
                    audit(s,'release_promoted',digest=digest)
                self.change(complete);promoted.inc();log('RELEASE_PROMOTED',digest=digest,version=active['version'])
            elif phase=='ROLLBACK':
                if active.get('restore_stable'):
                    previous=active['previous_stable']
                    self.ensure('stable',previous['digest'],1)
                    if not self.effects.ready('stable',previous):
                        if self.clock()>active['deadline']:
                            self.change(lambda s:s['active'].update(phase='BLOCKED'))
                            log('RECOVERY_BLOCKED',severity='ERROR',reason='previous_stable_not_ready')
                        return
                    def restored(s):
                        route(s,0);s['active']['restore_stable']=False
                    self.change(restored);return
                self.change(lambda s:route(s,0))
                observed=self.effects.router()
                current=self.guard()
                if observed.get('weight')!=0 or observed.get('revision')!=current['route']['revision']: return
                self.ensure('canary',digest,0)
                def complete_rollback(s):
                    s['failed']=(s['failed']+[{'digest':digest,'version':active['version'],'source_time':active.get('source_time',0),'reason':active['reason'],'time':self.clock()}])[-HISTORY:]
                    s['active']=None;audit(s,'release_rolled_back',digest=digest)
                self.change(complete_rollback);rolled_back.inc();log('RELEASE_ROLLED_BACK',digest=digest)
            # BLOCKED deliberately retains durable state for operator recovery.
        except Busy: raise
        except Exception as exc:
            failed.inc();log('RELEASE_ERROR',phase=phase,error_type=type(exc).__name__)
            if phase in ('PREPARING','RAMP','FINALIZING'): self.begin_rollback(type(exc).__name__)
            # Recovery errors do not clear state or claim rollback succeeded.

def main():
    from .state import Store
    from .kube import Kube
    policy=Policy.from_env()
    kube=Kube();controller=Controller(Store(kube),Effects(kube),dwell=policy.dwell,timeout=policy.timeout,
                                    minimum=policy.minimum,error_limit=policy.error_limit,p95_limit=policy.p95_limit);health={'last':0}
    serve(9092,lambda:time.time()-health['last']<30)
    while True:
        try: controller.tick();health['last']=time.time()
        except Exception as exc: log('CONTROLLER_RETRY',error_type=type(exc).__name__)
        time.sleep(3)

if __name__=='__main__': main()
