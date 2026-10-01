import logging
from copy import deepcopy
from pathlib import Path

import joblib
import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from model_service.model.adapter import SklearnJoblibAdapter
from model_service.model.errors import InferenceError, InputContractError, ModelLoadError


ARTIFACT_PATH = Path(__file__).resolve().parents[2] / "artifacts" / "model.joblib"


def _write_artifact(tmp_path: Path, artifact: object) -> Path:
    artifact_path = tmp_path / "model.joblib"
    joblib.dump(artifact, artifact_path)
    return artifact_path


@pytest.mark.parametrize(
    "model_version", ["iris-demo-v1", "iris-demo-v2", "2026.10.01+build-1"]
)
def test_load_accepts_compatible_model_identity_and_logs_it(
    tmp_path: Path, caplog, model_version: str
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"]["model_version"] = model_version
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with caplog.at_level(logging.INFO, logger="model_service.model.adapter"):
        adapter.load()

    assert adapter.is_ready()
    assert adapter.contract.model_version == model_version
    assert any(
        record.message == "Model artifact ready"
        and record.model_version == model_version
        for record in caplog.records
    )
    instance = {
        "sepal_length": 5.1,
        "sepal_width": 3.5,
        "petal_length": 1.4,
        "petal_width": 0.2,
    }
    assert adapter.predict([instance]) == ["setosa"]


@pytest.mark.parametrize(
    "model_version",
    [
        None, 2, True, [], {}, "", " ", " iris-demo-v2", "iris-demo-v2 ",
        "iris demo v2", "iris-demo-v2\n", "iris\x00v2", "iris/v2", ".",
    ],
)
def test_load_rejects_invalid_model_identity(tmp_path: Path, model_version: object) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"]["model_version"] = model_version
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError, match="model version is invalid"):
        adapter.load()

    assert not adapter.is_ready()


@pytest.mark.parametrize("model_version", ["iris-demo-v1", "iris-demo-v2"])
@pytest.mark.parametrize(
    ("container", "field", "value", "message"),
    [
        (
            "metadata", "feature_names",
            ["sepal_width", "sepal_length", "petal_length", "petal_width"],
            "feature contract",
        ),
        (
            "metadata", "target_names", ["virginica", "versicolor", "setosa"],
            "target contract",
        ),
        ("metadata", "feature_units", "millimetres", "feature units"),
        ("metadata", "python_version", "3.11.9", "Python version"),
        ("metadata", "sklearn_version", "1.7.1", "sklearn version"),
        ("signature", "input", "instances: list of numbers", "input signature"),
        ("signature", "output", "predictions: list of integers", "output signature"),
        ("signature", "min_batch_size", 0, "batch-size contract"),
        ("signature", "min_batch_size", True, "batch-size contract"),
        ("signature", "max_batch_size", 101, "batch-size contract"),
    ],
)
def test_model_identity_does_not_bypass_contract_validation(
    tmp_path: Path,
    model_version: str,
    container: str,
    field: str,
    value: object,
    message: str,
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    metadata = artifact["metadata"]
    metadata["model_version"] = model_version
    target = metadata if container == "metadata" else metadata["signature"]
    target[field] = value
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError, match=message):
        adapter.load()

    assert not adapter.is_ready()


@pytest.mark.parametrize("model_version", ["iris-demo-v1", "iris-demo-v2"])
def test_load_rejects_incompatible_sklearn_runtime(
    tmp_path: Path, monkeypatch, model_version: str
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"]["model_version"] = model_version
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))
    monkeypatch.setattr("model_service.model.adapter.sklearn.__version__", "1.7.1")

    with pytest.raises(ModelLoadError, match="runtime sklearn versions do not match"):
        adapter.load()

    assert not adapter.is_ready()


def test_load_warns_for_python_runtime_difference(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"]["model_version"] = "iris-demo-v2"
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))
    monkeypatch.setattr(
        "model_service.model.adapter.platform.python_version", lambda: "3.12.15"
    )

    adapter.load()

    assert adapter.is_ready()
    assert "Artifact Python version differs from runtime" in caplog.text


