from __future__ import annotations

from collections.abc import Mapping, Sequence

from model_service.model.adapter import ModelAdapter
from model_service.model.errors import InferenceError, NotReadyError


class InferenceService:
    def __init__(self, adapter: ModelAdapter, model_name: str) -> None:
        self._adapter = adapter
        self.model_name = model_name

    def is_ready(self) -> bool:
        return self._adapter.is_ready()

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        if not self.is_ready():
            raise NotReadyError("Model service is not ready")

        predictions = self._adapter.predict(instances)
        if len(predictions) != len(instances):
            raise InferenceError("Prediction count does not match input count")
        return predictions
