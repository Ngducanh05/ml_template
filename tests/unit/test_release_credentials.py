import base64
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

from scripts.github import rotate_release_state_credential as rotate


def jwt(payload):
    segment = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "header." + segment + ".signature"


def test_token_contract_and_expiration():
    token = jwt({"sub": "system:serviceaccount:production:release-state-reader-v2", "aud": ["https://kubernetes.default.svc"], "iat": 1000, "exp": 4600})
    claims = rotate.validate_token(token, "release-state-reader-v2", 1800, now=1000)
    assert claims.remaining_seconds == 3600
    config = rotate.credential("https://api.example", "Y2E=", token)
    assert set(config) == {"server", "certificate_authority_data", "token", "namespace", "state_name"}
    assert json.loads(base64.b64decode(rotate.encode_credential(config))) == config


@pytest.mark.parametrize("change", ["subject", "audience", "expired", "short", "future"])
def test_invalid_or_insufficient_token_fails_closed(change):
    payload = {"sub": "system:serviceaccount:production:release-state-reader-v2", "aud": ["https://kubernetes.default.svc"], "iat": 1000, "exp": 4600}
    if change == "subject": payload["sub"] = "system:serviceaccount:production:other"
    if change == "audience": payload["aud"] = ["other"]
    if change == "expired": payload["exp"] = 999
    if change == "short": payload["exp"] = 2000
    if change == "future": payload["iat"] = 2000
    with pytest.raises(ValueError):
        rotate.validate_token(jwt(payload), "release-state-reader-v2", 1800, now=1000)


def test_writer_storage_requires_explicit_confirmation(monkeypatch):
    command = Mock(return_value="")
    monkeypatch.setattr(rotate, "_run", command)
    with pytest.raises(ValueError, match="confirm-writer"):
        rotate.store_secret("writer", "encoded", False)
    rotate.store_secret("writer", "encoded", True)
    assert command.call_args.args == ("gh", "secret", "set", "RELEASE_STATE_WRITER_B64", "--repo", rotate.REPOSITORY, "--env", "production")
    assert command.call_args.kwargs == {"input_text": "encoded"}


def test_reader_storage_uses_repository_scope_and_stdin(monkeypatch):
    command = Mock(return_value="")
    monkeypatch.setattr(rotate, "_run", command)
    rotate.store_secret("reader", "encoded", False)
    assert command.call_args.args == ("gh", "secret", "set", "RELEASE_STATE_READER_B64", "--repo", rotate.REPOSITORY)
    assert command.call_args.kwargs == {"input_text": "encoded"}


def test_permission_matrix_is_exact_and_fail_closed(monkeypatch):
    observed = []
    def access(config, verb, resource, name, **kwargs):
        observed.append((verb, resource, name, kwargs))
        return verb in {"get", "update"} and resource == "configmaps" and name == rotate.STATE_NAME
    monkeypatch.setattr(rotate, "_self_access", access)
    rotate.verify_permissions({}, "writer")
    assert ("get", "secrets", "unrelated", {"group": "", "subresource": None}) in observed
    monkeypatch.setattr(rotate, "_self_access", lambda *args, **kwargs: True)
    with pytest.raises(PermissionError):
        rotate.verify_permissions({}, "reader")


def test_state_summary_ignores_non_candidate_pending_rows():
    state = {
        "stable": {"model_version": "6"},
        "pending": [
            {"status": "FAILED"},
            {"status": "READY_FOR_RELEASE", "pair": {"model_version": "11"}},
            {"status": "READY_FOR_RELEASE", "pair": {"model_version": "9"}},
        ],
        "active": None,
        "route": {"weight": 0},
    }
    assert rotate.summarize_state(state) == {
        "stable_model_version": "6",
        "candidate_model_version": "11",
        "candidate_status": "READY_FOR_RELEASE",
        "active": False,
        "traffic_weight": 0,
    }