@pytest.mark.parametrize("model_version", ["iris-demo-v1", "iris-demo-v2"])
@pytest.mark.parametrize(
    "pipeline_kind", ["not_pipeline", "unsupported_steps", "unfitted", "wrong_classes"]
)
def test_model_identity_does_not_bypass_pipeline_validation(
    tmp_path: Path, model_version: str, pipeline_kind: str
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"]["model_version"] = model_version
    if pipeline_kind == "not_pipeline":
        artifact["model"] = LogisticRegression()
        message = "must be an sklearn Pipeline"
    elif pipeline_kind == "unsupported_steps":
        artifact["model"] = Pipeline(artifact["model"].steps[1:])
        message = "pipeline is unsupported"
    elif pipeline_kind == "unfitted":
        artifact["model"] = Pipeline([
            ("scaler", StandardScaler()), ("classifier", LogisticRegression())
        ])
        message = "feature count"
    else:
        artifact["model"].steps[-1][1].classes_ = np.array([0, 1, 99])
        message = "classes are unsupported"
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError, match=message):
        adapter.load()

    assert not adapter.is_ready()


@pytest.mark.parametrize("missing_key", ["model", "metadata"])
def test_load_fails_fast_when_required_artifact_member_is_missing(
    tmp_path: Path, missing_key: str
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    del artifact[missing_key]
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError) as caught:
        adapter.load()

    assert caught.value.__cause__ is not None
    assert not adapter.is_ready()


@pytest.mark.parametrize(
    ("container", "missing_key"),
    [
        ("metadata", "feature_names"),
        ("metadata", "feature_units"),
        ("metadata", "target_names"),
        ("metadata", "python_version"),
        ("metadata", "sklearn_version"),
        ("metadata", "model_version"),
        ("metadata", "signature"),
        ("signature", "input"),
        ("signature", "output"),
        ("signature", "min_batch_size"),
        ("signature", "max_batch_size"),
    ],
)
def test_load_fails_fast_when_required_contract_field_is_missing(
    tmp_path: Path, container: str, missing_key: str
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    if container == "metadata":
        del artifact["metadata"][missing_key]
    else:
        del artifact["metadata"]["signature"][missing_key]
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError) as caught:
        adapter.load()

    assert caught.value.__cause__ is not None
    assert not adapter.is_ready()


def test_load_rejects_model_feature_count_mismatch(tmp_path: Path) -> None:
    artifact = deepcopy(joblib.load(ARTIFACT_PATH))
    artifact["model"].steps[0][1].n_features_in_ = 3
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError, match="feature count"):
        adapter.load()

    assert not adapter.is_ready()


def test_load_rejects_class_count_mismatch(tmp_path: Path) -> None:
    artifact = deepcopy(joblib.load(ARTIFACT_PATH))
    artifact["model"].steps[-1][1].classes_ = np.array([0, 1])
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError, match="class count"):
        adapter.load()

    assert not adapter.is_ready()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("feature_names", "sepal_length"),
        ("target_names", ["setosa", "versicolor", 3]),
        ("feature_units", None),
        ("signature", []),
    ],
)
def test_load_rejects_malformed_metadata(
    tmp_path: Path, field: str, value: object
) -> None:
    artifact = joblib.load(ARTIFACT_PATH)
    artifact["metadata"][field] = value
    adapter = SklearnJoblibAdapter(_write_artifact(tmp_path, artifact))

    with pytest.raises(ModelLoadError):
        adapter.load()

    assert not adapter.is_ready()


def test_load_preserves_deserialization_failure_cause(tmp_path: Path) -> None:
    artifact_path = tmp_path / "corrupt.joblib"
    artifact_path.write_bytes(b"not a joblib artifact")
    adapter = SklearnJoblibAdapter(artifact_path)

    with pytest.raises(ModelLoadError) as caught:
        adapter.load()

    assert caught.value.__cause__ is not None
    assert not adapter.is_ready()


@pytest.mark.parametrize("batch_size", [0, 101])
def test_predict_enforces_verified_batch_bounds(batch_size: int) -> None:
    adapter = SklearnJoblibAdapter(ARTIFACT_PATH)
    adapter.load()
    instance = {
        "sepal_length": 5.1,
        "sepal_width": 3.5,
        "petal_length": 1.4,
        "petal_width": 0.2,
    }

    with pytest.raises(InputContractError, match="Batch size"):
        adapter.predict([instance] * batch_size)


def test_predict_rejects_unknown_raw_class_id(monkeypatch) -> None:
    adapter = SklearnJoblibAdapter(ARTIFACT_PATH)
    adapter.load()
    model = adapter._model
    assert model is not None
    monkeypatch.setattr(model, "predict", lambda _rows: np.array([99]))

    with pytest.raises(InferenceError, match="unknown class"):
        adapter.predict(
            [
                {
                    "sepal_length": 5.1,
                    "sepal_width": 3.5,
                    "petal_length": 1.4,
                    "petal_width": 0.2,
                }
            ]
        )
