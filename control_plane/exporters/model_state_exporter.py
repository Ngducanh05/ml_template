"""Read-only KServe status and new audit events; release gauges stay in Router-v2."""
import math
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from prometheus_client import CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily
from control_plane.kserve_effects import LANES

REASONS = frozenset(('release_started','stage_promoted','stable_committed','release_completed','rollback_completed','release_blocked'))
STAGES = frozenset(('10','25','50','100'))


class ModelStateCollector:
    def __init__(self, api, clock=time.time, max_age=45):
        self.api, self.clock, self.max_age = api, clock, max_age
        self.lock = threading.Lock()
        self.rows = []; self.updated = None; self.ok = False
        self.watermark = None; self.events = Counter()

    def poll(self):
        try:
            rows = []
            for lane, name in LANES.items():
                obj = self.api.get('inferenceservices', name)
                if obj is None:
                    continue
                status = obj.get('status', {})
                conditions = status.get('conditions', [])
                ready = next((x.get('status') for x in conditions if x.get('type') == 'Ready'), None)
                revision = status.get('latestReadyRevision') or status.get('components', {}).get('predictor', {}).get('latestReadyRevision') or 'N/A'
                if not isinstance(revision, str) or not re.fullmatch(r'[A-Za-z0-9./_-]{1,128}', revision):
                    revision = 'N/A'
                mode = obj.get('metadata', {}).get('annotations', {}).get('serving.kserve.io/deploymentMode', 'Unknown')
                if mode not in ('Standard', 'Knative', 'RawDeployment'): mode = 'Unknown'
                rows.append(dict(lane=lane, name=name, ready={'True':1,'False':0}.get(ready),
                    generation=obj['metadata']['generation'], observed=status.get('observedGeneration'), revision=revision, mode=mode))
            _, state = self.api.read_state()
            audit = state.get('audit', [])
            audit = [x for x in audit if isinstance(x.get('time'), (int,float)) and math.isfinite(x['time']) and x['time'] >= 0]
            latest = max([x['time'] for x in audit] + [self.watermark or 0])
            with self.lock:
                if self.watermark is not None:
                    for row in audit:
                        if row['time'] > self.watermark:
                            reason = row.get('reason') if row.get('reason') in REASONS else 'other'
                            stage = str(row.get('previous_weight')) if reason == 'stage_promoted' else 'none'
                            if stage not in STAGES: stage = 'none'
                            self.events[(reason,stage)] += 1
                self.watermark = latest
                self.rows = rows; self.ok = True; self.updated = self.clock()
        except Exception:
            with self.lock: self.ok = False
            # Never emit arbitrary exception text, API response, token or state.

    def collect(self):
        with self.lock:
            rows, events = list(self.rows), dict(self.events)
            fresh = self.ok and self.updated is not None and 0 <= self.clock()-self.updated <= self.max_age
        up = GaugeMetricFamily('mlops_v2_state_exporter_up', 'Successful fresh read of both KServe lanes and CAS state')
        up.add_metric([], int(fresh)); yield up
        if not fresh: return
        labels = ['namespace','lane','name']
        for suffix, field in [('ready','ready'),('generation','generation'),('observed_generation','observed')]:
            metric = GaugeMetricFamily('mlops_v2_kserve_isvc_'+suffix, 'KServe '+suffix+'; Ready alone is not rollout proof', labels=labels)
            for row in rows:
                if row[field] is not None: metric.add_metric(['production',row['lane'],row['name']],row[field])
            yield metric
        info = GaugeMetricFamily('mlops_v2_kserve_isvc_info', 'Current KServe mode and reported revision; N/A when absent', labels=labels+['latest_ready_revision','deployment_mode'])
        for row in rows: info.add_metric(['production',row['lane'],row['name'],row['revision'],row['mode']],1)
        yield info
        counter = CounterMetricFamily('mlops_v2_release_event', 'New audit rows since exporter startup; historical rows are baseline only', labels=['reason','stage'])
        # Pre-create the finite label domain so first observed events have a baseline scrape.
        for reason in sorted(REASONS | {'other'}):
            for stage in (sorted(STAGES) if reason=='stage_promoted' else ['none']):
                counter.add_metric([reason,stage],events.get((reason,stage),0))
        counter.add_metric(['stage_promoted','none'],events.get(('stage_promoted','none'),0))
        yield counter


def main():
    from control_plane.kube_v2 import KubeV2
    collector = ModelStateCollector(KubeV2())
    registry = CollectorRegistry(); registry.register(collector)
    def poll():
        while True: collector.poll(); time.sleep(15)
    threading.Thread(target=poll,daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path not in ('/metrics','/health/live'): self.send_error(404); return
            body = generate_latest(registry) if self.path=='/metrics' else b'{}'
            self.send_response(200); self.send_header('Content-Type',CONTENT_TYPE_LATEST if self.path=='/metrics' else 'application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
        def setup(self): super().setup(); self.connection.settimeout(5)
    HTTPServer(('0.0.0.0',9093),Handler).serve_forever()


if __name__=='__main__': main()
