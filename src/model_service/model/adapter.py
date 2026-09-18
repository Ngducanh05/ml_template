from __future__ import annotations

import logging
import math
import platform
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from pathlib import Path
from typing import Protocol

import joblib
import sklearn
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from model_service.model.contract import (
    VERIFIED_FEATURE_NAMES,
    VERIFIED_TARGET_NAMES,
)
from model_service.model.errors import (
    InferenceError,
    InputContractError,
    ModelLoadError,
)

logger = logging.getLogger(__name__)

VERIFIED_FEATURE_UNITS = "centimetres"
VERIFIED_CLASS_IDS = (0, 1, 2)
VERIFIED_MIN_BATCH_SIZE = 1
VERIFIED_MAX_BATCH_SIZE = 100
VERIFIED_MODEL_VERSION = "iris-demo-v1"
VERIFIED_PYTHON_VERSION = "3.12.14"
VERIFIED_SKLEARN_VERSION = "1.7.2"
VERIFIED_SIGNATURE_INPUT = (
    "instances: list of objects with four positive finite numbers"
)
VERIFIED_SIGNATURE_OUTPUT = "predictions: list of Iris class names"


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    model_version: str
    feature_names: tuple[str, ...]
    feature_units: str
    class_ids: tuple[int, ...]
    target_names: tuple[str, ...]
    min_batch_size: int
    max_batch_size: int
    input_signature: str
    output_signature: str
    python_version: str
    sklearn_version: str


class ModelAdapter(Protocol):
    def load(self) -> None: ...

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]: ...

    def is_ready(self) -> bool: ...


