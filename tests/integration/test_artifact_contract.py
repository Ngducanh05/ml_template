from pathlib import Path

import joblib
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from model_service.model.adapter import SklearnJoblibAdapter


ARTIFACT_PATH = Path(__file__).resolve().parents[2] / "artifacts" / "model.joblib"


def test_artifact_exposes_the_verified_contract() -> None:
    artifact = joblib.load(ARTIFACT_PATH)

    assert set(artifact) >= {"model", "metadata"}
    model = artifact["model"]
    metadata = artifact["metadata"]

    assert isinstance(model, Pipeline)
    assert [type(step) for _name, step in model.steps] == [
        StandardScaler,
        LogisticRegression,
    ]
    assert model.n_features_in_ == 4
    assert model.classes_.tolist() == [0, 1, 2]

    assert metadata["feature_names"] == [
        "sepal_length",
        "sepal_width",
        "petal_length",
        "petal_width",
    ]
    assert metadata["feature_units"] == "centimetres"
    assert metadata["target_names"] == ["setosa", "versicolor", "virginica"]
    assert metadata["python_version"] == "3.12.14"
    assert metadata["sklearn_version"] == "1.7.2"
    assert metadata["model_version"] == "iris-demo-v1"
    assert metadata["signature"] == {
        "input": "instances: list of objects with four positive finite numbers",
        "min_batch_size": 1,
        "max_batch_size": 100,
        "output": "predictions: list of Iris class names",
    }


def test_adapter_normalizes_by_metadata_order_and_returns_public_names() -> None:
    adapter = SklearnJoblibAdapter(ARTIFACT_PATH)
    adapter.load()
    canonical = {
        "sepal_length": 5.1,
        "sepal_width": 3.5,
        "petal_length": 1.4,
        "petal_width": 0.2,
    }
    reversed_order = dict(reversed(tuple(canonical.items())))

    predictions = adapter.predict([canonical, reversed_order])

    assert len(predictions) == 2
    assert predictions[0] == predictions[1]
    assert all(name in adapter.contract.target_names for name in predictions)


def test_adapter_does_not_preprocess_before_the_pipeline(monkeypatch) -> None:
    adapter = SklearnJoblibAdapter(ARTIFACT_PATH)
    adapter.load()
    model = adapter._model
    assert model is not None
    original_predict = model.predict
    received_rows: list[list[list[float]]] = []

    def capture_predict(rows: list[list[float]]):
        received_rows.append(rows)
        return original_predict(rows)

    monkeypatch.setattr(model, "predict", capture_predict)
    instance = {
        "petal_width": 0.2,
        "sepal_width": 3.5,
        "petal_length": 1.4,
        "sepal_length": 5.1,
    }

    adapter.predict([instance])

    assert received_rows == [[[5.1, 3.5, 1.4, 0.2]]]
