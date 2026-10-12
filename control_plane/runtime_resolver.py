"""Resolve an artifact dependency contract to a reusable serving runtime.

The runtime fingerprint contains technical compatibility inputs only.
Model identity, model version, run ID, artifact URI and artifact bytes are
deliberately excluded.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Mapping


_PYTHON = re.compile(
    r"(?m)^python:\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)['\"]?\s*$"
)
_EXACT_PIN = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[0-9][A-Za-z0-9.!+_-]*)$"
)
_EXACT_RUNTIME = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def normalize_requirements(text: str) -> tuple[str, ...]:
    """Return deterministic exact pins and reject ambiguous build contracts."""
    pins: dict[str, str] = {}

    for lineno, original in enumerate(text.splitlines(), start=1):
        line = original.split("#", 1)[0].strip()
        if not line:
            continue

        compact = re.sub(r"\s+", "", line)
        match = _EXACT_PIN.fullmatch(compact)
        _require(
            match is not None,
            f"Unsupported requirement at line {lineno}; exact package==version pin required",
        )

        name = _normalize_name(match.group("name"))
        version = match.group("version")

        if name in pins:
            _require(
                pins[name] == version,
                f"Conflicting exact pins for dependency {name}",
            )
        pins[name] = version

    _require(bool(pins), "Artifact requirements.txt contains no runtime dependencies")
    return tuple(f"{name}=={pins[name]}" for name in sorted(pins))


def parse_python_version(text: str) -> str:
    versions = _PYTHON.findall(text)
    _require(
        len(versions) == 1,
        "python_env.yaml must contain exactly one top-level Python X.Y.Z version",
    )
    return versions[0]


@dataclass(frozen=True)
class ArtifactContract:
    python_version: str
    requirements: tuple[str, ...]

    @classmethod
    def from_artifact(cls, artifact_dir: Path) -> "ArtifactContract":
        root = artifact_dir.resolve()
        python_env = root / "python_env.yaml"
        requirements = root / "requirements.txt"

        _require(python_env.is_file(), "Artifact is missing python_env.yaml")
        _require(requirements.is_file(), "Artifact is missing requirements.txt")

        return cls(
            python_version=parse_python_version(
                python_env.read_text(encoding="utf-8-sig")
            ),
            requirements=normalize_requirements(
                requirements.read_text(encoding="utf-8-sig")
            ),
        )


@dataclass(frozen=True)
class RuntimeProfile:
    """Versioned identity of our generic serving/build implementation."""

    mlserver_version: str
    mlserver_mlflow_version: str
    platform: str
    build_profile: str
    serving_profile: str

    def canonical(self) -> dict[str, str]:
        return {
            "mlserver_version": self.mlserver_version,
            "mlserver_mlflow_version": self.mlserver_mlflow_version,
            "platform": self.platform,
            "build_profile": self.build_profile,
            "serving_profile": self.serving_profile,
        }


# Any material change to base image / hardening recipe / serving semantics MUST
# create a new build_profile or serving_profile. Never silently reuse this ID.
RUNTIME_X_V1 = RuntimeProfile(
    mlserver_version="1.7.1",
    mlserver_mlflow_version="1.7.1",
    platform="linux/amd64",
    build_profile="runtime-x-hardened-v1",
    serving_profile="mlserver-v2-parallel-workers-0",
)


# Versioned recipe: preserve all existing runtime-x fingerprints/catalog hits.
RUNTIME_CI_V2 = RuntimeProfile(
    mlserver_version="1.7.1", mlserver_mlflow_version="1.7.1", platform="linux/amd64",
    build_profile="runtime-ci-py31214-slim-v2",
    serving_profile="mlserver-v2-parallel-workers-0",
)


def runtime_fingerprint(
    contract: ArtifactContract,
    profile: RuntimeProfile = RUNTIME_X_V1,
) -> str:
    payload = {
        "schema_version": 1,
        "python_version": contract.python_version,
        "requirements": list(contract.requirements),
        "runtime_profile": profile.canonical(),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _validate_runtime_image(image: str) -> str:
    _require(
        isinstance(image, str) and _EXACT_RUNTIME.fullmatch(image) is not None,
        "Runtime catalog values must be immutable repository@sha256 references",
    )
    return image


def resolve_runtime(
    contract: ArtifactContract,
    catalog: Mapping[str, str],
    profile: RuntimeProfile = RUNTIME_X_V1,
) -> dict[str, str]:
    """Choose REUSE or BUILD without performing side effects."""
    fingerprint = runtime_fingerprint(contract, profile)
    existing = catalog.get(fingerprint)

    if existing is not None:
        return {
            "action": "REUSE",
            "fingerprint": fingerprint,
            "runtime_image": _validate_runtime_image(existing),
        }

    return {
        "action": "BUILD",
        "fingerprint": fingerprint,
    }
