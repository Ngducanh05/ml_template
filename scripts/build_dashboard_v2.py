"""Build the reviewed dashboard and its ConfigMap locally; no cluster effects."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
DS={'type':'prometheus','uid':'${DS_PROMETHEUS}'}
JOB='job="credit-risk-router-v2"'
TRACK=JOB+',track=~"$track"'
POD='namespace="$namespace",pod=~"${service:regex}.*"'
CONTAINER=POD+',container!="",container!="POD",image!=""'
HPA='namespace="$namespace",horizontalpodautoscaler=~"${model:regex}-(stable|canary)-v2-predictor"'
RATE='[$__rate_interval]'
PANELS=[]


def panel(title, description, queries, x,y,w=8,h=8,kind='timeseries',unit='short',table=False):
    targets=[]
    for i,(legend,expr) in enumerate(queries):
        targets.append(dict(refId=chr(65+i),expr=expr,legendFormat=legend,datasource=DS,range=not table,instant=table,format='table' if table else 'time_series'))
    defaults=dict(unit=unit,noValue='NO DATA',color={'mode':'fixed','fixedColor':'#8E8E8E'},custom=dict(spanNulls=False,drawStyle='line',lineWidth=2,fillOpacity=6,stacking={'mode':'none','group':'A'}))
    overrides=[]
    for regex,color in [('(?i).*stable.*','#73BF69'),('(?i).*canary.*','#E0B400'),('(?i)^Ready$','#73BF69'),('(?i)^Unknown$','#FF9830'),('(?i).*(error|notready|down|blocked|crash).*','#F2495C'),('(?i).*(desired|warning).*','#FF9830')]:
        overrides.append(dict(matcher={'id':'byRegexp','options':regex},properties=[{'id':'color','value':{'mode':'fixed','fixedColor':color}}]))
    obj=dict(id=len(PANELS)+1,title=title,description=description,type=kind,datasource=DS,gridPos=dict(x=x,y=y,w=w,h=h),targets=targets,
        fieldConfig=dict(defaults=defaults,overrides=overrides),options=dict(legend={'displayMode':'table','placement':'bottom','calcs':['lastNotNull']},tooltip={'mode':'multi','sort':'none'}))
    if kind=='stat': obj['options']=dict(reduceOptions={'calcs':['lastNotNull'],'fields':'','values':False},colorMode='value',graphMode='none',textMode='auto',justifyMode='auto')
    if table:
        obj['transformations']=[{'id':'joinByField','options':{'byField':'pod' if 'Pod' in title else 'name','mode':'outerTabular'}}] if len(queries)>1 else []
        obj['options']={'showHeader':True,'cellHeight':'sm','footer':{'show':False}}
    if kind=='state-timeline' and title in ('Pod readiness timeline','Prometheus scrape health'):
        defaults.update(color={'mode':'thresholds'},thresholds={'mode':'absolute','steps':[{'color':'red','value':None},{'color':'green','value':1}]})
    if kind=='state-timeline' and title=='Release phase timeline':
        for phase,color in [('IDLE','#8E8E8E'),('PREPARING','#5794F2'),('RAMP','#E0B400'),('FINALIZING','#73BF69'),('DRAINING','#73BF69'),('ROLLBACK','#FF9830'),('BLOCKED','#F2495C')]:
            overrides.append(dict(matcher={'id':'byName','options':phase},properties=[{'id':'color','value':{'mode':'fixed','fixedColor':color}}]))
    PANELS.append(obj);return obj


def build():
    PANELS.clear()
    panel('Nodes Ready / NotReady','Live KSM node conditions. Missing KSM is NO DATA. Unknown readiness is shown separately.',[(k,f'sum(kube_node_status_condition{{condition="Ready",status="{v}",node=~"$node"}} == bool 1)') for k,v in [('Ready','true'),('NotReady','false'),('Unknown','unknown')]],0,0,4,4,'stat')
    panel('Stable model','Durable version from Router-v2 ReleaseCollector; not proof of complete backend rollout.',[('Stable',f'mlops_v2_stable_version{{{JOB}}}')],4,0,4,4,'stat')
    panel('Active candidate','Absent during IDLE is NO DATA, not model version zero. Check phase and source health.',[('Canary',f'mlops_v2_candidate_version{{{JOB}}}')],8,0,4,4,'stat')
    panel('Canary weight','Effective Router-v2 percentage. This dashboard never changes it.',[('Canary',f'credit_risk_router_weight_percent{{{JOB}}}')],12,0,4,4,'stat','percent')
    panel('Stable HPA current / max','Current and maximum replicas from KSM; a configured max is not scale-out evidence.',[(k,f'kube_horizontalpodautoscaler_{metric}{{namespace="$namespace",horizontalpodautoscaler="${{model}}-stable-v2-predictor"}}') for k,metric in [('Current','status_current_replicas'),('Max','spec_max_replicas')]],16,0,4,4,'stat')
    cost=panel('Nodes / EC2 compute estimate','EC2 compute estimate only: live node count × editable USD/hour ×730. Assumes every counted node is t3.medium Linux On-Demand Singapore. Excludes EKS, EBS, transfer, tax and T3 Unlimited credits. AWS Pricing verified 2026-10-09:0.0528 USD/hour.',[('Nodes','count(kube_node_info)'),('USD/month','count(kube_node_info) * ${node_hourly_usd:raw} * 730')],20,0,4,4,'stat')
    cost['fieldConfig']['overrides'].append(dict(matcher={'id':'byName','options':'USD/month'},properties=[{'id':'unit','value':'currencyUSD'}]))
    traffic=f'sum by(track)(rate(credit_risk_router_requests_total{{{TRACK}}}{RATE}))'
    errors=f'sum by(track)(rate(credit_risk_router_requests_total{{{TRACK},status=~"5.."}}{RATE}))'
    panel('Stable / Canary RPS','Actual forwarded requests, including explicitly run synthetic tests. Source fixed to production Router-v2; namespace affects Kubernetes panels. Zero is valid only with healthy scrape.', [('{{track}}',traffic)],0,4,8,8,unit='reqps')
    panel('5xx error ratio','No traffic => NO DATA. No zero-fill, denominator clamp, or legacy router metrics.', [('{{track}}',f'(100 * {errors} / {traffic}) and on(track) ({traffic} > 0)')],8,4,8,8,unit='percent')
    panel('p50 / p95 latency','Histogram quantiles across all Router-v2 instances, masked when no observations exist. No gaps bridged.',[(f'p{int(q*100)} {{{{track}}}}',f'histogram_quantile({q},sum by(le,track)(rate(credit_risk_router_request_duration_seconds_bucket{{{TRACK}}}{RATE}))) and on(track) ({traffic} > 0)') for q in (.5,.95)],16,4,8,8,unit='s')
    panel('Node CPU used / allocatable','Root cgroup CPU from authenticated kubelet/cAdvisor, compared with schedulable CPU from KSM. Requires node label on scrape targets.', [('{{node}} Used',f'sum by(node)(rate(container_cpu_usage_seconds_total{{id="/",node=~"$node"}}{RATE}))'),('{{node}} Allocatable','kube_node_status_allocatable{resource="cpu",unit="core",node=~"$node"}')],0,12,12,8,unit='cores')
    panel('Node memory working set / allocatable','Root cgroup working set includes system processes; allocatable excludes reserved memory. No fabricated node metrics.', [('{{node}} Working set','max by(node)(container_memory_working_set_bytes{id="/",node=~"$node"})'),('{{node}} Allocatable','kube_node_status_allocatable{resource="memory",unit="byte",node=~"$node"}')],12,12,12,8,unit='bytes')
    cpu=f'sum by(pod)(rate(container_cpu_usage_seconds_total{{{CONTAINER}}}{RATE}))'
    ram=f'sum by(pod)(container_memory_working_set_bytes{{{CONTAINER}}})'
    cpu_req=f'sum by(pod)(kube_pod_container_resource_requests{{{POD},resource="cpu",unit="core"}})'
    cpu_limit=f'sum by(pod)(kube_pod_container_resource_limits{{{POD},resource="cpu",unit="core"}})'
    ram_req=f'sum by(pod)(kube_pod_container_resource_requests{{{POD},resource="memory",unit="byte"}})'
    ram_limit=f'sum by(pod)(kube_pod_container_resource_limits{{{POD},resource="memory",unit="byte"}})'
    panel('Per-Pod CPU usage / request / limit','Cores by Pod; regular container requests/limits, excluding init-container scheduling maxima. Missing limits stay absent.',[('{{pod}} Used',cpu),('{{pod}} Request',cpu_req),('{{pod}} Limit',cpu_limit)],0,20,12,8,unit='cores')
    panel('Per-Pod memory usage / request / limit','Working set and regular container request/limit bytes. Missing metrics are not zero.',[('{{pod}} Used',ram),('{{pod}} Request',ram_req),('{{pod}} Limit',ram_limit)],12,20,12,8,unit='bytes')
    panel('Per-Pod CPU utilization ratios','Usage/request and usage/limit; absent or zero denominators yield NO DATA.',[(f'{{{{pod}}}} / {label}',f'(100 * {cpu} / {denom}) and on(pod) ({denom} > 0)') for label,denom in [('request',cpu_req),('limit',cpu_limit)]],0,28,12,7,unit='percent')
    panel('Per-Pod memory utilization ratios','Working set/request and working set/limit; no synthetic denominator.',[(f'{{{{pod}}}} / {label}',f'(100 * {ram} / {denom}) and on(pod) ({denom} > 0)') for label,denom in [('request',ram_req),('limit',ram_limit)]],12,28,12,7,unit='percent')
    panel('Pod inventory','All selected namespace Pods: name, namespace, node, active phase, Ready and restarts. KSM uses raw API state; readiness is not model identity.',[('Inventory',f'kube_pod_info{{{POD}}} * on(namespace,pod) group_left(phase) (kube_pod_status_phase{{{POD}}} == 1)'),('Ready',f'min by(pod)(kube_pod_container_status_ready{{{POD}}})'),('Restarts',f'sum by(pod)(kube_pod_container_status_restarts_total{{{POD}}})')],0,35,24,10,'table',table=True)
    panel('KServe InferenceServices','Get-only exporter: Ready, metadata/observed generation, mode and latest revision. Standard mode absent revision = N/A. Cross-check Deployment convergence below; Ready alone is insufficient.',[(metric,f'mlops_v2_kserve_isvc_{metric}{{namespace="$namespace"}}') for metric in ('info','ready','generation','observed_generation')],0,45,12,9,'table',table=True)
    hp=panel('HPA details','KServe-owned autoscalers. Missing target metrics may require KSM metric opt-in; do not infer utilization from replica counts.',[(metric,f'kube_horizontalpodautoscaler_{metric}{{{HPA}}}') for metric in ('spec_min_replicas','spec_max_replicas','status_current_replicas','status_desired_replicas','spec_target_metric','status_target_metric','status_condition')],12,45,12,9,'table',table=True)
    hp['transformations']=[{'id':'joinByField','options':{'byField':'horizontalpodautoscaler','mode':'outerTabular'}}]
    panel('Deployment rollout convergence','KSM generation, observedGeneration, desired, updated, ready and available must agree before claiming rollout complete. Identity evidence additionally checks annotations/artifact/image out of band.',[(m,f'kube_deployment_{m}{{namespace="$namespace",deployment=~"${{model:regex}}-(stable|canary)-v2-predictor"}}') for m in ('metadata_generation','status_observed_generation','spec_replicas','status_replicas_updated','status_replicas_ready','status_replicas_available')],0,54,12,9,'table',table=True)['transformations']=[{'id':'joinByField','options':{'byField':'deployment','mode':'outerTabular'}}]
    panel('Prometheus scrape health','Every discovered target: 1=UP,0=DOWN. A target absent from discovery is absent, not healthy; inspect source presence too.', [('{{job}} {{instance}}','up')],12,54,12,9,'state-timeline')
    panel('Pod readiness timeline','Minimum container readiness per selected Pod. Empty intervals remain gaps.', [('{{pod}}',f'min by(pod)(kube_pod_container_status_ready{{{POD}}})')],0,63,12,9,'state-timeline')
    alerts=panel('Operational alert conditions','Conditions only, no notifications: HPA desired>current (investigate5m), CrashLoop2m, target down2m, BLOCKED1m, stale snapshot. Zero requires present healthy telemetry; missing remains NO DATA.',[
        ('HPA replica gap',f'kube_horizontalpodautoscaler_status_desired_replicas{{{HPA}}} > bool kube_horizontalpodautoscaler_status_current_replicas{{{HPA}}}'),
        ('CrashLoop',f'max(kube_pod_container_status_waiting_reason{{{POD},reason="CrashLoopBackOff"}})'),
        ('Target down','max(up == bool 0)'),('Release BLOCKED',f'mlops_v2_release_phase{{{JOB},phase="BLOCKED"}}'),('Stale snapshot',f'1 - mlops_v2_release_snapshot_fresh{{{JOB}}}')],12,63,12,9,'stat')
    alerts['fieldConfig']['defaults'].update(color={'mode':'thresholds'},thresholds={'mode':'absolute','steps':[{'color':'green','value':None},{'color':'red','value':1}]})
    panel('Release state','Current phase and bounded pending states; durable state does not alone prove backend readiness.', [('{{phase}}',f'mlops_v2_release_phase{{{JOB}}} == 1'),('{{status}}',f'mlops_v2_pending_releases{{{JOB}}}'),('Elapsed seconds',f'mlops_v2_release_elapsed_seconds{{{JOB}}}')],0,72,8,8,'table',table=True)['transformations']=[]
    panel('Release phase timeline','One active phase at a time; metric disappears on stale snapshot.', [('{{phase}}',f'mlops_v2_release_phase{{{JOB}}} == 1')],8,72,8,8,'state-timeline')
    panel('Router state freshness','Age of last successful state read and snapshot freshness. Age>10s is stale; missing telemetry is not healthy.', [('Age',f'credit_risk_router_state_age_seconds{{{JOB}}}'),('Snapshot fresh',f'mlops_v2_release_snapshot_fresh{{{JOB}}}')],16,72,8,8,unit='s')
    panel('Release audit / stage timestamps','Latest retained durable audit timestamps; these are historical timestamps, not newly replayed events. New-event annotations use exporter counters with startup baseline.', [('{{weight}}%',f'mlops_v2_stage_promotion_timestamp_seconds{{{JOB}}}'),('{{outcome}}',f'mlops_v2_last_outcome_timestamp_seconds{{{JOB}}}')],0,80,12,8,'table','dateTimeAsIso',True)['transformations']=[]
    panel('Required telemetry source presence','1=family present; 0=absent from selected datasource. This is source presence, not serving health.',[(name,f'(count({metric}) > bool 0) or (absent({metric}) * 0)') for name,metric in [('KSM','kube_node_info'),('cAdvisor','container_cpu_usage_seconds_total{image!=""}'),('Router',f'credit_risk_router_requests_total{{{JOB}}}'),('Release',f'mlops_v2_release_snapshot_fresh{{{JOB}}}'),('KServe exporter','mlops_v2_state_exporter_up')]],12,80,12,8,'stat')
    links=panel('Evidence / runbook','Static links and explicit operational limits. No synthetic traffic or release mutation from dashboard.',[],0,88,24,6,'text')
    links['options']={'mode':'markdown','content':'### Read-only operational view\n[Runbook](https://github.com/Ngducanh05/ml_template/blob/feat/canary-monitoring-demo/docs/mlops-v2-dashboard.md) · [Evidence](https://github.com/Ngducanh05/ml_template/blob/feat/canary-monitoring-demo/docs/mlops-v2-dashboard.md#evidence-and-live-acceptance)\n\n**NO DATA is not healthy.** Check source presence before interpreting zero traffic. ISVC Ready alone is not complete rollout proof. EC2 estimate excludes EKS/EBS/network/tax/T3 credits. Never use this view to infer an autoscaling PASS without a recorded desired/ready transition.'}
    variables=[dict(name='DS_PROMETHEUS',label='Prometheus',type='datasource',query='prometheus',refresh=1,current={},options=[]),
        dict(name='namespace',label='Namespace',type='query',datasource=DS,query='label_values(kube_pod_info, namespace)',refresh=1,current=dict(text='production',value='production'),options=[]),
        dict(name='model',label='Model',type='constant',query='credit-risk',current=dict(text='credit-risk',value='credit-risk')),
        dict(name='service',label='Pod prefix',type='custom',query='credit-risk-stable-v2,credit-risk-canary-v2,credit-risk-router-v2,model-',includeAll=True,allValue='.*',multi=True,current=dict(text='All',value='$__all'),options=[]),
        dict(name='node',label='Node',type='query',datasource=DS,query='label_values(kube_node_info,node)',refresh=1,includeAll=True,allValue='.*',multi=True,current=dict(text='All',value='$__all'),options=[]),
        dict(name='track',label='Track',type='custom',query='stable,canary',includeAll=True,allValue='stable|canary',multi=True,current=dict(text='All',value='$__all'),options=[]),
        dict(name='node_hourly_usd',label='EC2 USD/hour (approx.)',type='textbox',query='0.0528',current=dict(text='0.0528',value='0.0528'))]
    annotations=[dict(name='Canary weight change',datasource=DS,enable=True,iconColor='#E0B400',expr=f'changes(credit_risk_router_weight_percent{{{JOB}}}[1m]) > 0',step='15s',titleFormat='Canary weight changed'),
        dict(name='New release audit event',datasource=DS,enable=True,iconColor='#73BF69',expr='increase(mlops_v2_release_event_total{reason=~"release_started|stage_promoted|stable_committed|release_completed|rollback_completed|release_blocked"}[1m]) > 0',step='15s',titleFormat='{{reason}}',textFormat='stage {{stage}}')]
    return dict(uid='mlops-v2-infra-serving',title='MLOps v2 \u2014 Infrastructure & Serving',tags=['mlops-v2','infrastructure','serving'],schemaVersion=39,version=1,editable=False,style='dark',timezone='browser',refresh='30s',time={'from':'now-1h','to':'now'},templating={'list':variables},annotations={'list':annotations},panels=PANELS)


def main():
    dashboard=build();directory=ROOT/'k8s/monitoring/grafana';(directory/'dashboards').mkdir(parents=True,exist_ok=True)
    encoded=json.dumps(dashboard,ensure_ascii=False,indent=2)+'\n'
    (directory/'dashboards/mlops_v2_infra.json').write_text(encoded,encoding='utf-8')
    cm=dict(apiVersion='v1',kind='ConfigMap',metadata=dict(name='mlops-v2-infra-dashboard',namespace='monitoring',labels={'grafana_dashboard':'1'},annotations={'grafana_folder':'MLOps v2'}),data={'mlops_v2_infra.json':encoded})
    # JSON is valid YAML; deterministic and no YAML quoting ambiguity.
    (directory/'dashboard-mlops-v2-infra.configmap.yaml').write_text(json.dumps(cm,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Rendered',len(PANELS),'panels')

if __name__=='__main__': main()
