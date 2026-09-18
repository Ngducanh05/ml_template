from collections.abc import Mapping, Sequence

import pytest

from model_service.model.errors import InferenceError, NotReadyError
from model_service.services.inference import InferenceService


class StubAdapter:
    def __init__(self, predictions: list[str], *, ready: bool = True) -> None:
        self.predictions = predictions
        self.ready = ready
        self.received: Sequence[Mapping[str, float]] | None = None

    def load(self) -> None:
        self.ready = True

    def is_ready(self) -> bool:
        return self.ready

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        self.received = instances
        return self.predictions


def test_service_delegates_prediction_to_ready_adapter() -> None:
    adapter = StubAdapter(["setosa"])
    service = InferenceService(adapter=adapter, model_name="test-model")
    instances = [{"feature": 1.0}]

    assert service.predict(instances) == ["setosa"]
    assert adapter.received == instances
    assert service.model_name == "test-model"


def test_service_rejects_prediction_when_adapter_is_not_ready() -> None:
    service = InferenceService(
        adapter=StubAdapter([], ready=False), model_name="test-model"
    )

    with pytest.raises(NotReadyError, match="not ready"):
        service.predict([{"feature": 1.0}])


def test_service_rejects_prediction_count_mismatch() -> None:
    service = InferenceService(adapter=StubAdapter([]), model_name="test-model")

    with pytest.raises(InferenceError, match="count"):
        service.predict([{"feature": 1.0}])
