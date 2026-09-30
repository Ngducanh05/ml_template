"""Failure-path tests for Week 2 release controls (no registry/cluster required)."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

SPEC = importlib.util.spec_from_file_location(
    "release", Path(__file__).resolve().parents[2] / "scripts" / "release.py"
)
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


@pytest.fixture
def identity():
    sha, digest = "a" * 40, "sha256:" + "b" * 64
    image = "registry.example.test/demo/model-service"
    return {
        "git_sha": sha, "git_short_sha": sha[:12], "release_version": "v1.2.3",
        "image_name": image, "image_tag": "m-" + sha[:12], "image_digest": digest,
        "exact_deploy_image": image + "@" + digest, "ticket_reference": "demo-reference",
        "workflow_run": "https://github.com/example/demo/actions/runs/123",
        "tests_result": "success", "push_result": "success", "security_result": "success",
    }


@pytest.mark.parametrize("field", release.IDENTITY_KEYS)
def test_identity_fails_closed_for_every_missing_field(identity, field):
    identity[field] = ""
    with pytest.raises(ValueError):
        release.validate_identity(identity)


@pytest.mark.parametrize("field,value", [
    ("release_version", "latest"), ("release_version", "v01.2.3"),
    ("image_digest", "sha256:short"), ("exact_deploy_image", "image:v1.2.3"),
    ("git_short_sha", "not-the-source"), ("ticket_reference", "  "),
    ("ticket_reference", "reference\ninjected"), ("image_name", "https://bad/name"),
])
def test_malformed_or_mutable_identity_is_rejected(identity, field, value):
    identity[field] = value
    with pytest.raises(ValueError):
        release.validate_identity(identity)


@pytest.fixture
def build_evidence(monkeypatch, tmp_path, identity):
    monkeypatch.setattr(release, "EVIDENCE", tmp_path)
    release.write("release.json", identity)
    release.write("trivy.json", {"ArtifactName": identity["exact_deploy_image"],
                               "Results": [{"Target": "test OS"}]})
    (tmp_path / "tests.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>'
    )
    return tmp_path


@pytest.mark.parametrize("field", ["tests_result", "push_result", "security_result"])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", ""])
def test_failed_automatic_prerequisites_are_ineligible(build_evidence, identity, field, state):
    identity[field] = state
    with pytest.raises(ValueError):
        release.verify_build(identity)


def test_scan_for_another_image_is_ineligible(build_evidence, identity):
    release.write("trivy.json", {"ArtifactName": "other", "Results": [{}]})
    with pytest.raises(ValueError, match="scan evidence"):
        release.verify_build(identity)


def test_missing_evidence_is_ineligible(build_evidence, identity):
    (build_evidence / "tests.xml").unlink()
    with pytest.raises(FileNotFoundError):
        release.verify_build(identity)


def test_failed_test_report_cannot_claim_success(build_evidence, identity):
    (build_evidence / "tests.xml").write_text(
        '<testsuites><testsuite tests="1" failures="1" errors="0"/></testsuites>'
    )
    with pytest.raises(ValueError, match="failures"):
        release.verify_build(identity)


def test_successful_evidence_is_eligible(build_evidence, identity):
    release.verify_build(identity)


@pytest.fixture
def validation(identity):
    return {**identity, "deployment_environment": "validation",
            "deployment_result": "success",
            "checks": {k: "success" for k in ("apply", "rollout", "readiness", "image", "smoke")}}


@pytest.fixture
def eks_dev(validation):
    return {**validation, "deployment_environment": "eks-dev"}


@pytest.mark.parametrize("stage", ["apply", "rollout", "readiness", "image", "smoke"])
def test_incomplete_validation_cannot_create_candidate(identity, validation, stage):
    validation["checks"][stage] = "failure"
    with pytest.raises(ValueError):
        release.verify_validation(identity, validation, "validation")


def test_validation_cannot_substitute_another_digest(identity, validation):
    validation["image_digest"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="different release"):
        release.verify_validation(identity, validation, "validation")


def test_eks_dev_cannot_create_candidate_for_another_digest(
    build_evidence, github_run, eks_dev
):
    eks_dev["image_digest"] = "sha256:" + "c" * 64
    eks_dev["exact_deploy_image"] = eks_dev["image_name"] + "@" + eks_dev["image_digest"]
    release.write("eks-dev.json", eks_dev)
    with pytest.raises(ValueError, match="different release"):
        release.candidate()
    assert not (build_evidence / "candidate.json").exists()


@pytest.fixture
def protection():
    return {
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "User"}],
            }
        ],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def test_protection_requires_reviewers_self_review_prevention_and_release_policy(
    protection,
):
    policies = {
        "total_count": 1,
        "branch_policies": [{"id": 1, "node_id": "test", "name": "v*"}],
    }

    release.verify_protection(protection, policies)

    for field, value in [
        ("protection_rules", []),
        ("deployment_branch_policy", None),
    ]:
        invalid = {**protection, field: value}
        with pytest.raises(ValueError):
            release.verify_protection(invalid, policies)

    invalid = deepcopy(protection)
    invalid["protection_rules"][0]["prevent_self_review"] = False

    with pytest.raises(ValueError):
        release.verify_protection(invalid, policies)

    with pytest.raises(ValueError):
        release.verify_protection(
            protection,
            {
                "total_count": 1,
                "branch_policies": [
                    {"id": 2, "node_id": "test", "name": "release/*"}
                ],
            },
        )


@pytest.fixture
def aws_config(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("ECR_REPOSITORY", "model-service")


def test_prepare_uses_configured_ecr_identity(
    monkeypatch, build_evidence, github_run, aws_config, identity
):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/tags/" + identity["release_version"])
    monkeypatch.setenv("TRIVY_BLOCKING_SEVERITIES", "HIGH,CRITICAL")
    monkeypatch.setenv("TRIVY_IGNORE_UNFIXED", "false")
    monkeypatch.setenv("GITHUB_OUTPUT", str(build_evidence / "outputs"))
    monkeypatch.setattr(release, "command", Mock(side_effect=[identity["git_sha"], ""]))
    release.prepare()
    data = release.read("release.json")
    assert data["image_name"] == "123456789012.dkr.ecr.us-east-2.amazonaws.com/model-service"
    assert data["image_tag"] == identity["image_tag"]
    assert (build_evidence / "outputs").read_text().splitlines() == [
        f'image_name={data["image_name"]}', f'image_tag={data["image_tag"]}',
        f'release_version={data["release_version"]}',
    ]


@pytest.mark.parametrize("existing_tag_index", [0, 1])
def test_ecr_existing_tag_cannot_be_reused(
    monkeypatch, build_evidence, aws_config, existing_tag_index
):
    missing = subprocess.CompletedProcess([], 254, "", "ImageNotFoundException")
    exists = subprocess.CompletedProcess([], 0, "{}", "")
    run = Mock(side_effect=[missing] * existing_tag_index + [exists])
    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(ValueError, match="identities cannot be reused"):
        release.registry_check()
    assert run.call_count == existing_tag_index + 1


def test_ecr_only_image_not_found_allows_both_tags(
    monkeypatch, build_evidence, aws_config, identity
):
    run = Mock(return_value=subprocess.CompletedProcess([], 254, "", "ImageNotFoundException"))
    monkeypatch.setattr(release.subprocess, "run", run)
    release.registry_check()
    assert run.call_count == 2
    for call, tag in zip(run.call_args_list, (identity["release_version"], identity["image_tag"])):
        assert call.args[0] == [
            "aws", "ecr", "describe-images", "--region", "us-east-2",
            "--repository-name", "model-service", "--image-ids", f"imageTag={tag}",
            "--output", "json",
        ]


@pytest.mark.parametrize("error", [
    "AccessDeniedException", "RepositoryNotFoundException", "Unable to locate credentials",
    "Could not connect to the endpoint URL", "Unknown AWS CLI error", "",
])
def test_ecr_errors_never_mean_tag_absent(monkeypatch, build_evidence, aws_config, error):
    run = Mock(return_value=subprocess.CompletedProcess([], 254, "", error))
    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(ValueError, match="ECR identity check failed"):
        release.registry_check()


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("aws", 30)])
def test_ecr_cli_failure_is_closed(monkeypatch, build_evidence, aws_config, error):
    monkeypatch.setattr(release.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(ValueError, match="ECR identity check failed"):
        release.registry_check()


@pytest.mark.parametrize("target", ["validation", "eks-dev", "production"])
@pytest.mark.parametrize("has_pull_secret", [True, False])
@pytest.mark.parametrize("stage", ["apply", "rollout", "wait", "smoke", None])
def test_deployment_records_success_or_native_failure(
    monkeypatch, build_evidence, identity, eks_dev, target, has_pull_secret, stage
):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", target)
    monkeypatch.setenv("KUBE_CONTEXT", "docker-desktop" if target == "validation" else "test-cluster")
    monkeypatch.setenv("KUBE_NAMESPACE", "release-test")
    if target == "production":
        release.write("candidate.json", {**identity, "approval_state": "pending_human_review"})
        release.write("eks-dev.json", eks_dev)
        monkeypatch.setenv("KUBECONFIG_B64", "dGVzdA==")
        monkeypatch.setenv("KUBECONFIG", "")
    monkeypatch.setattr(release, "bound_release", lambda: identity)
    calls = []
    applied = []
    pod_spec = {"containers": [{"name": "model-service", "image": identity["exact_deploy_image"]}]}
    if has_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": "model-service-registry"}]

    def fake_command(*args, **kwargs):
        calls.append(args)
        if stage in args or (stage == "smoke" and "scripts/smoke_test.py" in args):
            raise subprocess.CalledProcessError(1, args)
        if "set" in args:
            return json.dumps({"spec": {"template": {"metadata": {}, "spec": pod_spec}}})
        if "apply" in args and "input" in kwargs:
            applied.append(json.loads(kwargs["input"]))
        if "get" in args and "deployment" in args:
            return json.dumps({"spec": {"template": {"spec": {"containers": [
                {"image": identity["exact_deploy_image"]}]}}}})
        if "get" in args and "pods" in args:
            return json.dumps({"items": [{"metadata": {}, "spec": {"containers": [
                {"name": "model-service", "image": identity["exact_deploy_image"]}]},
                "status": {"containerStatuses": [{"name": "model-service", "ready": True,
                                                 "imageID": identity["exact_deploy_image"]}]}}]})
        return ""

    class Forward:
        def poll(self): return None
        def terminate(self): pass
        def wait(self, **kwargs): return 0

    monkeypatch.setattr(release, "command", fake_command)
    monkeypatch.setattr(release.subprocess, "Popen", lambda *a, **k: Forward())
    monkeypatch.setattr(release.socket, "create_connection", lambda *a, **k: release.socket.socket())
    if stage is None:
        release.deploy()
    else:
        with pytest.raises(subprocess.CalledProcessError):
            release.deploy()
    report = release.read(f"{target}.json")
    assert report["deployment_environment"] == target
    if stage is None:
        release.verify_validation(identity, report, target)
        assert report["approval_state"] == (
            "environment_gate_passed" if target == "production" else "not_applicable"
        )
    else:
        assert report["deployment_result"] == "failure"
        assert report["checks"].get("smoke") != "success"
        if stage != "smoke":
            assert not any("scripts/smoke_test.py" in c for c in calls)
    if stage != "apply":
        expected_spec = deepcopy(pod_spec)
        if target == "eks-dev":
            expected_spec.pop("imagePullSecrets", None)
        assert applied[0]["spec"]["template"]["spec"] == expected_spec


def test_rerun_cannot_rebuild_or_deploy(monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    with pytest.raises(ValueError, match="reruns"):
        release.first_attempt()


@pytest.fixture
def github_run(monkeypatch, identity):
    values = {
        "GITHUB_SHA": identity["git_sha"], "GITHUB_REF_NAME": identity["release_version"],
        "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "example/demo",
        "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1",
        "EXACT_DEPLOY_IMAGE": identity["exact_deploy_image"],
        "TICKET_REFERENCE": identity["ticket_reference"],
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("field,value", [
    ("GITHUB_SHA", "c" * 40), ("GITHUB_RUN_ID", "999"),
    ("EXACT_DEPLOY_IMAGE", "other@sha256:" + "c" * 64),
])
def test_cross_job_identity_substitution_is_blocked(
    monkeypatch, build_evidence, github_run, field, value
):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        release.bound_release()


@pytest.fixture
def audit_evidence(monkeypatch, build_evidence, github_run, identity, eks_dev):
    monkeypatch.setenv("NEEDS_JSON", json.dumps({
        k: {"result": "success"} for k in ("build", "eks_dev", "candidate", "production")
    }))
    release.write("eks-dev.json", eks_dev)
    release.write("candidate.json", {**identity, "approval_state": "pending_human_review"})
    release.write("production.json", {**eks_dev, "deployment_environment": "production",
                                      "approval_state": "environment_gate_passed"})
    return build_evidence


@pytest.mark.parametrize("job", ["build", "eks_dev", "candidate", "production"])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped"])
def test_rejected_or_failed_prerequisite_never_records_release_success(
    monkeypatch, audit_evidence, job, state
):
    needs = {k: {"result": "success"} for k in ("build", "eks_dev", "candidate", "production")}
    needs[job] = {"result": state}
    monkeypatch.setenv("NEEDS_JSON", json.dumps(needs))
    release.summary()
    assert release.read("summary.json")["release_result"] == "not_successful"


@pytest.mark.parametrize("filename", [
    "release.json", "eks-dev.json", "candidate.json", "production.json", "trivy.json", "tests.xml",
])
def test_success_requires_final_evidence(audit_evidence, filename):
    (audit_evidence / filename).unlink()
    with pytest.raises(ValueError, match="audit evidence"):
        release.summary()
    assert release.read("summary.json")["release_result"] == "not_successful"


def test_complete_successful_audit(audit_evidence):
    release.summary()
    assert release.read("summary.json")["release_result"] == "success"


@pytest.mark.parametrize("field,value", [
    ("deployment_environment", "validation"), ("deployment_result", "failure"),
    ("image_digest", "sha256:" + "c" * 64),
    *[("checks", {k: "failure"}) for k in ("apply", "rollout", "readiness", "image", "smoke")],
])
def test_invalid_eks_dev_evidence_blocks_candidate_production_and_audit(
    monkeypatch, audit_evidence, eks_dev, field, value
):
    if field == "checks":
        eks_dev["checks"].update(value)
    else:
        eks_dev[field] = value
    release.write("eks-dev.json", eks_dev)
    (audit_evidence / "candidate.json").unlink()
    with pytest.raises(ValueError):
        release.candidate()
    assert not (audit_evidence / "candidate.json").exists()
    release.write("candidate.json", {**release.read("release.json"),
                                      "approval_state": "pending_human_review"})
    with pytest.raises(ValueError, match="audit evidence"):
        release.summary()
    assert release.read("summary.json")["release_result"] == "not_successful"
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    with pytest.raises(ValueError):
        release.deploy()
    assert release.read("production.json")["deployment_result"] == "failure"


@pytest.mark.parametrize("target,context", [
    ("validation", "test-cluster"), ("eks-dev", "docker-desktop"), ("production", "docker-desktop"),
])
def test_deployment_context_rules_fail_before_kubectl(
    monkeypatch, audit_evidence, target, context
):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", target)
    monkeypatch.setenv("KUBE_CONTEXT", context)
    monkeypatch.setenv("KUBE_NAMESPACE", "release-test")
    monkeypatch.setenv("KUBECONFIG_B64", "dGVzdA==")
    monkeypatch.setenv("KUBECONFIG", "")
    command = Mock()
    monkeypatch.setattr(release, "command", command)
    with pytest.raises(ValueError, match="Docker Desktop"):
        release.deploy()
    command.assert_not_called()
    assert release.read(f"{target}.json")["deployment_result"] == "failure"


def test_protection_api_timeout_does_not_create_candidate(
    monkeypatch, build_evidence, github_run, eks_dev
):
    release.write("eks-dev.json", eks_dev)

    def timeout(*args):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(release, "github_json", timeout)
    with pytest.raises(TimeoutError):
        release.candidate()
    assert not (build_evidence / "candidate.json").exists()
