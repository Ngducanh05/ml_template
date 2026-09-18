import joblib
from pprint import pprint

artifact = joblib.load("artifacts/model.joblib")

model = artifact["model"]
metadata = artifact["metadata"]

print("MODEL")
print("type:", type(model))
print("n_features_in_:", getattr(model, "n_features_in_", None))
print("classes_:", getattr(model, "classes_", None))

print("\nPIPELINE")
for name, step in model.named_steps.items():
    print(f"- {name}: {type(step)}")

print("\nCONTRACT METADATA")

for key in [
    "model_version",
    "dataset",
    "training_rows",
    "feature_names",
    "feature_units",
    "target_names",
    "python_version",
    "sklearn_version",
    "purpose",
]:
    print(f"\n{key}:")
    pprint(metadata.get(key))

print("\nSIGNATURE")
pprint(metadata.get("signature"))