def test_oidc_probe_is_main_only_read_only_and_has_no_release_access():
    path = Path(".github/workflows/oidc-catalog-probe.yml")
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["probe"]
    assert workflow["on"] == {"workflow_dispatch": ""}
    assert workflow["permissions"] == {"contents": "read", "id-token": "write"}
    assert job["runs-on"] == "ubuntu-latest"
    assert "github.ref == 'refs/heads/main'" in job["if"]
    text = path.read_text()
    for action in ("sts get-caller-identity", "ecr describe-repositories", "ecr describe-images", "ecr batch-get-image"):
        assert action in text
    for prohibited in ("kubectl", "RELEASE_STATE_", "ecr put-image", "workflow_run", "environment: production"):
        assert prohibited not in text


def test_catalog_reader_trust_uses_only_exact_immutable_subjects():
    trust = json.loads(Path("k8s/release-v2/catalog-reader-trust.proposed.json").read_text())
    condition = trust["Statement"][0]["Condition"]
    assert condition["StringEquals"] == {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"}
    assert set(condition["StringLike"]["token.actions.githubusercontent.com:sub"]) == {
        "repo:Ngducanh05@209518583/ml_template@1375284682:ref:refs/heads/main",
        "repo:Ngducanh05@209518583/ml_template@1375284682:ref:refs/tags/v*",
        "repo:Ngducanh05@209518583/ml_template@1375284682:environment:production",
    }


def test_k8s_oidc_probe_separates_token_generation_from_live_api_get():
    path = Path(".github/workflows/oidc-k8s-reader-probe.yml")
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert workflow["on"]["workflow_dispatch"]["inputs"]["live_get"]["default"] == "false"
    hosted = workflow["jobs"]["probe"]
    live = workflow["jobs"]["live-get"]
    assert hosted["runs-on"] == "ubuntu-latest"
    assert live["runs-on"] == ["self-hosted", "Linux", "X64", "mlops-release-readonly"]
    hosted_text = json.dumps(hosted)
    live_text = json.dumps(live)
    assert "--verify-only" in hosted_text and "--output" not in hosted_text
    assert "--output" in live_text and "OIDC_KUBERNETES_API_GET_PASS" not in path.read_text()
    reader_role = "arn:aws:iam::596432515999:role/ml-template-v2-k8s-reader"
    assert reader_role in hosted_text and reader_role in live_text
    assert "RELEASE_STATE_READER_B64" not in path.read_text()
    assert "RELEASE_STATE_READER_SNAPSHOT" not in live.get("env", {})
    get_step = next(step for step in live["steps"]
                    if step.get("name") == "Verify live scoped Kubernetes API GET")
    snapshot = get_step["env"]["RELEASE_STATE_READER_SNAPSHOT"]
    assert "${{ runner.temp }}" in snapshot and "${{ github.run_attempt }}" in snapshot


@pytest.mark.parametrize("path,job_name,consumer", [
    (".github/workflows/release.yml", "preflight",
     "Read exact Release State through OIDC and scoped EKS RBAC"),
    (".github/workflows/runtime-build-trigger.yml", "observe-and-dispatch",
     "Read exact Release State through OIDC and scoped EKS RBAC"),
])
def test_snapshot_path_is_initialized_at_runtime_and_cleanup_is_fail_safe(path, job_name, consumer):
    workflow = yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"][job_name]
    assert "runner.temp" not in json.dumps(job.get("env", {}))
    steps = job["steps"]
    init_index = next(i for i, step in enumerate(steps)
                      if step.get("name") == "Initialize temporary Release State snapshot path")
    consumer_index = next(i for i, step in enumerate(steps) if step.get("name") == consumer)
    assert init_index < consumer_index
    initialization = steps[init_index]["run"]
    for runtime_name in ("RUNNER_TEMP", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_ENV"):
        assert runtime_name in initialization
    assert "token" not in initialization.lower()
    cleanup = next(step for step in steps
                   if step.get("name") == "Remove temporary non-secret Release State snapshot")
    assert cleanup["if"] == "always()"
    assert "${RELEASE_STATE_READER_SNAPSHOT:-}" in cleanup["run"]
    assert "rm -f --" in cleanup["run"]
