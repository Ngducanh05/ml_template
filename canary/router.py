"""Deterministic weighted demo router; loopback defaults, configurable Kubernetes DNS."""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest, CONTENT_TYPE_LATEST

registry = CollectorRegistry()
weight_metric = Gauge('canary_router_weight_percent', 'Canary traffic percent', registry=registry)
requests = Counter('canary_router_requests_total', 'Proxy responses', ['track', 'status'], registry=registry)
for track in ('stable', 'canary'):
    for status in ('200', '422', '500', '502', '503'):
        requests.labels(track, status)
lock = threading.Lock()
weight, sequence = 10, 0
weight_metric.set(weight)

def choose_track(index, percent):
    return 'canary' if index * percent % 100 + percent >= 100 else 'stable'

def upstream_url(track):
    key = 'CANARY_CANARY_URL' if track == 'canary' else 'CANARY_STABLE_URL'
    port_key = 'CANARY_CANARY_PORT' if track == 'canary' else 'CANARY_STABLE_PORT'
    port = os.environ.get(port_key, '8082' if track == 'canary' else '8081')
    return os.environ.get(key, f'http://127.0.0.1:{port}').rstrip('/')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass

    def reply(self, status, data, content_type='application/json', track=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        if track: self.send_header('X-Lab-Route', track)
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass

    def do_GET(self):
        if self.path == '/__admin/state':
            with lock: data = json.dumps({'canary_weight': weight}).encode()
            return self.reply(200, data)
        if self.path == '/metrics':
            return self.reply(200, generate_latest(registry), CONTENT_TYPE_LATEST)
        if self.path == '/health/ready': return self.reply(200, b'{"status":"ready"}')
        self.reply(404, b'{}')

    def do_POST(self):
        global weight, sequence
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 1048576: raise ValueError()
            body = self.rfile.read(size)
            if self.path == '/__admin/weight':
                value = json.loads(body)['canary_weight']
                if type(value) is not int or value not in (0,10,25,50,100): raise ValueError()
                with lock:
                    weight, sequence = value, 0
                    weight_metric.set(value)
                return self.reply(200, json.dumps({'canary_weight': value}).encode())
        except (ValueError, KeyError, TypeError):
            return self.reply(400, b'{"error":"invalid request"}')
        if self.path != '/api/v1/predict': return self.reply(404, b'{}')
        with lock:
            track = choose_track(sequence, weight)
            sequence += 1
        try:
            req = Request(upstream_url(track) + '/api/v1/predict', data=body, headers={'Content-Type':'application/json'})
            with urlopen(req, timeout=2) as response:
                status, data = response.status, response.read()
        except HTTPError as exc:
            status, data = exc.code, exc.read()
        except (OSError, TimeoutError):
            status, data = 502, b'{"error":"upstream unavailable"}'
        requests.labels(track, str(status)).inc()
        self.reply(status, data, track=track)

if __name__ == '__main__':
    ThreadingHTTPServer((os.environ.get('CANARY_BIND_HOST', '127.0.0.1'),8080), Handler).serve_forever()
