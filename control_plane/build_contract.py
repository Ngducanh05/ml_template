"""Normalized CI build request; no model identity, credentials or builder effect."""
from .runtime_resolver import (ArtifactContract, RuntimeProfile, RUNTIME_X_V1, RUNTIME_CI_V2,
                               normalize_requirements, parse_python_version, runtime_fingerprint)


def build_request(contract, profile=RUNTIME_X_V1):
    return dict(schema_version=1, fingerprint=runtime_fingerprint(contract, profile),
                python_version=contract.python_version, requirements=list(contract.requirements),
                runtime_profile=profile.canonical())


def validate_request(data):
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("Unsupported build request")
    if set(data) != {"schema_version", "fingerprint", "python_version", "requirements", "runtime_profile"}:
        raise ValueError("Build request must contain technical fields only")
    if not isinstance(data["requirements"], list) or not all(isinstance(x, str) for x in data["requirements"]):
        raise ValueError("Invalid requirements")
    contract = ArtifactContract(parse_python_version("python: " + data["python_version"]),
                                normalize_requirements("\n".join(data["requirements"])))
    profile = RuntimeProfile(**data["runtime_profile"])
    # Only the reviewed serving/build profile is currently defined. New profiles
    # need an explicit recipe; accepting arbitrary strings would promise a build.
    if profile not in (RUNTIME_X_V1, RUNTIME_CI_V2) or build_request(contract, profile) != data:
        raise ValueError("Noncanonical or unsupported technical build request")
    return contract, profile
