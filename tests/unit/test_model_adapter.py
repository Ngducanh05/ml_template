from copy import deepcopy
from pathlib import Path

import joblib
import numpy as np
import pytest

from model_service.model.adapter import SklearnJoblibAdapter
from model_service.model.errors import InferenceError, InputContractError, ModelLoadError


ARTIFACT_PATH = Path(__file__).resolve().parents[2] / "artifacts" / "model.joblib"


def _write_artifact(tmp_path: Path, artifact: object) -> Path:
    artifact_path = tmp_path / "model.joblib"
    joblib.dump(artifact, artifact_path)
    return artifact_path


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
