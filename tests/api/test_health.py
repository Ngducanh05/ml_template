from collections.abc import Mapping, Sequence
from pathlib import Path

from fastapi.testclient import TestClient

from model_service.core.config import Settings
from model_service.main import create_app


class HealthAdapter:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self.load_calls = 0

    def load(self) -> None:
        self.load_calls += 1

    def is_ready(self) -> bool:
        return self.ready

    def predict(self, _instances: Sequence[Mapping[str, float]]) -> list[str]:
        return []


def _settings() -> Settings:
    return Settings(model_path=Path("unused.joblib"), model_name="test-model")


def test_lifespan_loads_adapter_once_and_health_endpoints_are_ready() -> None:
    adapter = HealthAdapter()
    app = create_app(settings=_settings(), adapter_factory=lambda _settings: adapter)

    with TestClient(app) as client:
        assert client.get("/health/live").json() == {"status": "live"}
        assert client.get("/health/ready").json() == {"status": "ready"}
        assert client.get("/health/ready").status_code == 200

    assert adapter.load_calls == 1


def test_readiness_returns_503_when_adapter_is_not_ready() -> None:
    adapter = HealthAdapter(ready=False)
    app = create_app(settings=_settings(), adapter_factory=lambda _settings: adapter)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "code": "NOT_READY",
        "message": "Model service is not ready",
    }
