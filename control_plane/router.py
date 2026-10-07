"""Credit-risk /predict gateway. Route comes from durable state, never an admin POST."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen
from prometheus_client import Counter,Gauge,Histogram,generate_latest,CONTENT_TYPE_LATEST
from canary.router import choose_track
from .telemetry import log

requests=Counter('credit_risk_router_requests_total','Real forwarded predictions',['track','status'])
latency=Histogram('credit_risk_router_request_duration_seconds','Upstream response latency',['track','status'],buckets=(.005,.01,.025,.05,.075,.1,.25,.5,1,2,5))
weight_metric=Gauge('credit_risk_router_weight_percent','Effective Canary percentage')
state_age=Gauge('credit_risk_router_state_age_seconds','Age of last successful durable state read')
for track in ('stable','canary'):
    for status in ('200','422','500','502','503','504'):
        requests.labels(track,status);latency.labels(track,status)

class Routing:
    def __init__(self,clock=time.time):
        self.clock=clock;self.lock=threading.Lock();self.state=None;self.updated=0;self.sequence=0;self.revision=-1
    def set(self,state):
        with self.lock:
            if state['route']['revision']!=self.revision:
                self.sequence=0;self.revision=state['route']['revision']
            self.state=state;self.updated=self.clock()
    def status(self):
        with self.lock:
            age=self.clock()-self.updated;state_age.set(age)
            if not self.state: return dict(ready=False,weight=0,revision=-1)
            active=self.state['active'];lease=self.state.get('lease')
            ready=age<=10 and (not active or (lease and lease['until']>self.clock()))
            weight=self.state['route']['weight'] if ready else 0
            weight_metric.set(weight)
            return dict(ready=bool(ready),weight=weight,revision=self.revision)
    def choose(self):
        status=self.status()
        if not status['ready']: return None
        with self.lock:
            track=choose_track(self.sequence,status['weight']);self.sequence+=1;return track

class BoundedServer(ThreadingHTTPServer):
    daemon_threads=True
    def __init__(self,*args,**kwargs): super().__init__(*args,**kwargs);self.slots=threading.BoundedSemaphore(64)
    def process_request(self,request,address):
        if not self.slots.acquire(blocking=False):
            request.close();return
        super().process_request(request,address)
    def process_request_thread(self,*args):
        try: super().process_request_thread(*args)
        finally: self.slots.release()

def main():
    from .kube import Kube
    kube=Kube();routing=Routing()
    def sync():
        while True:
            try: routing.set(kube.read_state()[1])
            except Exception as exc: log('ROUTE_STATE_UNAVAILABLE',error_type=type(exc).__name__)
            time.sleep(1)
    threading.Thread(target=sync,daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def setup(self): super().setup();self.connection.settimeout(5)
        def log_message(self,*args): pass
        def reply(self,status,data,kind='application/json',track=None):
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)))
            if track: self.send_header('X-Release-Track',track)
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError,ConnectionResetError): pass
        def do_GET(self):
            if self.path=='/metrics': return self.reply(200,generate_latest(),CONTENT_TYPE_LATEST)
            status=routing.status()
            if self.path=='/state': return self.reply(200,json.dumps(status).encode())
            if self.path=='/health/live': return self.reply(200,b'{"status":"ok"}')
            if self.path=='/health/ready': return self.reply(200 if status['ready'] else 503,json.dumps(status).encode())
            self.reply(404,b'{}')
        def do_POST(self):
            if self.path!='/predict': return self.reply(404,b'{}')
            try:
                size=int(self.headers.get('Content-Length','0'))
                if not 0<size<=1048576: return self.reply(413,b'{"error":"body_size"}')
                data=self.rfile.read(size)
                if len(data)!=size: return self.reply(400,b'{"error":"truncated_body"}')
                json.loads(data)
            except (ValueError,TimeoutError): return self.reply(400,b'{"error":"invalid_request"}')
            track=routing.choose()
            if track is None: return self.reply(503,b'{"error":"release_state_unavailable"}')
            started=time.monotonic()
            try:
                request=Request(f'http://credit-risk-classifier-{track}:8080/predict',data=data,headers={'Content-Type':'application/json'})
                with urlopen(request,timeout=3) as response:
                    status=response.status;body=response.read(1048577);kind=response.headers.get('Content-Type','application/json')
                if len(body)>1048576: status=502;body=b'{"error":"response_too_large"}'
            except HTTPError as exc: status=exc.code;body=exc.read(1048576);kind='application/json';exc.close()
            except (OSError,TimeoutError): status=502;body=b'{"error":"backend_unavailable"}';kind='application/json'
            requests.labels(track,str(status)).inc();latency.labels(track,str(status)).observe(time.monotonic()-started)
            self.reply(status,body,kind,track)
    BoundedServer(('0.0.0.0',8080),Handler).serve_forever()

if __name__=='__main__': main()
