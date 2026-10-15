"""Fail-closed GitHub OIDC reader for the exact Release State v2 object."""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
from pathlib import Path
import re
import stat
import time

from .authorization_backend import AuthorizationBackend
from .state_v2 import STATE_NAME, Store


ACCOUNT = "596432515999"
REGION = "ap-southeast-1"
CLUSTER_NAME = "ml-template-dev-sg"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER_NAME}"
ROLE_NAME = "ml-template-v2-k8s-reader"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/{ROLE_NAME}"
NAMESPACE = "production"
MAX_SNAPSHOT_BYTES = 600_000
MAX_SNAPSHOT_AGE_SECONDS = 300
ASSUMED_ROLE = re.compile(
    rf"arn:aws:sts::{ACCOUNT}:assumed-role/{ROLE_NAME}/[A-Za-z0-9+=,.@_-]{{1,128}}\Z"
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_identity(sts):
    identity = sts.get_caller_identity()
    require(set(identity) >= {"Account", "Arn"}, "Malformed STS identity")
    require(identity["Account"] == ACCOUNT and ASSUMED_ROLE.fullmatch(identity["Arn"]),
            "Unexpected Kubernetes Reader identity")
    return identity


def cluster_metadata(eks):
    response = eks.describe_cluster(name=CLUSTER_NAME)
    cluster = response.get("cluster") if isinstance(response, dict) else None
    require(isinstance(cluster, dict), "Malformed EKS cluster metadata")
    require(cluster.get("name") == CLUSTER_NAME and cluster.get("arn") == CLUSTER_ARN,
            "Unexpected EKS cluster identity")
    require(cluster.get("status") == "ACTIVE", "EKS cluster is not ACTIVE")
    endpoint = cluster.get("endpoint")
    ca = cluster.get("certificateAuthority", {}).get("data")
    require(isinstance(endpoint, str) and isinstance(ca, str), "Incomplete EKS cluster metadata")
    from urllib.parse import urlsplit
    parsed = urlsplit(endpoint)
    require(parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".eks.amazonaws.com")
            and parsed.path in ("", "/") and not parsed.username and not parsed.password
            and not parsed.query and not parsed.fragment, "Unexpected EKS API endpoint")
    try:
        base64.b64decode(ca, validate=True)
    except Exception as exc:
        raise ValueError("Invalid EKS certificate authority") from exc
    return endpoint, ca


def eks_token(session):
    credentials = session.get_credentials()
    require(credentials is not None, "AWS credentials are unavailable")
    frozen = credentials.get_frozen_credentials()
    require(bool(frozen.access_key and frozen.secret_key and frozen.token),
            "Temporary GitHub OIDC credentials required")
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    request = AWSRequest(
        method="GET",
        url=f"https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        headers={"x-k8s-aws-id": CLUSTER_NAME},
    )
    SigV4QueryAuth(frozen, "sts", REGION, expires=60).add_auth(request)
    encoded = base64.urlsafe_b64encode(request.url.encode()).decode().rstrip("=")
    return "k8s-aws-v1." + encoded


def verify_aws_path(session):
    require(os.environ.get("AWS_REGION", REGION) == REGION, "Unexpected AWS region")
    identity = verify_identity(session.client("sts", region_name=REGION))
    print("OIDC_K8S_READER_IDENTITY_PASS")
    endpoint, ca = cluster_metadata(session.client("eks", region_name=REGION))
    print("OIDC_EKS_DESCRIBE_CLUSTER_PASS")
    token = eks_token(session)
    print("OIDC_K8S_TOKEN_GENERATION_PASS")
    return identity, endpoint, ca, token


def fetch_snapshot(session, *, backend_cls=AuthorizationBackend, now=time.time):
    identity, endpoint, ca, token = verify_aws_path(session)
    config = {
        "server": endpoint,
        "certificate_authority_data": ca,
        "token": token,
        "namespace": NAMESPACE,
        "state_name": STATE_NAME,
    }
    revision, state = backend_cls(config).read_state()
    Store.validate(state)
    print("OIDC_KUBERNETES_API_GET_PASS")
    return {
        "schema_version": 1,
        "source": {
            "account": identity["Account"],
            "role_arn": ROLE_ARN,
            "cluster_arn": CLUSTER_ARN,
        },
        "namespace": NAMESPACE,
        "state_name": STATE_NAME,
        "resource_version": revision,
        "created_at": int(now()),
        "state": state,
    }


def _safe_snapshot_path(path):
    root_value = os.environ.get("RUNNER_TEMP", "")
    require(root_value, "RUNNER_TEMP is required")
    root = Path(root_value).resolve(strict=True)
    candidate = Path(path)
    require(candidate.is_absolute(), "Snapshot path must be absolute")
    resolved_parent = candidate.parent.resolve(strict=True)
    require(resolved_parent == root or root in resolved_parent.parents,
            "Snapshot path must remain under RUNNER_TEMP")
    if candidate.exists() or candidate.is_symlink():
        require(not candidate.is_symlink() and candidate.resolve().parent == resolved_parent,
                "Unsafe snapshot path")
    return candidate


def write_snapshot(path, snapshot):
    target = _safe_snapshot_path(path)
    payload = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
    require(len(payload) <= MAX_SNAPSHOT_BYTES, "Release state snapshot exceeds bound")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def read_snapshot(path=None, *, now=time.time):
    target = _safe_snapshot_path(path or os.environ.get("RELEASE_STATE_READER_SNAPSHOT", ""))
    info = target.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_size <= MAX_SNAPSHOT_BYTES,
            "Invalid release state snapshot file")
    snapshot = json.loads(target.read_text(encoding="utf-8"))
    require(set(snapshot) == {"schema_version", "source", "namespace", "state_name",
                              "resource_version", "created_at", "state"},
            "Invalid release state snapshot contract")
    require(snapshot["schema_version"] == 1 and snapshot["source"] == {
        "account": ACCOUNT, "role_arn": ROLE_ARN, "cluster_arn": CLUSTER_ARN,
    }, "Unexpected release state snapshot source")
    require(snapshot["namespace"] == NAMESPACE and snapshot["state_name"] == STATE_NAME,
            "Unexpected release state snapshot target")
    require(isinstance(snapshot["resource_version"], str)
            and 1 <= len(snapshot["resource_version"]) <= 128,
            "Invalid release state resourceVersion")
    created = snapshot["created_at"]
    current = int(now())
    require(type(created) is int
            and current - MAX_SNAPSHOT_AGE_SECONDS <= created <= current + 30,
            "Expired release state snapshot")
    Store.validate(snapshot["state"])
    return copy.deepcopy(snapshot)


class SnapshotBackend:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def read_state(self):
        return self.snapshot["resource_version"], copy.deepcopy(self.snapshot["state"])

    def write_state(self, revision, state):
        raise ValueError("OIDC release snapshot is read-only")


def snapshot_store(path=None, *, now=time.time):
    return Store(SnapshotBackend(read_snapshot(path, now=now)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--output")
    group.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        import boto3
        session = boto3.Session(region_name=REGION)
        if args.verify_only:
            verify_aws_path(session)
        else:
            snapshot = fetch_snapshot(session)
            write_snapshot(args.output, snapshot)
    except Exception:
        raise SystemExit("OIDC_EKS_RELEASE_STATE_READ_FAILED") from None


if __name__ == "__main__":
    main()
