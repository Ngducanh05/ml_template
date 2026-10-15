import base64
import json
from pathlib import Path
from collections import namedtuple
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from control_plane import oidc_eks_reader as reader
from control_plane.state_v2 import initial_state


ENDPOINT = "https://example.gr7.ap-southeast-1.eks.amazonaws.com"
CA = base64.b64encode(b"test-ca").decode()
Credentials = namedtuple("Credentials", "access_key secret_key token")
REAL_EKS_TOKEN = reader.eks_token


class Session:
    def __init__(self, *, account=reader.ACCOUNT, arn=None, cluster=None, token="session-token"):
        self.sts = Mock()
        self.sts.get_caller_identity.return_value = {
            "Account": account,
            "Arn": arn or f"arn:aws:sts::{reader.ACCOUNT}:assumed-role/{reader.ROLE_NAME}/github-run",
        }
        self.eks = Mock()
        self.eks.describe_cluster.return_value = {"cluster": cluster or {
            "name": reader.CLUSTER_NAME,
            "arn": reader.CLUSTER_ARN,
            "status": "ACTIVE",
            "endpoint": ENDPOINT,
            "certificateAuthority": {"data": CA},
        }}
        self.credentials = Mock()
        self.credentials.get_frozen_credentials.return_value = Credentials(
            "temporary-access", "temporary-secret", token
        )

    def client(self, service, region_name=None):
        assert region_name == reader.REGION
        return {"sts": self.sts, "eks": self.eks}[service]

    def get_credentials(self):
        return self.credentials


class Backend:
    observed = None

    def __init__(self, config):
        type(self).observed = config

    def read_state(self):
        return "123", initial_state()


@pytest.fixture(autouse=True)
def token_signer(monkeypatch):
    monkeypatch.setattr(reader, "eks_token", lambda session: "k8s-aws-v1.mock-token")


def valid_snapshot(now=1000):
    return {
        "schema_version": 1,
        "source": {
            "account": reader.ACCOUNT,
            "role_arn": reader.ROLE_ARN,
            "cluster_arn": reader.CLUSTER_ARN,
        },
        "namespace": reader.NAMESPACE,
        "state_name": reader.STATE_NAME,
        "resource_version": "123",
        "created_at": now,
        "state": initial_state(),
    }


def test_exact_oidc_identity_describe_token_and_state_get(capsys):
    snapshot = reader.fetch_snapshot(Session(), backend_cls=Backend, now=lambda: 1000)
    assert snapshot == valid_snapshot()
    assert Backend.observed["server"] == ENDPOINT
    assert Backend.observed["namespace"] == "production"
    assert Backend.observed["state_name"] == "model-release-state-v2"
    assert Backend.observed["token"].startswith("k8s-aws-v1.")
    output = capsys.readouterr().out
    assert output.splitlines() == [
        "OIDC_K8S_READER_IDENTITY_PASS",
        "OIDC_EKS_DESCRIBE_CLUSTER_PASS",
        "OIDC_K8S_TOKEN_GENERATION_PASS",
        "OIDC_KUBERNETES_API_GET_PASS",
    ]
    assert "temporary-secret" not in output and Backend.observed["token"] not in output


@pytest.mark.parametrize("session", [
    Session(account="000000000000"),
    Session(arn=f"arn:aws:sts::{reader.ACCOUNT}:assumed-role/other/github-run"),
])
def test_wrong_sts_identity_fails_closed_before_eks(session):
    with pytest.raises(ValueError, match="Unexpected Kubernetes Reader identity"):
        reader.fetch_snapshot(session, backend_cls=Backend)
    session.eks.describe_cluster.assert_not_called()


@pytest.mark.parametrize("change", ["name", "arn", "status", "endpoint", "ca"])
def test_wrong_eks_metadata_fails_closed(change):
    cluster = {
        "name": reader.CLUSTER_NAME,
        "arn": reader.CLUSTER_ARN,
        "status": "ACTIVE",
        "endpoint": ENDPOINT,
        "certificateAuthority": {"data": CA},
    }
    if change == "name": cluster["name"] = "other"
    if change == "arn": cluster["arn"] = cluster["arn"].replace(reader.CLUSTER_NAME, "other")
    if change == "status": cluster["status"] = "UPDATING"
    if change == "endpoint": cluster["endpoint"] = "https://attacker.example"
    if change == "ca": cluster["certificateAuthority"]["data"] = "not-base64"
    with pytest.raises(ValueError):
        reader.fetch_snapshot(Session(cluster=cluster), backend_cls=Backend)


def test_long_lived_or_expired_aws_credentials_are_rejected():
    session = Session(token=None)
    with pytest.raises(ValueError, match="Temporary GitHub OIDC credentials required"):
        REAL_EKS_TOKEN(session)


def test_expired_sts_session_fails_before_cluster_lookup():
    session = Session()
    session.sts.get_caller_identity.side_effect = RuntimeError("ExpiredToken")
    with pytest.raises(RuntimeError, match="ExpiredToken"):
        reader.fetch_snapshot(session, backend_cls=Backend)
    session.eks.describe_cluster.assert_not_called()


@pytest.mark.parametrize("failure", [ConnectionError("network"), RuntimeError("HTTP 403")])
def test_network_or_rbac_failure_never_returns_snapshot(failure, capsys):
    class FailedBackend:
        def __init__(self, config):
            self.config = config

        def read_state(self):
            raise failure

    with pytest.raises(type(failure)):
        reader.fetch_snapshot(Session(), backend_cls=FailedBackend)
    output = capsys.readouterr().out
    assert "OIDC_K8S_TOKEN_GENERATION_PASS" in output
    assert "OIDC_KUBERNETES_API_GET_PASS" not in output
    assert "k8s-aws-v1." not in output and "temporary-secret" not in output


def test_snapshot_is_bounded_fresh_and_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    path = tmp_path / "state.json"
    reader.write_snapshot(path, valid_snapshot())
    loaded = reader.read_snapshot(path, now=lambda: 1000)
    assert loaded == valid_snapshot()
    store = reader.snapshot_store(path, now=lambda: 1000)
    assert store.read() == initial_state()
    with pytest.raises(ValueError, match="read-only"):
        store.backend.write_state("123", initial_state())
    assert "token" not in path.read_text()


def test_expired_or_external_snapshot_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    path = tmp_path / "expired.json"
    reader.write_snapshot(path, valid_snapshot(now=1000))
    with pytest.raises(ValueError, match="Expired"):
        reader.read_snapshot(path, now=lambda: 1301)
    outside = tmp_path.parent / "outside-state.json"
    outside.write_text(json.dumps(valid_snapshot()))
    with pytest.raises(ValueError, match="RUNNER_TEMP"):
        reader.read_snapshot(outside, now=lambda: 1000)


def test_cli_failure_is_sanitized_and_never_prints_secret(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=lambda **kwargs: object()))
    monkeypatch.setattr(reader, "fetch_snapshot",
                        Mock(side_effect=RuntimeError("secret-token-value")))
    with pytest.raises(SystemExit) as failure:
        reader.main(["--output", str(tmp_path / "state.json")])
    assert str(failure.value) == "OIDC_EKS_RELEASE_STATE_READ_FAILED"
    output = capsys.readouterr()
    assert "secret-token-value" not in output.out + output.err
