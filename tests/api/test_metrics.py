from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

import model_service.metrics as metrics_module
from model_service.core.config import Settings
from model_service.main import create_app
from model_service.model.errors import InferenceError, InputContractError

VALID_INSTANCE = {
    "sepal_length": 5.1,
    "sepal_width": 3.5,
    "petal_length": 1.4,
    "petal_width": 0.2,
}
PREDICT = "/api/v1/predict"


class MetricsAdapter:
    def __init__(
        self,
        *,
        ready: bool = True,
        error: Exception | None = None,
        mismatched_count: bool = False,
    ) -> None:
        self.ready = ready
        self.error = error
        self.mismatched_count = mismatched_count
        self.calls = 0

    def load(self) -> None:
        pass

    def is_ready(self) -> bool:
        return self.ready

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.mismatched_count:
            return []
        return ["setosa"] * len(instances)


def _app(adapter: MetricsAdapter | None = None):
    return create_app(
        settings=Settings(model_path=Path("unused.joblib"), model_name="test-model"),
        adapter_factory=lambda _settings: adapter or MetricsAdapter(),
    )


def _samples(client: TestClient):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain;")
    return [
        sample
        for family in text_string_to_metric_families(response.text)
        for sample in family.samples
    ]


def _value(samples, name: str, labels: dict[str, str]) -> float:
    matches = [s.value for s in samples if s.name == name and s.labels == labels]
    assert len(matches) == 1, (name, labels, matches)
    return matches[0]


def _http_labels(status: int) -> dict[str, str]:
    return {"method": "POST", "route": PREDICT, "status": str(status)}


def test_successful_batch_counts_one_request_and_each_item_once() -> None:
    adapter = MetricsAdapter()
    with TestClient(_app(adapter)) as client:
        response = client.post(PREDICT, json={"instances": [VALID_INSTANCE] * 3})
        assert response.json() == {"predictions": ["setosa"] * 3}
        assert response.status_code == 200
        samples = _samples(client)

    assert adapter.calls == 1
    assert _value(samples, "model_service_http_requests_total", _http_labels(200)) == 1
    assert (
        _value(
            samples,
            "model_service_http_request_duration_seconds_count",
            _http_labels(200),
        )
        == 1
    )
    assert (
        _value(
            samples,
            "model_service_http_request_duration_seconds_sum",
            _http_labels(200),
        )
        > 0
    )
    assert (
        _value(
            samples, "model_service_inference_requests_total", {"outcome": "success"}
        )
        == 1
    )
    assert (
        _value(samples, "model_service_inference_items_total", {"outcome": "success"})
        == 3
    )
    assert (
        _value(
            samples,
            "model_service_inference_duration_seconds_count",
            {"outcome": "success"},
        )
        == 1
    )
    assert (
        _value(
            samples,
            "model_service_inference_duration_seconds_sum",
            {"outcome": "success"},
        )
        > 0
    )
    assert (
        _value(
            samples,
            "model_service_inference_duration_seconds_bucket",
            {"outcome": "success", "le": "+Inf"},
        )
        == 1
    )


def test_schema_rejection_counts_http_error_without_an_inference_attempt() -> None:
    adapter = MetricsAdapter()
    with TestClient(_app(adapter)) as client:
        response = client.post(PREDICT, json={"instances": []})
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_REQUEST"
        samples = _samples(client)

    assert adapter.calls == 0
    assert _value(samples, "model_service_http_requests_total", _http_labels(422)) == 1
    assert (
        sum(
            s.value
            for s in samples
            if s.name == "model_service_inference_requests_total"
        )
        == 0
    )
    assert (
        sum(s.value for s in samples if s.name == "model_service_inference_items_total")
        == 0
    )


@pytest.mark.parametrize(
    ("adapter", "status", "outcome", "code"),
    [
        (
            MetricsAdapter(error=InputContractError("sensitive detail")),
            422,
            "input_error",
            "INPUT_CONTRACT_ERROR",
        ),
        (MetricsAdapter(ready=False), 503, "not_ready", "NOT_READY"),
        (
            MetricsAdapter(error=InferenceError("sensitive detail")),
            500,
            "error",
            "INFERENCE_FAILED",
        ),
        (MetricsAdapter(mismatched_count=True), 500, "error", "INFERENCE_FAILED"),
        (MetricsAdapter(error=ValueError("sensitive detail")), 500, "error", None),
    ],
)
def test_operational_failures_count_once_and_preserve_http_contract(
    adapter: MetricsAdapter, status: int, outcome: str, code: str | None
) -> None:
    with TestClient(_app(adapter), raise_server_exceptions=False) as client:
        response = client.post(PREDICT, json={"instances": [VALID_INSTANCE] * 2})
        assert response.status_code == status
        assert "sensitive detail" not in response.text
        if code is not None:
            assert response.json()["code"] == code
        samples = _samples(client)

    assert adapter.calls == (0 if outcome == "not_ready" else 1)
    assert (
        _value(samples, "model_service_http_requests_total", _http_labels(status)) == 1
    )
    assert (
        _value(
            samples,
            "model_service_http_request_duration_seconds_count",
            _http_labels(status),
        )
        == 1
    )
    assert (
        _value(samples, "model_service_inference_requests_total", {"outcome": outcome})
        == 1
    )
    assert (
        _value(samples, "model_service_inference_items_total", {"outcome": outcome})
        == 2
    )
    assert (
        _value(
            samples,
            "model_service_inference_duration_seconds_count",
            {"outcome": outcome},
        )
        == 1
    )
    assert (
        sum(
            s.value
            for s in samples
            if s.name == "model_service_inference_requests_total"
        )
        == 1
    )


