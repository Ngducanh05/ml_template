import importlib.util
from io import BytesIO
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "service_load", Path(__file__).resolve().parents[2] / "scripts/service_load.py"
)
load = importlib.util.module_from_spec(spec)
spec.loader.exec_module(load)


class Response(BytesIO):
    status = 200


@pytest.mark.parametrize("body,expected", [
    (b'{"predictions":["setosa"]}', "200"),
    (b'not-json', "contract_error"),
    (b'{"predictions":[]}', "contract_error"),
])
def test_request_checks_response_and_disables_connection_reuse(monkeypatch, body, expected):
    def open_request(request, timeout):
        assert request.get_header("Connection") == "close"
        assert timeout == 2
        return Response(body)
    monkeypatch.setattr(load, "urlopen", open_request)
    result, seconds = load.request("http://model-service.dev.svc.cluster.local:8000",
                                   json.dumps({"instances": [load.INSTANCE]}).encode(), 2)
    assert result == expected
    assert seconds >= 0


def test_no_requests_is_not_healthy_measurement():
    assert load.summarize([])["p95_seconds"] is None


def test_client_error_and_latency_accounting():
    result = load.summarize([("200", .01)] * 19 + [("503", 2.0)])
    assert result["requests"] == 20 and result["errors"] == 1
    assert result["p95_seconds"] == .01
    assert result["max_seconds"] == 2.0


def test_invalid_duration_rejected_before_load(monkeypatch):
    monkeypatch.setattr("sys.argv", ["service_load.py", "--namespace", "dev", "--duration", "601"])
    with pytest.raises(SystemExit) as exc:
        load.main()
    assert exc.value.code == 2
