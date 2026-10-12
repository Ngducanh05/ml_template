import copy
import joblib
from sklearn.datasets import load_iris
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression

stable = joblib.load("canary/artifacts/stable.joblib")

X, y = load_iris(return_X_y=True)

candidate_model = Pipeline([
    ("scaler", StandardScaler()),
    ("model", LogisticRegression(
        C=0.20,
        max_iter=500,
        random_state=42
    )),
])

candidate_model.fit(X, y)

candidate = {
    "model": candidate_model,
    "metadata": copy.deepcopy(stable["metadata"]),
}

candidate["metadata"]["model_version"] = "iris-canary-v2"

joblib.dump(candidate, "canary/artifacts/canary-v2.joblib")
print(candidate["metadata"]["model_version"])