def test_probes_and_scrapes_are_excluded_even_when_not_ready() -> None:
    with TestClient(_app(MetricsAdapter(ready=False))) as client:
        for _ in range(3):
            assert client.get("/health/live").status_code == 200
            assert client.get("/health/ready").status_code == 503
            client.get("/metrics/")
        samples = _samples(client)

    assert (
        sum(s.value for s in samples if s.name == "model_service_http_requests_total")
        == 0
    )
    assert not any(
        s.labels.get("route", "").startswith(("/metrics", "/health")) for s in samples
    )


def test_route_and_method_labels_cannot_contain_arbitrary_request_values() -> None:
    app = _app()

    @app.get("/observations/{item_id}")
    def observation(item_id: str):
        return {"found": True}

    with TestClient(app) as client:
        for item in ("private-item-a", "private-item-b"):
            assert (
                client.get(f"/observations/{item}?request_id={item}").status_code == 200
            )
            assert client.get(f"/missing/{item}").status_code == 404
            assert (
                client.request(f"CUSTOM-{item}", f"/missing/{item}").status_code == 404
            )
        samples = _samples(client)

    assert (
        _value(
            samples,
            "model_service_http_requests_total",
            {"method": "GET", "route": "/observations/{item_id}", "status": "200"},
        )
        == 2
    )
    assert (
        _value(
            samples,
            "model_service_http_requests_total",
            {"method": "GET", "route": "unmatched", "status": "404"},
        )
        == 2
    )
    assert (
        _value(
            samples,
            "model_service_http_requests_total",
            {"method": "OTHER", "route": "unmatched", "status": "404"},
        )
        == 2
    )
    assert not any("private-item" in str(s.labels) for s in samples)


def test_factories_have_isolated_registries_and_no_duplicate_registration() -> None:
    first, second = _app(), _app()
    with TestClient(first) as client:
        client.post(PREDICT, json={"instances": [VALID_INSTANCE]})
        first_samples = _samples(client)
    with TestClient(second) as client:
        second_samples = _samples(client)

    assert (
        _value(first_samples, "model_service_http_requests_total", _http_labels(200))
        == 1
    )
    assert (
        _value(second_samples, "model_service_http_requests_total", _http_labels(200))
        == 0
    )
    assert first.state.metrics.registry is not second.state.metrics.registry


def test_build_identity_uses_baked_file_and_actual_loaded_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity_path = tmp_path / "build-identity.json"
    identity_path.write_text(
        json.dumps(
            {
                "source_commit": "a" * 40,
                "source_digest": "sha256:" + "b" * 64,
                "source_dirty": True,
                "ignored_secret": "must-never-be-exported",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(metrics_module, "BUILD_IDENTITY_PATH", identity_path)
    monkeypatch.setenv("MODEL_NAME", "fake-model")
    monkeypatch.setenv("SOURCE_COMMIT", "fake-commit")
    app = create_app(
        settings=Settings(
            model_path=Path(__file__).resolve().parents[2] / "artifacts/model.joblib",
            model_name="fake-model",
        )
    )
    with TestClient(app) as client:
        samples = _samples(client)
        actual_version = app.state.inference_service._adapter.contract.model_version

    identity_samples = [s for s in samples if s.name == "model_service_build_info"]
    assert len(identity_samples) == 1
    assert identity_samples[0].labels == {
        "source_commit": "a" * 40,
        "source_digest": "sha256:" + "b" * 64,
        "dirty": "true",
        "model_version": actual_version,
    }
    assert actual_version != "fake-model"


@pytest.mark.parametrize(
    "contents",
    [
        None,
        "invalid json",
        "[]",
        '{"source_commit":"secret","source_digest":"secret","source_dirty":"secret"}',
    ],
)
def test_missing_or_invalid_identity_is_unknown_without_breaking_serving(
    contents: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity_path = tmp_path / "build-identity.json"
    if contents is not None:
        identity_path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(metrics_module, "BUILD_IDENTITY_PATH", identity_path)
    with TestClient(_app()) as client:
        assert (
            client.post(PREDICT, json={"instances": [VALID_INSTANCE]}).status_code
            == 200
        )
        samples = _samples(client)

    assert (
        _value(
            samples,
            "model_service_build_info",
            {
                "source_commit": "unknown",
                "source_digest": "unknown",
                "dirty": "unknown",
                "model_version": "unknown",
            },
        )
        == 1
    )
