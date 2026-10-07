from pathlib import Path

import pytest

from control_plane.runtime_resolver import (
    ArtifactContract,
    RUNTIME_X_V1,
    RuntimeProfile,
    normalize_requirements,
    resolve_runtime,
    runtime_fingerprint,
)


RUNTIME = (
    "596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service"
    "@sha256:"
    "f1ae0d6c95d19b73adbfa72ae183ef6f207a2dc647bc2d4a1a36386a02ba5ae0"
)


def artifact(root: Path, *, python="3.12.14", requirements=None, marker="v1"):
    root.mkdir()
    (root / "python_env.yaml").write_text(
        f"python: {python}\n"
        "build_dependencies:\n"
        "- pip==26.2.1\n"
        "- setuptools\n"
        "- wheel\n"
        "dependencies:\n"
        "- -r requirements.txt\n",
        encoding="utf-8",
    )
    (root / "requirements.txt").write_text(
        requirements
        or "mlflow==3.14.0\n"
           "scikit-learn==1.7.2\n"
           "pandas==2.3.3\n"
           "numpy==2.5.3\n"
           "scipy==1.18.1\n"
           "skops==0.16.0\n",
        encoding="utf-8",
    )

    # These intentionally differ between model artifacts and MUST NOT influence
    # runtime compatibility.
    (root / "MLmodel").write_text(f"model_version: {marker}\n", encoding="utf-8")
    (root / "model.skops").write_bytes(marker.encode())
    return root


def test_same_runtime_contract_reuses_across_different_model_artifacts(tmp_path):
    first = ArtifactContract.from_artifact(
        artifact(tmp_path / "v3", marker="model-v3")
    )
    second = ArtifactContract.from_artifact(
        artifact(tmp_path / "v4", marker="completely-different-model-v4")
    )

    assert first == second
    fingerprint = runtime_fingerprint(first)
    assert runtime_fingerprint(second) == fingerprint

    assert resolve_runtime(second, {fingerprint: RUNTIME}) == {
        "action": "REUSE",
        "fingerprint": fingerprint,
        "runtime_image": RUNTIME,
    }


def test_requirement_order_spacing_and_name_separators_normalize():
    first = normalize_requirements(
        "mlflow==3.14.0\nscikit-learn==1.7.2\nnumpy==2.5.3\n"
    )
    second = normalize_requirements(
        " numpy == 2.5.3\nSCIKIT_LEARN == 1.7.2\nmlflow == 3.14.0\n"
    )
    assert first == second


def test_dependency_change_requires_different_runtime(tmp_path):
    original = ArtifactContract.from_artifact(artifact(tmp_path / "original"))
    changed = ArtifactContract.from_artifact(
        artifact(
            tmp_path / "changed",
            requirements=(
                "mlflow==3.15.0\n"
                "scikit-learn==1.7.2\n"
                "pandas==2.3.3\n"
                "numpy==2.5.3\n"
                "scipy==1.18.1\n"
                "skops==0.16.0\n"
            ),
        )
    )
    assert runtime_fingerprint(original) != runtime_fingerprint(changed)


def test_python_change_requires_different_runtime(tmp_path):
    original = ArtifactContract.from_artifact(
        artifact(tmp_path / "py312", python="3.12.14")
    )
    changed = ArtifactContract.from_artifact(
        artifact(tmp_path / "py313", python="3.13.1")
    )
    assert runtime_fingerprint(original) != runtime_fingerprint(changed)


def test_serving_profile_change_requires_different_runtime(tmp_path):
    contract = ArtifactContract.from_artifact(artifact(tmp_path / "artifact"))
    changed_profile = RuntimeProfile(
        mlserver_version=RUNTIME_X_V1.mlserver_version,
        mlserver_mlflow_version=RUNTIME_X_V1.mlserver_mlflow_version,
        platform=RUNTIME_X_V1.platform,
        build_profile=RUNTIME_X_V1.build_profile,
        serving_profile="different-serving-semantics",
    )
    assert runtime_fingerprint(contract) != runtime_fingerprint(
        contract, changed_profile
    )


def test_unknown_runtime_fingerprint_selects_build(tmp_path):
    contract = ArtifactContract.from_artifact(artifact(tmp_path / "artifact"))
    result = resolve_runtime(contract, {})
    assert result["action"] == "BUILD"
    assert result["fingerprint"].startswith("sha256:")
    assert "runtime_image" not in result


@pytest.mark.parametrize(
    "requirement",
    [
        "mlflow>=3.14.0",
        "mlflow",
        "-r other.txt",
        "git+https://example.invalid/repo.git",
        "mlflow==3.14.0; python_version >= '3.12'",
    ],
)
def test_ambiguous_requirements_fail_closed(requirement):
    with pytest.raises(ValueError, match="exact package==version"):
        normalize_requirements(requirement)


def test_conflicting_duplicate_dependency_fails_closed():
    with pytest.raises(ValueError, match="Conflicting exact pins"):
        normalize_requirements("mlflow==3.14.0\nMLFLOW==3.15.0\n")


def test_catalog_must_return_immutable_digest(tmp_path):
    contract = ArtifactContract.from_artifact(artifact(tmp_path / "artifact"))
    fingerprint = runtime_fingerprint(contract)

    with pytest.raises(ValueError, match="immutable repository@sha256"):
        resolve_runtime(
            contract,
            {fingerprint: "example.invalid/runtime:latest"},
        )
