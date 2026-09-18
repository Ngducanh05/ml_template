from collections.abc import Mapping, Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from model_service.core.config import Settings
from model_service.main import create_app
from model_service.model.errors import InferenceError, InputContractError


VALID_INSTANCE = {
    "sepal_length": 5.1,
    "sepal_width": 3.5,
    "petal_length": 1.4,
    "petal_width": 0.2,
}


class PredictAdapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.received: Sequence[Mapping[str, float]] | None = None

    def load(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        self.received = instances
        if self.error is not None:
            raise self.error
        return ["setosa"] * len(instances)


def _client(adapter: PredictAdapter) -> TestClient:
    settings = Settings(model_path=Path("unused.joblib"), model_name="test-model")
    app = create_app(settings=settings, adapter_factory=lambda _settings: adapter)
    return TestClient(app)


def test_predict_returns_public_predictions_for_valid_payload() -> None:
    adapter = PredictAdapter()

    with _client(adapter) as client:
        response = client.post(
            "/api/v1/predict", json={"instances": [VALID_INSTANCE]}
        )

    assert response.status_code == 200
    assert response.json() == {"predictions": ["setosa"]}
    assert adapter.received == [VALID_INSTANCE]


def test_predict_returns_stable_validation_error() -> None:
    with _client(PredictAdapter()) as client:
        response = client.post("/api/v1/predict", json={"instances": []})

    assert response.status_code == 422
    assert response.json() == {
        "code": "INVALID_REQUEST",
        "message": "Request payload is invalid",
    }


def test_predict_maps_adapter_contract_errors_without_leaking_details() -> None:
    adapter = PredictAdapter(InputContractError("sensitive implementation detail"))

    with _client(adapter) as client:
        response = client.post(
            "/api/v1/predict", json={"instances": [VALID_INSTANCE]}
        )

    assert response.status_code == 422
    assert response.json() == {
        "code": "INPUT_CONTRACT_ERROR",
        "message": "Input is not valid for the model",
    }


def test_predict_maps_runtime_failures_without_leaking_details() -> None:
    adapter = PredictAdapter(InferenceError("sensitive implementation detail"))

    with _client(adapter) as client:
        response = client.post(
            "/api/v1/predict", json={"instances": [VALID_INSTANCE]}
        )

    assert response.status_code == 500
    assert response.json() == {
        "code": "INFERENCE_FAILED",
        "message": "Inference could not be completed",
    }
