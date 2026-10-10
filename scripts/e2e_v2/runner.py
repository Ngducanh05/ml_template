"""Bounded read-only release observer and explicitly opted-in synthetic V2 traffic."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import math
import json
import os
from pathlib import Path
import re
import signal
import ssl
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

STATE_NAME = "model-release-state-v2"
STAGES = {10, 25, 50, 100}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value) or int(value)>2147483647:
        raise ValueError("invalid_model_version")
    return value


def validate_config(config):
    version(config["version"])
    if config["mode"] not in ("observe", "baseline", "policy-test"):
        raise ValueError("invalid_mode")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", config["run_id"]):
        raise ValueError("invalid_run_id")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", config["namespace"]):
        raise ValueError("invalid_namespace")
    for key, low, high in (("rps",0,20),("concurrency",1,8),("requests",1,2000),("duration",1,900),("api_retries",0,3)):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("invalid_"+key)
    if config["mode"]=="observe" and config["rps"]!=0:
        raise ValueError("observe_has_no_traffic")
    if config["mode"]!="observe" and config["rps"]==0:
        raise ValueError("traffic_rate_required")
    if config["mode"]=="policy-test" and config.get("allow_synthetic_gate_traffic") is not True:
        raise ValueError("synthetic_quality_gate_opt_in_required")


def exact_ramp(config,state):
    """Automatic runs bind one claimed authorization, never just a version number."""
    if 'expected_release' not in config:return
    from control_plane.automatic_traffic_v2 import binding
    active=state.get('active')
    if (config['mode']!='policy-test' or not active or active.get('phase')!='RAMP'
            or state['route']['weight'] not in STAGES
            or binding(active)!=config['expected_release']
            or state['stable']!=config['expected_release']['previous_stable']):
        raise ValueError('automatic_release_identity_or_phase_changed')


def automatic_observation(config, state):
    """Observe one authorization across phases; this does not grant send permission."""
    from control_plane.automatic_traffic_v2 import binding
    expected=config['expected_release']
    if (config['mode']!='policy-test' or config['version']!=expected['pair']['model_version']
            or state.get('schema_version')!=2 or state.get('model_name')!='credit-risk-classifier'):
        raise ValueError('unexpected_automatic_release')
    active=state.get('active')
    if active and binding(active)!=expected:
        raise ValueError('automatic_release_identity_changed')
    # Stage audit rows lack IDs: only the latest contiguous release segment is usable.
    starts=[i for i,row in enumerate(state['audit']) if row['reason']=='release_started']
    if not starts:
        return 'BLOCKED','missing_exact_release_evidence',[]
    rows=state['audit'][starts[-1]:]; start=rows[0]
    if start.get('pair')!=expected['pair'] or start.get('authorization_id')!=expected['authorization_id']:
        return 'BLOCKED','conflicting_release_evidence',[]
    stages=[]; terminal=None
    for row in rows[1:]:
        if terminal is not None:
            if row['reason'] in ('stage_promoted','release_completed','rollback_completed'):
                return 'BLOCKED','conflicting_terminal_evidence',stages
        elif row['reason']=='stage_promoted':
            stages.append(row['previous_weight'])
        elif row['reason'] in ('release_completed','rollback_completed'):
            terminal=row
    if active:
        if terminal is not None:
            return 'BLOCKED','conflicting_terminal_evidence',stages
        phase=active['phase']
        if phase=='BLOCKED': return 'BLOCKED','controller_blocked',stages
        if phase not in ('PREPARING','RAMP','FINALIZING','DRAINING','ROLLBACK'):
            raise ValueError('unexpected_automatic_phase')
        if phase in ('PREPARING','RAMP','FINALIZING') and state['stable']!=expected['previous_stable']:
            raise ValueError('automatic_stable_changed')
        if phase=='DRAINING' and state['stable']!=expected['pair']:
            raise ValueError('automatic_stable_changed')
        if phase=='ROLLBACK' and state['stable'] not in (expected['previous_stable'],expected['pair']):
            raise ValueError('automatic_stable_changed')
        return None,None,stages
    if (not terminal or terminal.get('authorization_id')!=expected['authorization_id']
            or state['route']['weight']!=0):
        return 'BLOCKED','missing_exact_terminal_evidence',stages
    if terminal['reason']=='release_completed':
        if terminal.get('pair')!=expected['pair'] or state['stable']!=expected['pair']:
            return 'BLOCKED','terminal_pair_mismatch',stages
        if stages!=[10,25,50,100]: return 'BLOCKED','incomplete_stage_evidence',stages
        return 'PASS','release_completed',stages
    failed=[x for x in state['failed'] if x['pair']==expected['pair']]
    if (terminal.get('previous_stable')!=expected['previous_stable']
            or state['stable']!=expected['previous_stable'] or len(failed)!=1):
        return 'BLOCKED','rollback_identity_mismatch',stages
    reasons=[row.get('detail') for row in rows if row['reason']=='rollback_requested']
    if not reasons or reasons[-1]!=failed[0]['reason']:
        return 'BLOCKED','rollback_reason_mismatch',stages
    return 'FAIL',failed[0]['reason'],stages


def admit(state, target, mode):
    if state.get("schema_version")!=2 or state.get("model_name")!="credit-risk-classifier":
        raise ValueError("unexpected_state")
    if mode=="baseline":
        if state["active"] or not state["stable"] or state["stable"]["model_version"]!=target or state["route"]["weight"]!=0:
            raise ValueError("baseline_not_idle_or_wrong_version")
        return
    if any(p["model_version"]==target for p in state["processed"]) or any(p["pair"]["model_version"]==target for p in state["failed"]):
        raise ValueError("replay_rejected")
    active=state.get("active")
    if active and active["pair"]["model_version"]!=target:
        raise ValueError("conflicting_release")
    known=(active and active["pair"]["model_version"]==target) or any(p["event"]["model_version"]==target for p in state["pending"])
    if not known and int(target)<=state["highest_accepted"]:
        raise ValueError("stale_target")


def observation(state, target):
    active=state.get("active")
    failed=next((x for x in state["failed"] if x["pair"]["model_version"]==target),None)
    if failed:
        return "FAIL", (failed["reason"] if failed["reason"] in {"insufficient_canary_traffic", "invalid_canary_metrics", "backend_unhealthy", "errors", "latency"} else "controller_rollback"), []
    collecting=False; stages=[]; completed=False
    for row in state["audit"]:
        if row["reason"]=="release_started":
            collecting=row.get("pair",{}).get("model_version")==target
        if collecting and row["reason"]=="stage_promoted":
            stages.append(row["previous_weight"])
        if collecting and row["reason"]=="release_completed" and row.get("pair",{}).get("model_version")==target:
            completed=True; collecting=False
    if active and active["pair"]["model_version"]!=target:
        return "BLOCKED", "conflicting_release", stages
    if active and active["phase"]=="BLOCKED":
        return "BLOCKED", "controller_blocked", stages
    if completed and state["stable"]["model_version"]==target and active is None and state["route"]["weight"]==0:
        return ("PASS","release_completed",stages) if STAGES.issubset(stages) else ("BLOCKED","incomplete_stage_evidence",stages)
    return None,None,stages


def validate_payload(payload, metadata):
    if set(payload)-{"inputs","parameters","id"} or not isinstance(payload.get("inputs"),list):
        raise ValueError("invalid_v2_payload")
    fields={x["name"]:x for x in metadata["inputs"]}
    if len(payload["inputs"])!=len(fields) or {x.get("name") for x in payload["inputs"]}!=set(fields):
        raise ValueError("model_signature_mismatch")
    for item in payload["inputs"]:
        if item.get("datatype")!=fields[item["name"]]["datatype"] or not item.get("data") or not item.get("shape"):
            raise ValueError("model_input_contract_mismatch")
        shape=item['shape']
        if any(type(n) is not int or n < 1 for n in shape) or math.prod(shape)!=len(item['data']):
            raise ValueError('model_input_shape_mismatch')
        expected=fields[item['name']].get('shape')
        if expected and (len(expected)!=len(shape) or any(a not in (-1, None) and a!=b for a,b in zip(expected,shape))):
            raise ValueError('model_input_shape_mismatch')
        if any(isinstance(x,float) and not math.isfinite(x) for x in item['data']):
            raise ValueError('nonfinite_input')
    if len(json.dumps(payload).encode())>65536:
        raise ValueError("payload_too_large")


def deployment_matches(obj, pair, allow_scaling=False):
    """Reject an old Ready replica masking a pending candidate rollout."""
    spec=obj.get('spec',{}); status=obj.get('status',{}); desired=spec.get('replicas',0)
    template=spec.get('template',{}); ann=template.get('metadata',{}).get('annotations',{})
    containers=template.get('spec',{}).get('containers',[])
    actual=status.get('replicas',0)
    minimum=1 if allow_scaling else desired
    return (desired>0 and actual>0 and status.get('observedGeneration',0)>=obj['metadata']['generation']
        and status.get('updatedReplicas',0)==actual
        and (allow_scaling or actual==desired)
        and status.get('readyReplicas',0)>=minimum and status.get('availableReplicas',0)>=minimum
        and ann.get('ml-template.io/model-version')==pair['model_version']
        and ann.get('internal.serving.kserve.io/storage-initializer-sourceuri')==pair['storage_uri']
        and any(c.get('name')=='kserve-container' and c.get('image')==pair['runtime_digest'] for c in containers))


class Evidence:
    def __init__(self, directory):
        self.root=Path(directory); self.root.mkdir(parents=True,exist_ok=True)
        self.file=(self.root/"events.jsonl").open("a",encoding="utf-8")
    def emit(self, kind, **fields):
        # Call sites provide a fixed allowlist, never state/event/payload/error bodies.
        line=json.dumps(dict(time=time.time(),kind=kind,**fields),allow_nan=False)
        self.file.write(line+"\n"); self.file.flush(); print(line,flush=True)
    def finish(self, result):
        (self.root/"summary.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
        self.file.close()


class Live:
    def __init__(self, namespace):
        token_root=Path("/var/run/secrets/kubernetes.io/serviceaccount")
        if token_root.joinpath("namespace").read_text().strip()!=namespace:
            raise ValueError("namespace_mismatch")
        self.token=token_root/"token"
        self.context=ssl.create_default_context(cafile=str(token_root/"ca.crt"))
        self.api="https://"+os.environ["KUBERNETES_SERVICE_HOST"]+":"+os.environ.get("KUBERNETES_SERVICE_PORT","443")
        self.state_url=self.api+"/api/v1/namespaces/"+namespace+"/configmaps/"+STATE_NAME
        self.router="http://credit-risk-router-v2."+namespace+".svc:8080"
        self.namespace=namespace
    def state(self):
        request=Request(self.state_url,headers={"Authorization":"Bearer "+self.token.read_text().strip()})
        with urlopen(request,context=self.context,timeout=3) as response:
            obj=json.load(response)
        return json.loads(obj["data"]["state.json"])
    def ready(self):
        with urlopen(self.router+"/health/ready",timeout=3) as r: return r.status==200
    def metadata(self, lane='stable'):
        name='credit-risk-'+lane+'-v2'
        with urlopen('http://'+name+'-predictor.'+self.namespace+'.svc/v2/models/'+name,timeout=3) as r:
            return json.load(r)
    def verify_pair(self, lane, pair, allow_scaling=False):
        name='credit-risk-'+lane+'-v2-predictor'
        url=self.api+'/apis/apps/v1/namespaces/'+self.namespace+'/deployments/'+name
        request=Request(url,headers={'Authorization':'Bearer '+self.token.read_text().strip()})
        with urlopen(request,context=self.context,timeout=3) as response:
            return deployment_matches(json.load(response),pair,allow_scaling)
    def send(self, body, run_id):
        request=Request(self.router+"/v2/models/credit-risk-classifier/infer",data=body,
            headers={"Content-Type":"application/json","X-E2E-Run":run_id,"X-Traffic-Origin":"synthetic"})
        started=time.monotonic()
        try:
            try: response=urlopen(request,timeout=3)
            except HTTPError as exc: response=exc
            with response:
                raw=response.read(1048577)
                status=response.status
                if 200<=status<300:
                    try:
                        obj=json.loads(raw)
                        if len(raw)>1048576 or not obj.get("outputs") or not all("data" in x for x in obj["outputs"]): status=502
                    except (ValueError,TypeError,AttributeError): status=502
                track=response.headers.get("X-Release-Track","unknown")
                if track not in ("stable","canary"): track="unknown"
                return dict(status=status,track=track,latency_ms=round(1000*(time.monotonic()-started),3))
        except (OSError,TimeoutError):
            return dict(status=0,track="unknown",latency_ms=round(1000*(time.monotonic()-started),3))


def execute(config, payload, client, evidence, clock=time.monotonic, sleep=time.sleep, cancelled=lambda:False):
    validate_config(config)
    counts={"requests":0,"errors":0,"stable":0,"canary":0}; stages=set(); started=clock()
    outcome,reason="BLOCKED","timeout"; futures=[]; next_send=started; next_poll=started; failures=0
    evidence.emit("run_started",run_id=config["run_id"],target=config["version"],mode=config["mode"],synthetic=config["mode"]!="observe",
        synthetic_can_affect_quality_gate=config["mode"]=="policy-test",limits={k:config[k] for k in ("rps","concurrency","requests","duration","api_retries")})
    def collect(future):
        row=future.result(); counts["requests"]+=1
        counts["errors"]+=int(not 200<=row["status"]<300)
        if row["track"] in ("stable","canary"): counts[row["track"]]+=1
        else: counts["errors"]+=int(200<=row["status"]<300)
        evidence.emit("request",**row)
    try:
        automatic='expected_release' in config
        state=client.state()
        if automatic:
            status,detail,seen=automatic_observation(config,state)
            stages.update(seen)
            if status:
                result=dict(outcome=status,reason=detail,target=config['version'],mode=config['mode'],
                    synthetic=True,stages=sorted(stages),**counts)
                evidence.finish(result)
                return result
        else:
            admit(state,config["version"],config["mode"])
            if not client.ready(): raise ValueError("router_not_ready")
        if config['mode']=='baseline':
            if not client.verify_pair('stable',state['stable']): raise ValueError('backend_identity_not_ready')
            validate_payload(payload,client.metadata('stable'))
        body=json.dumps(payload,allow_nan=False).encode()
        with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
            while clock()-started<config["duration"]:
                if cancelled(): outcome,reason="BLOCKED","cancelled"; break
                for f in list(futures):
                    if f.done(): collect(f); futures.remove(f)
                if clock()>=next_poll:
                    try:
                        state=client.state(); failures=0
                    except (OSError,TimeoutError):
                        failures+=1
                        if failures>config["api_retries"]: outcome,reason="BLOCKED","kubernetes_unavailable"; break
                        sleep(min(1,config["duration"]-(clock()-started))); continue
                    next_poll=clock()+1
                    if config["mode"]=="baseline":
                        admit(state,config["version"],"baseline")
                        if not client.verify_pair('stable',state['stable'],allow_scaling=True):
                            sleep(0.1); next_poll=clock(); continue
                    else:
                        status,detail,seen=automatic_observation(config,state) if automatic else observation(state,config["version"]); stages.update(seen)
                        if status: outcome,reason=status,detail; break
                    active=state.get("active")
                    evidence.emit("state",target=config["version"],phase=active["phase"] if active else "IDLE",weight=state["route"]["weight"],stages=sorted(stages))
                sent=counts["requests"]+len(futures)
                if sent>=config["requests"] and not futures and not automatic:
                    if config["mode"]=="baseline": outcome,reason="PASS","baseline_smoke"; break
                    outcome,reason="BLOCKED","request_budget_exhausted"; break
                active=state.get("active")
                traffic=config["mode"]=="baseline" or (config["mode"]=="policy-test" and active and active["pair"]["model_version"]==config["version"] and active["phase"]=="RAMP")
                if traffic and clock()>=next_send and len(futures)<config["concurrency"] and sent<config["requests"]:
                    if config['mode']=='policy-test':
                        if 'expected_release' in config:
                            state=client.state()
                            status,detail,_=automatic_observation(config,state)
                            if status or not state.get('active') or state['active']['phase']!='RAMP':
                                next_poll=clock(); continue
                            exact_ramp(config,state);active=state['active']
                            if not client.ready():raise ValueError('router_not_ready')
                            if not client.verify_pair('stable',state['stable']):raise ValueError('stable_identity_not_ready')
                        if not client.verify_pair('canary',active['pair']): raise ValueError('backend_identity_not_ready')
                        validate_payload(payload,client.metadata('canary'))
                        validate_payload(payload,client.metadata('stable'))
                    if 'expected_release' in config:
                        state=client.state()
                        status,detail,_=automatic_observation(config,state)
                        if status or not state.get('active') or state['active']['phase']!='RAMP':
                            next_poll=clock(); continue
                        exact_ramp(config,state)
                        if cancelled():outcome,reason='BLOCKED','cancelled';break
                    futures.append(pool.submit(client.send,body,config["run_id"])); next_send=clock()+1/config["rps"]
                sleep(0.05)
            for future in futures: collect(future)
        if counts["errors"] and not automatic: outcome,reason="FAIL","http_or_track_errors"
    except (ValueError,KeyError,TypeError): outcome,reason="BLOCKED","admission_or_contract_rejected"
    except (OSError,TimeoutError): outcome,reason="BLOCKED","backend_or_api_unavailable"
    result=dict(outcome=outcome,reason=reason,target=config["version"],mode=config["mode"],synthetic=config["mode"]!="observe",stages=sorted(stages),**counts)
    evidence.finish(result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    inputs=parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--config",type=Path);inputs.add_argument("--config-json")
    parser.add_argument("--payload",type=Path,required=True)
    parser.add_argument("--evidence",default="/evidence"); args=parser.parse_args()
    config=json.loads(args.config_json if args.config_json is not None else args.config.read_text()); payload=json.loads(args.payload.read_text())
    stop=[False]
    for sig in (signal.SIGTERM,signal.SIGINT): signal.signal(sig,lambda *_:stop.__setitem__(0,True))
    result=execute(config,payload,Live(config["namespace"]),Evidence(args.evidence),cancelled=lambda:stop[0])
    print(json.dumps(result)); raise SystemExit({"PASS":0,"FAIL":1,"BLOCKED":2}[result["outcome"]])


if __name__=="__main__": main()
