from __future__ import annotations

from collections.abc import Mapping, Sequence
from time import perf_counter

from model_service.metrics import ServingMetrics
from model_service.model.adapter import ModelAdapter
from model_service.model.errors import InferenceError, InputContractError, NotReadyError


class InferenceService:
    def __init__(
        self,
        adapter: ModelAdapter,
        model_name: str,
        metrics: ServingMetrics | None = None,
    ) -> None:
        self._adapter = adapter
        self.model_name = model_name
        self._metrics = metrics

    def is_ready(self) -> bool:
        return self._adapter.is_ready()

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        started = perf_counter()
        outcome = "error"
        try:
            if not self.is_ready():
                raise NotReadyError("Model service is not ready")

            predictions = self._adapter.predict(instances)
            if len(predictions) != len(instances):
                raise InferenceError("Prediction count does not match input count")
            outcome = "success"
            return predictions
        except InputContractError:
            outcome = "input_error"
            raise
        except NotReadyError:
            outcome = "not_ready"
            raise
        finally:
            if self._metrics is not None:
                self._metrics.record_inference(
                    outcome, len(instances), perf_counter() - started
                )
