"""Native V2 proxy process; legacy routers remain unchanged and separately deployed."""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from .router import Routing, BoundedServer, requests, latency
from .kserve_effects import LANES
from .state_v2 import MODEL
from .telemetry import log

MAX_BODY = 1_048_576


def internal_address(obj, lane):
    base = obj.get("status", {}).get("address", {}).get("url", "")
    u = urlsplit(base)
    allowed = {LANES[lane] + "-predictor.production.svc", LANES[lane] + "-predictor.production.svc.cluster.local"}
    if u.scheme != "http" or u.hostname not in allowed or u.username or u.query or u.fragment or u.path not in ("", "/"):
        raise ValueError("Missing or unexpected internal KServe address")
    return base.rstrip("/")


def forward(base, lane, body, content_type, opener=urlopen):
    # KServe injects the InferenceService name as MLServer model name. Translate
    # only that URL component; the V2 request bytes and response are untouched.
    request = Request(base + f"/v2/models/{LANES[lane]}/infer", data=body,
                      headers={"Content-Type": content_type})
    try:
        try:
            response = opener(request, timeout=3)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                return 502, b'{"error":"response_too_large"}', "application/json"
            return response.status, data, response.headers.get("Content-Type", "application/json")
    except (OSError, TimeoutError):
        return 502, b'{"error":"backend_unavailable"}', "application/json"


def handler(routing, addresses, sender=forward):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup(); self.connection.settimeout(5)
        def log_message(self, *args): pass
        def reply(self, status, body, kind="application/json", track=None):
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if track: self.send_header("X-Release-Track", track)
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_GET(self):
            if self.path == "/metrics": return self.reply(200, generate_latest(), CONTENT_TYPE_LATEST)
            state = routing.status()
            if self.path == "/state": return self.reply(200, json.dumps(state).encode())
            if self.path == "/health/live": return self.reply(200, b'{}')
            if self.path == "/health/ready": return self.reply(200 if state["ready"] else 503, b'{}')
            self.reply(404, b'{}')
        def do_POST(self):
            if self.path != f"/v2/models/{MODEL}/infer": return self.reply(404, b'{}')
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    return self.reply(413, b'{}')
                body = self.rfile.read(size)
                if len(body) != size: return self.reply(400, b'{}')
            except (ValueError, TimeoutError): return self.reply(400, b'{}')
            track = routing.choose()
            base = addresses().get(track)
            if track is None or base is None: return self.reply(503, b'{"error":"release_state_unavailable"}')
            started = time.monotonic()
            status, data, kind = sender(base, track, body, self.headers.get("Content-Type", "application/json"))
            requests.labels(track, str(status)).inc()
            latency.labels(track, str(status)).observe(time.monotonic() - started)
            self.reply(status, data, kind, track)
    return Handler


def main():
    from .kube_v2 import KubeV2
    kube = KubeV2(); routing = Routing(); addresses = {}
    def sync():
        nonlocal addresses
        while True:
            try:
                discovered = {}
                for lane, name in LANES.items():
                    obj = kube.get("inferenceservices", name)
                    if obj: discovered[lane] = internal_address(obj, lane)
                state = kube.read_state()[1]
                if state["stable"] is None or "stable" not in discovered:
                    raise ValueError("Stable baseline unavailable")
                addresses = discovered
                routing.set(state)
            except Exception as exc:
                log("V2_ROUTE_STATE_UNAVAILABLE", error_type=type(exc).__name__)
            time.sleep(1)
    threading.Thread(target=sync, daemon=True).start()
    BoundedServer(("0.0.0.0", 8080), handler(routing, lambda: addresses)).serve_forever()


if __name__ == "__main__": main()
