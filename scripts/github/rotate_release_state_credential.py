"""Create and optionally store one short-lived Release v2 Kubernetes credential.

The command never prints the service-account token or the encoded credential.
It verifies the credential against the live API before it can be stored.
"""
from __future__ import annotations

import argparse
import base64
import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any
from urllib.request import Request, urlopen

from control_plane.authorization_backend import AuthorizationBackend


REPOSITORY = "Ngducanh05/ml_template"
NAMESPACE = "production"
STATE_NAME = "model-release-state-v2"
CONTEXT = "ml-template-dev-sg"
ROLE_CONFIG = {
    "reader": ("release-state-reader-v2", "RELEASE_STATE_READER_B64"),
    "writer": ("release-state-authorizer-v2", "RELEASE_STATE_WRITER_B64"),
}


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    expires_at: int
    remaining_seconds: int


def _run(*args: str, input_text: str | None = None) -> str:
    result = subprocess.run(args, input=input_text, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Command failed ({args[0]}), exit={result.returncode}")
    return result.stdout.strip()


def _decode_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except Exception as exc:
        raise ValueError("TokenRequest returned an invalid JWT") from exc
    if not isinstance(value, dict):
        raise ValueError("TokenRequest JWT payload must be an object")
    return value


def validate_token(token: str, service_account: str, minimum_ttl: int, now: int | None = None) -> TokenClaims:
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("TokenRequest did not return a JWT")
    payload = _decode_segment(parts[1])
    expected_subject = f"system:serviceaccount:{NAMESPACE}:{service_account}"
    if payload.get("sub") != expected_subject:
        raise ValueError("TokenRequest JWT has the wrong service-account subject")
    audiences = payload.get("aud", [])
    if isinstance(audiences, str):
        audiences = [audiences]
    if "https://kubernetes.default.svc" not in audiences:
        raise ValueError("TokenRequest JWT has the wrong audience")
    current = int(time.time()) if now is None else now
    expires_at = payload.get("exp")
    issued_at = payload.get("iat")
    if not isinstance(expires_at, int) or not isinstance(issued_at, int) or issued_at > current + 60:
        raise ValueError("TokenRequest JWT has invalid timestamps")
    remaining = expires_at - current
    if remaining < minimum_ttl:
        raise ValueError(f"Token TTL is insufficient: {remaining}s remaining")
    return TokenClaims(expected_subject, expires_at, remaining)


def cluster_config(context: str) -> tuple[str, str]:
    raw = _run("kubectl", "--context", context, "config", "view", "--raw", "--minify", "-o", "json")
    cluster = json.loads(raw)["clusters"][0]["cluster"]
    if "certificate-authority-data" not in cluster:
        raise ValueError("Kubernetes context must embed certificate-authority-data")
    return cluster["server"], cluster["certificate-authority-data"]


def credential(server: str, ca_data: str, token: str) -> dict[str, str]:
    return {
        "server": server,
        "certificate_authority_data": ca_data,
        "token": token,
        "namespace": NAMESPACE,
        "state_name": STATE_NAME,
    }


def _self_access(config: dict[str, str], verb: str, resource: str, name: str, *, group: str = "", subresource: str | None = None) -> bool:
    attrs: dict[str, str] = {"namespace": NAMESPACE, "verb": verb, "group": group, "resource": resource, "name": name}
    if subresource:
        attrs["subresource"] = subresource
    body = json.dumps({
        "apiVersion": "authorization.k8s.io/v1",
        "kind": "SelfSubjectAccessReview",
        "spec": {"resourceAttributes": attrs},
    }).encode()
    backend = AuthorizationBackend(config)
    request = Request(
        config["server"].rstrip("/") + "/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
        method="POST",
        data=body,
        headers={"Authorization": "Bearer " + config["token"], "Content-Type": "application/json"},
    )
    with urlopen(request, context=backend.context, timeout=15) as response:
        return bool(json.load(response).get("status", {}).get("allowed"))


def verify_permissions(config: dict[str, str], role: str) -> None:
    expected = {
        ("get", "configmaps", STATE_NAME, "", None): True,
        ("update", "configmaps", STATE_NAME, "", None): role == "writer",
        ("get", "configmaps", "model-release-state", "", None): False,
        ("get", "configmaps", "unrelated", "", None): False,
        ("get", "secrets", "unrelated", "", None): False,
        ("create", "jobs", "unrelated", "batch", None): False,
        ("create", "pods", "unrelated", "", "exec"): False,
    }
    for (verb, resource, name, group, subresource), allowed in expected.items():
        actual = _self_access(config, verb, resource, name, group=group, subresource=subresource)
        if actual is not allowed:
            raise PermissionError(f"Unexpected RBAC result for {verb} {resource}/{name}: {actual}")


def encode_credential(config: dict[str, str]) -> str:
    raw = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def store_secret(role: str, encoded: str, confirmed_writer: bool) -> None:
    _, secret_name = ROLE_CONFIG[role]
    if role == "writer" and not confirmed_writer:
        raise ValueError("Writer rotation requires --confirm-writer-rotation")
    args = ["gh", "secret", "set", secret_name, "--repo", REPOSITORY]
    if role == "writer":
        args.extend(["--env", "production"])
    _run(*args, input_text=encoded)


def summarize_state(state: dict[str, Any]) -> dict[str, Any]:
    candidates = [item for item in state["pending"] if isinstance(item.get("pair"), dict)]
    pending = max(candidates, key=lambda item: int(item["pair"]["model_version"])) if candidates else None
    return {
        "stable_model_version": state["stable"]["model_version"],
        "candidate_model_version": pending["pair"]["model_version"] if pending else None,
        "candidate_status": pending["status"] if pending else None,
        "active": state["active"] is not None,
        "traffic_weight": state["route"]["weight"],
    }


def rotate(role: str, context: str, duration: int, minimum_ttl: int, store: bool, confirmed_writer: bool) -> dict[str, Any]:
    if duration < minimum_ttl or minimum_ttl < 600 or duration > 7200:
        raise ValueError("Require 600 <= minimum TTL <= requested duration <= 7200 seconds")
    service_account, secret_name = ROLE_CONFIG[role]
    token = _run("kubectl", "--context", context, "-n", NAMESPACE, "create", "token", service_account, f"--duration={duration}s")
    claims = validate_token(token, service_account, minimum_ttl)
    server, ca_data = cluster_config(context)
    config = credential(server, ca_data, token)
    _, state = AuthorizationBackend(config, writable=role == "writer").read_state()
    verify_permissions(config, role)
    encoded = encode_credential(config)
    if store:
        store_secret(role, encoded, confirmed_writer)
    return {
        "status": "CREDENTIAL_STORED" if store else "CREDENTIAL_LIVE_VERIFIED",
        "role": role,
        "secret_name": secret_name,
        "secret_scope": "production environment" if role == "writer" else "repository",
        "subject": claims.subject,
        "expires_at_unix": claims.expires_at,
        "remaining_seconds": claims.remaining_seconds,
        "state": summarize_state(state),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=sorted(ROLE_CONFIG), required=True)
    parser.add_argument("--context", default=CONTEXT)
    parser.add_argument("--duration-seconds", type=int, default=3600)
    parser.add_argument("--minimum-remaining-seconds", type=int, default=1800)
    parser.add_argument("--store", action="store_true")
    parser.add_argument("--confirm-writer-rotation", action="store_true")
    args = parser.parse_args()
    result = rotate(args.role, args.context, args.duration_seconds, args.minimum_remaining_seconds, args.store, args.confirm_writer_rotation)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
