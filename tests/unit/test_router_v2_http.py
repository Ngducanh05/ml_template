import json
import threading
from urllib.request import Request, urlopen
from unittest.mock import Mock
from control_plane.router import Routing, BoundedServer
from control_plane.router_v2 import handler
from control_plane.state_v2 import initial_state, MODEL
from test_controller_v2 import OLD
from test_processor_v2 import FP


def test_real_http_v2_proxy_preserves_request_and_upstream_response():
    routing = Routing(clock=lambda: 0)
    state = initial_state(OLD, FP); state['route'] = dict(weight=100, revision=1)
    routing.set(state)
    body = b'{ "inputs":[{"name":"MonthlyIncome","data":[null]}] }'
    sender = Mock(return_value=(200, b'{"outputs":[{"datatype":"INT64","data":[0,0,0]}]}', 'application/json'))
    server = BoundedServer(('127.0.0.1', 0), handler(routing, lambda: {'canary': 'http://backend'}, sender))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        request = Request(f'http://127.0.0.1:{server.server_port}/v2/models/{MODEL}/infer', data=body, headers={'Content-Type':'application/json'})
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.headers['X-Release-Track'] == 'canary'
            assert json.load(response)['outputs'][0]['data'] == [0,0,0]
        assert sender.call_args.args == ('http://backend', 'canary', body, 'application/json')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
