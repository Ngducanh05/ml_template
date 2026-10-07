import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import threading
import time
from prometheus_client import generate_latest,CONTENT_TYPE_LATEST

def log(event,**fields):
    print(json.dumps({'time':time.time(),'event':event,**fields},allow_nan=False),flush=True)

def serve(port,ready=lambda:True):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            if self.path=='/metrics':
                data=generate_latest(); status=200; kind=CONTENT_TYPE_LATEST
            elif self.path in ('/health/live','/health/ready'):
                status=200 if self.path.endswith('live') or ready() else 503
                data=b'{"status":"ok"}' if status==200 else b'{"status":"not_ready"}';kind='application/json'
            else: status=404;data=b'{}';kind='application/json'
            self.send_response(status);self.send_header('Content-Type',kind)
            self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('0.0.0.0',port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    return server