class SklearnJoblibAdapter:
    """Adapter for the repository's trusted Iris sklearn artifact."""

    def __init__(self, artifact_path: Path) -> None:
        self._artifact_path = artifact_path
        self._model: Pipeline | None = None
        self._contract: ArtifactContract | None = None
        self._class_name_by_id: dict[int, str] | None = None

    @property
    def contract(self) -> ArtifactContract:
        if self._contract is None:
            raise ModelLoadError("Artifact contract is unavailable before load")
        return self._contract

    def load(self) -> None:
        logger.info("Loading model artifact", extra={"component": "model_adapter"})
        self._model = None
        self._contract = None
        self._class_name_by_id = None
        try:
            artifact = joblib.load(self._artifact_path)
            model, contract, class_name_by_id = self._validate_artifact(artifact)
        except ModelLoadError:
            logger.exception(
                "Model artifact validation failed",
                extra={"component": "model_adapter"},
            )
            raise
        except Exception as exc:
            logger.exception(
                "Model artifact load failed", extra={"component": "model_adapter"}
            )
            raise ModelLoadError("Model artifact could not be loaded") from exc

        self._model = model
        self._contract = contract
        self._class_name_by_id = class_name_by_id
        logger.info(
            "Model artifact ready",
            extra={
                "component": "model_adapter",
                "model_version": contract.model_version,
            },
        )

    def is_ready(self) -> bool:
        return (
            self._model is not None
            and self._contract is not None
            and self._class_name_by_id is not None
        )

    def predict(self, instances: Sequence[Mapping[str, float]]) -> list[str]:
        if not self.is_ready():
            raise InferenceError("Model adapter is not ready")

        rows = self._prepare_rows(instances)
        try:
            raw_predictions = self._model.predict(rows)  # type: ignore[union-attr]
        except Exception as exc:
            logger.exception(
                "Model runtime prediction failed",
                extra={"component": "model_adapter", "batch_size": len(rows)},
            )
            raise InferenceError("Model runtime prediction failed") from exc

        predictions = list(raw_predictions)
        if len(predictions) != len(rows):
            raise InferenceError("Model returned an unexpected number of predictions")

        class_name_by_id = self._class_name_by_id
        if class_name_by_id is None:
            raise InferenceError("Model class mapping is unavailable")

        normalized: list[str] = []
        for prediction in predictions:
            if isinstance(prediction, bool) or not isinstance(prediction, Integral):
                raise InferenceError("Model returned an unsupported prediction type")
            class_id = int(prediction)
            try:
                class_name = class_name_by_id[class_id]
            except KeyError as exc:
                raise InferenceError("Model returned an unknown class index")
            normalized.append(class_name)
        return normalized

    def _prepare_rows(
        self, instances: Sequence[Mapping[str, float]]
    ) -> list[list[float]]:
        contract = self.contract
        batch_size = len(instances)
        if not contract.min_batch_size <= batch_size <= contract.max_batch_size:
            raise InputContractError(
                f"Batch size must be between {contract.min_batch_size} "
                f"and {contract.max_batch_size}"
            )

        expected = set(contract.feature_names)
        rows: list[list[float]] = []
        for instance in instances:
            if set(instance) != expected:
                raise InputContractError("Instance feature names do not match the model")

            row: list[float] = []
            for feature_name in contract.feature_names:
                value = instance[feature_name]
                if isinstance(value, bool) or not isinstance(value, Real):
                    raise InputContractError("All feature values must be numeric")
                normalized = float(value)
                if not math.isfinite(normalized) or normalized <= 0:
                    raise InputContractError(
                        "All feature values must be positive finite numbers"
                    )
                row.append(normalized)
            rows.append(row)
        return rows

    @classmethod
    def _validate_artifact(
        cls, artifact: object
    ) -> tuple[Pipeline, ArtifactContract, dict[int, str]]:
        if not isinstance(artifact, Mapping):
            raise ModelLoadError("Artifact root must be a mapping")

        model = cls._required_value(artifact, "model", "Artifact")
        metadata = cls._required_value(artifact, "metadata", "Artifact")
        if not isinstance(model, Pipeline):
            raise ModelLoadError("Artifact model must be an sklearn Pipeline")
        if not isinstance(metadata, Mapping):
            raise ModelLoadError("Artifact metadata must be a mapping")

        feature_names = cls._string_sequence(
            cls._required_value(metadata, "feature_names", "Artifact metadata"),
            "feature_names",
        )
        target_names = cls._string_sequence(
            cls._required_value(metadata, "target_names", "Artifact metadata"),
            "target_names",
        )
        feature_units = cls._required_value(
            metadata, "feature_units", "Artifact metadata"
        )
        signature = cls._required_value(metadata, "signature", "Artifact metadata")
        model_version = cls._required_value(
            metadata, "model_version", "Artifact metadata"
        )
        artifact_sklearn_version = cls._required_value(
            metadata, "sklearn_version", "Artifact metadata"
        )
        artifact_python_version = cls._required_value(
            metadata, "python_version", "Artifact metadata"
        )

        if not isinstance(signature, Mapping):
            raise ModelLoadError("Artifact signature must be a mapping")
        input_signature = cls._required_value(
            signature, "input", "Artifact signature"
        )
        output_signature = cls._required_value(
            signature, "output", "Artifact signature"
        )
        min_batch_size = cls._required_value(
            signature, "min_batch_size", "Artifact signature"
        )
        max_batch_size = cls._required_value(
            signature, "max_batch_size", "Artifact signature"
        )

        if feature_names != VERIFIED_FEATURE_NAMES:
            raise ModelLoadError("Artifact feature contract is unsupported")
        if target_names != VERIFIED_TARGET_NAMES:
            raise ModelLoadError("Artifact target contract is unsupported")
        if feature_units != VERIFIED_FEATURE_UNITS:
            raise ModelLoadError("Artifact feature units are unsupported")
        if model_version != VERIFIED_MODEL_VERSION:
            raise ModelLoadError("Artifact model version is invalid")
        if artifact_python_version != VERIFIED_PYTHON_VERSION:
            raise ModelLoadError("Artifact Python version is invalid")
        if artifact_sklearn_version != VERIFIED_SKLEARN_VERSION:
            raise ModelLoadError("Artifact sklearn version is invalid")
        if artifact_sklearn_version != sklearn.__version__:
            raise ModelLoadError("Artifact and runtime sklearn versions do not match")
        if (
            isinstance(min_batch_size, bool)
            or isinstance(max_batch_size, bool)
            or not isinstance(min_batch_size, int)
            or not isinstance(max_batch_size, int)
            or min_batch_size != VERIFIED_MIN_BATCH_SIZE
            or max_batch_size != VERIFIED_MAX_BATCH_SIZE
        ):
            raise ModelLoadError("Artifact batch-size contract is invalid")
        if input_signature != VERIFIED_SIGNATURE_INPUT:
            raise ModelLoadError("Artifact input signature is invalid")
        if output_signature != VERIFIED_SIGNATURE_OUTPUT:
            raise ModelLoadError("Artifact output signature is invalid")

        steps = tuple(step for _name, step in model.steps)
        if (
            len(steps) != 2
            or not isinstance(steps[0], StandardScaler)
            or not isinstance(steps[1], LogisticRegression)
        ):
            raise ModelLoadError("Artifact model pipeline is unsupported")

        n_features_in = getattr(model, "n_features_in_", None)
        if (
            isinstance(n_features_in, bool)
            or not isinstance(n_features_in, Integral)
            or int(n_features_in) != len(feature_names)
        ):
            raise ModelLoadError("Artifact model feature count does not match metadata")

        raw_class_ids = getattr(model, "classes_", None)
        if raw_class_ids is None:
            raise ModelLoadError("Artifact model classes are malformed")
        try:
            raw_class_id_items = tuple(raw_class_ids)
        except TypeError as exc:
            raise ModelLoadError("Artifact model classes are malformed") from exc
        if any(
            isinstance(class_id, bool) or not isinstance(class_id, Integral)
            for class_id in raw_class_id_items
        ):
            raise ModelLoadError("Artifact model classes are malformed")
        class_ids = tuple(int(class_id) for class_id in raw_class_id_items)
        if len(class_ids) != len(target_names):
            raise ModelLoadError("Artifact class count does not match target names")
        if class_ids != VERIFIED_CLASS_IDS:
            raise ModelLoadError("Artifact model classes are unsupported")

        class_name_by_id = dict(zip(class_ids, target_names, strict=True))

        runtime_python = platform.python_version()
        if artifact_python_version != runtime_python:
            logger.warning(
                "Artifact Python version differs from runtime",
                extra={
                    "component": "model_adapter",
                    "artifact_python_version": artifact_python_version,
                    "runtime_python_version": runtime_python,
                },
            )

        return model, ArtifactContract(
            model_version=model_version,
            feature_names=feature_names,
            feature_units=feature_units,
            class_ids=class_ids,
            target_names=target_names,
            min_batch_size=min_batch_size,
            max_batch_size=max_batch_size,
            input_signature=input_signature,
            output_signature=output_signature,
            python_version=artifact_python_version,
            sklearn_version=artifact_sklearn_version,
        ), class_name_by_id

    @staticmethod
    def _required_value(
        values: Mapping[str, object], key: str, context: str
    ) -> object:
        try:
            return values[key]
        except KeyError as exc:
            raise ModelLoadError(f"{context} is missing required field: {key}") from exc

    @staticmethod
    def _string_sequence(value: object, field_name: str) -> tuple[str, ...]:
        if (
            not isinstance(value, (list, tuple))
            or not value
            or any(not isinstance(item, str) or not item for item in value)
        ):
            raise ModelLoadError(
                f"Artifact metadata field {field_name} must be a non-empty string list"
            )
        return tuple(value)
