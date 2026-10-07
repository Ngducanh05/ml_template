"""Validate the reviewed MLflow sklearn/skops serving surface without model bytes."""
import yaml
from .runtime_resolver import ArtifactContract


def validate_mlmodel(text, contract: ArtifactContract):
    model = yaml.safe_load(text)
    if not isinstance(model, dict): raise ValueError("Invalid MLmodel")
    flavors = model.get("flavors", {})
    if set(flavors) != {"python_function", "sklearn"}:
        raise ValueError("Unreviewed MLflow flavor")
    pyfunc, sklearn = flavors["python_function"], flavors["sklearn"]
    pins = dict(pin.split("==", 1) for pin in contract.requirements)
    expected = dict(loader_module="mlflow.sklearn", model_path="model.skops", predict_fn="predict",
                    python_version=contract.python_version)
    if any(pyfunc.get(k) != v for k, v in expected.items()):
        raise ValueError("Unsupported pyfunc contract")
    if pyfunc.get("code") is not None or sklearn.get("code") is not None:
        raise ValueError("Custom artifact code not approved")
    if sklearn.get("serialization_format") != "skops" or sklearn.get("pickled_model") != "model.skops":
        raise ValueError("Only reviewed skops serialization is allowed")
    if model.get("mlflow_version") != pins.get("mlflow") or sklearn.get("sklearn_version") != pins.get("scikit-learn"):
        raise ValueError("MLmodel and requirements mismatch")
    trusted = sklearn.get("skops_trusted_types", [])
    reviewed = {"sklearn.tree._tree.Tree", "sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor"}
    if not isinstance(trusted, list) or any(not isinstance(x, str) or x not in reviewed for x in trusted):
        raise ValueError("Unreviewed skops trusted type")
