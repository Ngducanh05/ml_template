"""Failure-path tests for dual-image release controls (no registry/cluster required)."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import shutil
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
    sha = "a" * 40
    runtime = "123456789012.dkr.ecr.us-east-2.amazonaws.com/model-service-runtime"
    artifact = "123456789012.dkr.ecr.us-east-2.amazonaws.com/model-service-artifact"
    runtime_digest, artifact_digest = "sha256:" + "b" * 64, "sha256:" + "d" * 64
    return {
        "schema_version": 2, "git_sha": sha, "git_short_sha": sha[:12], "release_version": "v1.2.3",
        "runtime_image_name": runtime, "runtime_digest": runtime_digest,
        "exact_runtime_image": runtime + "@" + runtime_digest,
        "artifact_image_name": artifact, "artifact_tag": "m-" + sha[:12],
        "artifact_digest": artifact_digest, "exact_artifact_image": artifact + "@" + artifact_digest,
        "ticket_reference": "demo-reference",
        "workflow_run": "https://github.com/example/demo/actions/runs/123",
        "security_policy": {"blocking_severities": "HIGH,CRITICAL", "ignore_unfixed": "false"},
        "tests_result": "success", "artifact_push_result": "success",
        "runtime_scan_result": "success", "artifact_scan_result": "success",
    }


@pytest.mark.parametrize("field", release.IDENTITY_KEYS)
@pytest.mark.parametrize("missing", [True, False])
def test_identity_fails_closed_for_every_missing_field(identity, field, missing):
    if missing:
        identity.pop(field)
    else:
        identity[field] = ""
    with pytest.raises(ValueError):
        release.validate_identity(identity)


@pytest.mark.parametrize("field,value", [
    ("release_version", "latest"), ("release_version", "v01.2.3"),
    ("schema_version", 1), ("git_sha", "g" * 40), ("artifact_tag", "latest"),
    *[(f"{kind}_digest", value) for kind in ("runtime", "artifact")
      for value in ("sha256:short", "sha256:" + "A" * 64, "sha512:" + "b" * 64)],
    *[(f"exact_{kind}_image", value) for kind in ("runtime", "artifact")
      for value in ("image:v1.2.3", "other/repo@sha256:" + "c" * 64)],
    *[(f"{kind}_image_name", "https://bad/name") for kind in ("runtime", "artifact")],
    ("git_short_sha", "not-the-source"), ("ticket_reference", "  "),
    ("ticket_reference", "reference\ninjected"), ("ticket_reference", "x" * 257),
])
def test_malformed_or_mutable_identity_is_rejected(identity, field, value):
    identity[field] = value
    with pytest.raises(ValueError):
        release.validate_identity(identity)


@pytest.fixture
def build_evidence(monkeypatch, tmp_path, identity):
    monkeypatch.setattr(release, "EVIDENCE", tmp_path)
    release.write("release.json", identity)
    for kind in ("runtime", "artifact"):
        release.write(f"trivy-{kind}.json", {"ArtifactName": identity[f"exact_{kind}_image"],
                                           "Results": [{"Target": "test OS"}]})
    (tmp_path / "tests.xml").write_text(
        '<testsuites><testsuite tests="1" failures="0" errors="0"/></testsuites>'
    )
    return tmp_path


@pytest.mark.parametrize("field", [
    "tests_result", "artifact_push_result", "runtime_scan_result", "artifact_scan_result",
])
@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped", ""])
def test_failed_automatic_prerequisites_are_ineligible(
    build_evidence, github_run, identity, field, state
):
    identity[field] = state
    release.write("release.json", identity)
    with pytest.raises(ValueError):
        release.verify_build(identity)
    with pytest.raises(ValueError):
        release.candidate()
    assert not (build_evidence / "candidate.json").exists()


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
def test_scan_for_another_image_is_ineligible(build_evidence, identity, kind):
    release.write(f"trivy-{kind}.json", {"ArtifactName": "other", "Results": [{}]})
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


@pytest.fixture
def pod(identity):
    return {
        "metadata": {},
        "spec": {
            "containers": [{"name": "model-service", "image": identity["exact_runtime_image"]}],
            "initContainers": [{"name": "model-artifact", "image": identity["exact_artifact_image"]}],
        },
        "status": {
            "phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
            "containerStatuses": [{"name": "model-service", "ready": True,
                                   "imageID": identity["exact_runtime_image"]}],
            "initContainerStatuses": [{"name": "model-artifact",
                                       "imageID": identity["exact_artifact_image"],
                                       "state": {"terminated": {"exitCode": 0}}}],
        },
    }


@pytest.mark.parametrize("stage", ["apply", "rollout", "readiness", "image", "smoke"])
def test_incomplete_validation_cannot_create_candidate(identity, validation, stage):
    validation["checks"][stage] = "failure"
    with pytest.raises(ValueError):
        release.verify_validation(identity, validation, "validation")


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
def test_validation_cannot_substitute_another_digest(identity, validation, kind):
    validation[f"{kind}_digest"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="different release"):
        release.verify_validation(identity, validation, "validation")


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
def test_eks_dev_cannot_create_candidate_for_another_digest(
    build_evidence, github_run, eks_dev, kind
):
    eks_dev[f"{kind}_digest"] = "sha256:" + "c" * 64
    eks_dev[f"exact_{kind}_image"] = eks_dev[f"{kind}_image_name"] + "@" + eks_dev[f"{kind}_digest"]
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
def aws_config(monkeypatch, identity, github_run):
    monkeypatch.setenv("AWS_ACCOUNT_ID", "123456789012")
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    monkeypatch.setenv("ECR_RUNTIME_REPOSITORY", "model-service-runtime")
    monkeypatch.setenv("ECR_ARTIFACT_REPOSITORY", "model-service-artifact")
    monkeypatch.setenv("RUNTIME_IMAGE_DIGEST", identity["runtime_digest"])


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
    assert data["schema_version"] == 2
    for key in ("runtime_image_name", "runtime_digest", "exact_runtime_image",
                "artifact_image_name", "artifact_tag"):
        assert data[key] == identity[key]
    assert data["artifact_digest"] == data["exact_artifact_image"] == ""
    release.validate_identity(data, allow_unpinned=True)
    with pytest.raises(ValueError):
        release.validate_identity(data)
    assert (build_evidence / "outputs").read_text().splitlines() == [
        f'artifact_image_name={data["artifact_image_name"]}', f'artifact_tag={data["artifact_tag"]}',
        f'exact_runtime_image={data["exact_runtime_image"]}',
        f'release_version={data["release_version"]}',
    ]


@pytest.mark.parametrize("existing_tag_index", [0, 1])
def test_ecr_existing_tag_cannot_be_reused(
    monkeypatch, build_evidence, aws_config, existing_tag_index
):
    missing = missing_image()
    exists = subprocess.CompletedProcess([], 0, "{}", "")
    run = Mock(side_effect=[missing] * existing_tag_index + [exists])
    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(ValueError, match="identities cannot be reused"):
        release.registry_check()
    assert run.call_count == existing_tag_index + 1


def missing_image():
    return subprocess.CompletedProcess([], 254, "", "An error occurred (ImageNotFoundException) "
                                       "when calling the DescribeImages operation: not found")


@pytest.fixture
def runtime_response(identity):
    return {"imageDetails": [{"imageDigest": identity["runtime_digest"],
                              "repositoryName": "model-service-runtime",
                              "registryId": "123456789012"}]}


def test_ecr_only_unused_artifact_tags_and_existing_runtime_pass(
    monkeypatch, build_evidence, aws_config, identity, runtime_response
):
    run = Mock(side_effect=[missing_image(), missing_image(),
                           subprocess.CompletedProcess([], 0, json.dumps(runtime_response), "")])
    monkeypatch.setattr(release.subprocess, "run", run)
    release.registry_check()
    assert run.call_count == 3
    for call, tag in zip(run.call_args_list, (identity["release_version"], identity["artifact_tag"])):
        assert call.args[0] == [
            "aws", "ecr", "describe-images", "--region", "us-east-2",
            "--registry-id", "123456789012", "--repository-name", "model-service-artifact",
            "--image-ids", f"imageTag={tag}",
            "--output", "json",
        ]
    assert run.call_args_list[2].args[0] == [
        "aws", "ecr", "describe-images", "--region", "us-east-2",
        "--registry-id", "123456789012", "--repository-name", "model-service-runtime",
        "--image-ids", f'imageDigest={identity["runtime_digest"]}', "--output", "json",
    ]


@pytest.mark.parametrize("error", [
    "AccessDeniedException", "RepositoryNotFoundException", "Unable to locate credentials",
    "Could not connect to the endpoint URL", "Unknown AWS CLI error", "", "ImageNotFoundException",
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
    monkeypatch, build_evidence, identity, eks_dev, pod, target, has_pull_secret, stage
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
    pod_spec = pod["spec"]
    if has_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": "model-service-registry"}]

    def fake_command(*args, **kwargs):
        calls.append(args)
        if stage in args or (stage == "smoke" and "scripts/smoke_test.py" in args):
            raise subprocess.CalledProcessError(1, args)
        if "set" in args:
            assert "--local" in args
            assert f'model-service={identity["exact_runtime_image"]}' in args
            assert f'model-artifact={identity["exact_artifact_image"]}' in args
            return json.dumps({"spec": {"template": {"metadata": {}, "spec": pod_spec}}})
        if "apply" in args and "input" in kwargs:
            applied.append(json.loads(kwargs["input"]))
        if "get" in args and "deployment" in args:
            return json.dumps({"spec": {"template": {"spec": pod_spec}}})
        if "get" in args and "pods" in args:
            return json.dumps({"items": [pod]})
        return ""

    forward = Mock()
    forward.poll.return_value = None
    monkeypatch.setattr(release, "command", fake_command)
    monkeypatch.setattr(release.subprocess, "Popen", lambda *a, **k: forward)
    monkeypatch.setattr(release.socket, "create_connection", lambda *a, **k: release.socket.socket())
    if stage is None:
        release.deploy()
    else:
        with pytest.raises(subprocess.CalledProcessError):
            release.deploy()
    report = release.read(f"{target}.json")
    assert report["deployment_environment"] == target
    assert report["exact_runtime_image"] == identity["exact_runtime_image"]
    assert report["exact_artifact_image"] == identity["exact_artifact_image"]
    if stage in {None, "smoke"}:
        forward.terminate.assert_called_once()
        forward.wait.assert_called_once_with(timeout=10)
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
        "GITHUB_REF": "refs/tags/" + identity["release_version"],
        "EXACT_RUNTIME_IMAGE": identity["exact_runtime_image"],
        "EXACT_ARTIFACT_IMAGE": identity["exact_artifact_image"],
        "TICKET_REFERENCE": identity["ticket_reference"],
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("field,value", [
    ("GITHUB_SHA", "c" * 40), ("GITHUB_RUN_ID", "999"),
    ("GITHUB_REF_NAME", "v2.0.0"), ("GITHUB_REF", "refs/heads/main"),
    ("TICKET_REFERENCE", "different-ticket"),
    ("EXACT_RUNTIME_IMAGE", "other/runtime@sha256:" + "c" * 64),
    ("EXACT_ARTIFACT_IMAGE", "other/artifact@sha256:" + "c" * 64),
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
    "release.json", "eks-dev.json", "candidate.json", "production.json",
    "trivy-runtime.json", "trivy-artifact.json", "tests.xml",
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
    ("runtime_digest", "sha256:" + "c" * 64),
    ("artifact_digest", "sha256:" + "c" * 64),
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


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
@pytest.mark.parametrize("field", ["image_name", "digest"])
def test_exact_image_must_match_both_name_and_digest(identity, kind, field):
    identity[f"{kind}_{field}"] = (
        "other.example.test/repository" if field == "image_name" else "sha256:" + "c" * 64
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        release.validate_identity(identity)


@pytest.mark.parametrize("policy", [
    {}, {"blocking_severities": "", "ignore_unfixed": "false"},
    {"blocking_severities": "HIGH,", "ignore_unfixed": "false"},
    {"blocking_severities": "HIGH", "ignore_unfixed": "yes"},
])
def test_security_policy_is_required_and_valid(identity, policy):
    identity["security_policy"] = policy
    with pytest.raises(ValueError):
        release.validate_identity(identity)


@pytest.fixture
def prepare_context(monkeypatch, build_evidence, github_run, aws_config, identity):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("TRIVY_BLOCKING_SEVERITIES", "HIGH,CRITICAL")
    monkeypatch.setenv("TRIVY_IGNORE_UNFIXED", "false")
    monkeypatch.setenv("GITHUB_OUTPUT", str(build_evidence / "outputs"))
    git = Mock(side_effect=[identity["git_sha"], ""])
    monkeypatch.setattr(release, "command", git)
    (build_evidence / "release.json").unlink()
    return git


@pytest.mark.parametrize("field,value", [
    ("GITHUB_EVENT_NAME", "push"), ("GITHUB_REF", "refs/heads/main"),
    ("GITHUB_REF_NAME", "v01.2.3"), ("GITHUB_SHA", "c" * 40),
    ("GITHUB_RUN_ATTEMPT", "2"), ("TICKET_REFERENCE", " "),
    ("RUNTIME_IMAGE_DIGEST", "latest"), ("RUNTIME_IMAGE_DIGEST", "sha256:" + "A" * 64),
    ("RUNTIME_IMAGE_DIGEST", "sha256:" + "b" * 63),
    ("ECR_RUNTIME_REPOSITORY", "runtime:latest"), ("ECR_ARTIFACT_REPOSITORY", "bad/repo@digest"),
    ("AWS_ACCOUNT_ID", "invalid"), ("AWS_REGION", "invalid"),
    ("TRIVY_BLOCKING_SEVERITIES", ""), ("TRIVY_IGNORE_UNFIXED", "yes"),
])
def test_prepare_fails_before_publishing_invalid_input(
    monkeypatch, prepare_context, build_evidence, field, value
):
    monkeypatch.setenv(field, value)
    with pytest.raises(ValueError):
        release.prepare()
    assert not (build_evidence / "release.json").exists()
    assert not (build_evidence / "outputs").exists()


def test_source_must_be_reachable_from_main(prepare_context, build_evidence, identity):
    prepare_context.side_effect = [identity["git_sha"], subprocess.CalledProcessError(1, "git")]
    with pytest.raises(subprocess.CalledProcessError):
        release.prepare()
    assert prepare_context.call_args.args == (
        "git", "merge-base", "--is-ancestor", identity["git_sha"], "origin/main",
    )
    assert not (build_evidence / "release.json").exists()


@pytest.mark.parametrize("error", [
    "ImageNotFoundException", "RepositoryNotFoundException", "AccessDeniedException",
    "Unable to locate credentials", "Could not connect to endpoint", "",
])
def test_runtime_lookup_failure_is_never_absence(
    monkeypatch, build_evidence, aws_config, error
):
    monkeypatch.setattr(release.subprocess, "run", Mock(side_effect=[
        missing_image(), missing_image(), subprocess.CompletedProcess([], 254, "", error),
    ]))
    with pytest.raises(ValueError, match="runtime digest lookup failed"):
        release.registry_check()


@pytest.mark.parametrize("body", [
    "not-json", "{}", '{"imageDetails": []}',
    *[json.dumps({"imageDetails": [{
        "imageDigest": "sha256:" + "b" * 64, "repositoryName": "model-service-runtime",
        "registryId": "123456789012", field: "wrong",
    }]}) for field in ("imageDigest", "repositoryName", "registryId")],
])
def test_runtime_lookup_requires_exact_digest_repository_and_account(
    monkeypatch, build_evidence, aws_config, body
):
    monkeypatch.setattr(release.subprocess, "run", Mock(side_effect=[
        missing_image(), missing_image(), subprocess.CompletedProcess([], 0, body, ""),
    ]))
    with pytest.raises(ValueError):
        release.registry_check()


@pytest.mark.parametrize("field,value", [
    ("ECR_RUNTIME_REPOSITORY", "other-runtime"), ("ECR_ARTIFACT_REPOSITORY", "other-artifact"),
    ("AWS_ACCOUNT_ID", "987654321012"), ("RUNTIME_IMAGE_DIGEST", "sha256:" + "c" * 64),
])
def test_registry_configuration_cannot_substitute_prepared_identity(
    monkeypatch, build_evidence, aws_config, field, value
):
    monkeypatch.setenv(field, value)
    run = Mock()
    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(ValueError):
        release.registry_check()
    run.assert_not_called()


def test_pin_only_artifact_and_refuse_repin(monkeypatch, prepare_context, identity, build_evidence):
    release.prepare()
    before = release.read("release.json")
    monkeypatch.setenv("ARTIFACT_IMAGE_DIGEST", identity["artifact_digest"])
    release.pin()
    after = release.read("release.json")
    assert after == {**before, "artifact_digest": identity["artifact_digest"],
                     "exact_artifact_image": identity["exact_artifact_image"]}
    assert (build_evidence / "outputs").read_text().endswith(
        f'exact_runtime_image={identity["exact_runtime_image"]}\n'
        f'exact_artifact_image={identity["exact_artifact_image"]}\n'
    )
    with pytest.raises(ValueError, match="already pinned"):
        release.pin()


@pytest.mark.parametrize("digest", ["", "latest", "sha256:short", "sha256:" + "A" * 64])
def test_pin_rejects_invalid_artifact_digest(monkeypatch, prepare_context, digest):
    release.prepare()
    before = release.read("release.json")
    monkeypatch.setenv("ARTIFACT_IMAGE_DIGEST", digest)
    with pytest.raises(ValueError):
        release.pin()
    assert release.read("release.json") == before


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
def test_finalize_records_failed_scan_and_blocks_candidate(
    monkeypatch, build_evidence, github_run, kind
):
    for key in ("TEST_RESULT", "ARTIFACT_PUSH_RESULT", "RUNTIME_SCAN_RESULT", "ARTIFACT_SCAN_RESULT"):
        monkeypatch.setenv(key, "success")
    monkeypatch.setenv(f"{kind.upper()}_SCAN_RESULT", "failure")
    with pytest.raises(ValueError, match="prerequisites"):
        release.finalize_build()
    assert release.read("release.json")[f"{kind}_scan_result"] == "failure"
    with pytest.raises(ValueError):
        release.candidate()
    assert not (build_evidence / "candidate.json").exists()


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
@pytest.mark.parametrize("results", [None, [], "invalid"])
def test_incomplete_scan_report_is_rejected(build_evidence, identity, kind, results):
    release.write(f"trivy-{kind}.json", {"ArtifactName": identity[f"exact_{kind}_image"],
                                       "Results": results})
    with pytest.raises(ValueError, match="scan evidence"):
        release.verify_build(identity)


@pytest.mark.parametrize("kind,group", [("runtime", "containers"), ("artifact", "initContainers")])
@pytest.mark.parametrize("stage", ["render", "deployment", "pod"])
def test_substituted_or_unrendered_image_fails_deployment(
    monkeypatch, build_evidence, github_run, identity, pod, kind, group, stage
):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "eks-dev")
    monkeypatch.setenv("KUBE_CONTEXT", "test-cluster")
    monkeypatch.setenv("KUBE_NAMESPACE", "release-test")
    changed = deepcopy(pod)
    changed["spec"][group][0]["image"] = (
        f"{kind.upper()}_IMAGE_REQUIRED" if stage == "render" else "other/repo@sha256:" + "c" * 64
    )
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        if "set" in args or ("get" in args and "deployment" in args):
            spec = changed["spec"] if (
                (stage == "render" and "set" in args) or
                (stage == "deployment" and "get" in args)
            ) else pod["spec"]
            return json.dumps({"spec": {"template": {"metadata": {}, "spec": spec}}})
        if "get" in args and "pods" in args:
            return json.dumps({"items": [changed]})
        return ""

    monkeypatch.setattr(release, "command", fake_command)
    with pytest.raises(ValueError, match="image changed"):
        release.deploy()
    report = release.read("eks-dev.json")
    assert report["deployment_result"] == "failure"
    assert report["checks"].get("image") != "success"
    assert not any("scripts/smoke_test.py" in call for call in calls)
    if stage == "render":
        assert not any("apply" in call for call in calls)


@pytest.mark.parametrize("group", ["containerStatuses", "initContainerStatuses"])
@pytest.mark.parametrize("image_id", ["", "sha256:" + "c" * 64, "bad-prefix-sha256:" + "b" * 64])
def test_pod_status_requires_both_approved_digests(identity, pod, group, image_id):
    pod["status"][group][0]["imageID"] = image_id
    with pytest.raises(ValueError, match="imageID"):
        release.verify_pods(identity, [pod])


@pytest.mark.parametrize("state", [
    {}, {"waiting": {"reason": "ImagePullBackOff"}}, {"running": {}},
    {"terminated": {"exitCode": 1}},
])
def test_artifact_init_must_finish_successfully(identity, pod, state):
    pod["status"]["initContainerStatuses"][0]["state"] = state
    with pytest.raises(ValueError, match="init container"):
        release.verify_pods(identity, [pod])


@pytest.mark.parametrize("field,value", [
    ("phase", "Pending"), ("conditions", []),
    ("conditions", [{"type": "Ready", "status": "False"}]),
    ("containerStatuses", []), ("initContainerStatuses", []),
])
def test_pod_readiness_and_status_are_required(identity, pod, field, value):
    pod["status"][field] = value
    with pytest.raises(ValueError):
        release.verify_pods(identity, [pod])


def test_runtime_must_be_ready(identity, pod):
    pod["status"]["containerStatuses"][0]["ready"] = False
    with pytest.raises(ValueError, match="not Ready"):
        release.verify_pods(identity, [pod])


def test_missing_or_terminating_pods_are_not_success(identity, pod):
    with pytest.raises(ValueError):
        release.verify_pods(identity, [])
    pod["metadata"]["deletionTimestamp"] = "2026-10-01T00:00:00Z"
    with pytest.raises(ValueError):
        release.verify_pods(identity, [pod])


@pytest.mark.parametrize("image_id_style", ["exact", "bare", "docker-pullable", "docker", "containerd"])
def test_both_pod_digests_accept_manifest_id_formats(identity, pod, image_id_style):
    for kind, group in (("runtime", "containerStatuses"), ("artifact", "initContainerStatuses")):
        pod["status"][group][0]["imageID"] = (
            image_id_style + "://" + identity[f"{kind}_digest"]
            if image_id_style in {"docker", "containerd"} else
            identity[f"{kind}_digest"] if image_id_style == "bare" else
            ("docker-pullable://" if image_id_style == "docker-pullable" else "") +
            identity[f"exact_{kind}_image"]
        )
    release.verify_pods(identity, [pod])


@pytest.mark.parametrize("kind", ["runtime", "artifact"])
def test_production_candidate_must_match_validated_pair(monkeypatch, audit_evidence, identity, kind):
    candidate = release.read("candidate.json")
    candidate[f"{kind}_digest"] = "sha256:" + "c" * 64
    candidate[f"exact_{kind}_image"] = candidate[f"{kind}_image_name"] + "@" + candidate[f"{kind}_digest"]
    release.write("candidate.json", candidate)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "production")
    command = Mock()
    monkeypatch.setattr(release, "command", command)
    with pytest.raises(ValueError, match="candidate identity mismatch"):
        release.deploy()
    command.assert_not_called()
    assert release.read("production.json")["deployment_result"] == "failure"
    with pytest.raises(ValueError, match="audit evidence"):
        release.summary()


def test_candidate_records_exact_pair(
    monkeypatch, build_evidence, github_run, identity, eks_dev, protection
):
    release.write("eks-dev.json", eks_dev)
    monkeypatch.setattr(release, "github_json", Mock(side_effect=[
        protection, {"total_count": 1, "branch_policies": [{"name": "v*"}]},
    ]))
    release.candidate()
    candidate = release.read("candidate.json")
    assert all(candidate[key] == identity[key] for key in release.IDENTITY_KEYS)
    assert candidate["approval_state"] == "pending_human_review"


@pytest.mark.skipif(shutil.which("kubectl") is None, reason="Optional local kubectl render check")
def test_real_kubectl_renders_dual_image_manifest_locally(monkeypatch, tmp_path, identity):
    # No cluster/API access; validate the checked-in topology with the real local renderer.
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "no-kubeconfig"))
    monkeypatch.chdir(Path(__file__).resolve().parents[2])
    rendered = json.loads(release.command(
        "kubectl", "set", "image", "--local", "-f", "k8s/deployment.yaml",
        f'model-service={identity["exact_runtime_image"]}',
        f'model-artifact={identity["exact_artifact_image"]}', "-o", "json",
    ))
    spec = rendered["spec"]["template"]["spec"]
    release.verify_images(identity, spec)
    assert rendered["metadata"]["name"] == "model-service"
    assert "replicas" not in rendered["spec"]
    assert spec["securityContext"] == {
        "runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
    }
    assert spec["volumes"] == [{"name": "model-volume", "emptyDir": {}}]
    for group, name, read_only in (("containers", "model-service", True),
                                   ("initContainers", "model-artifact", False)):
        container = release.named(spec[group], name)
        assert len(container["volumeMounts"]) == 1
        mount = container["volumeMounts"][0]
        assert mount["name"] == "model-volume" and mount["mountPath"] == "/models"
        # kubectl omits false/default fields when serializing the typed object.
        assert mount.get("readOnly", False) is read_only
    runtime = release.named(spec["containers"], "model-service")
    assert runtime["readinessProbe"]["httpGet"]["path"] == "/health/ready"
    assert runtime["livenessProbe"]["httpGet"]["path"] == "/health/live"


def test_shared_workload_deployment_accepts_dev_identity_without_fabricated_release(
    monkeypatch, identity, pod
):
    pair = {key: identity[key] for key in (
        "runtime_digest", "exact_runtime_image", "artifact_digest", "exact_artifact_image",
    )}
    annotations = {"dev.model-service/source-digest": "sha256:" + "e" * 64}
    applied = []

    def fake_command(*args, **kwargs):
        if "set" in args:
            return json.dumps({"spec": {"template": {"metadata": {}, "spec": pod["spec"]}}})
        if "apply" in args and "input" in kwargs:
            applied.append(json.loads(kwargs["input"]))
        if "get" in args and "deployment" in args:
            return json.dumps({"spec": {"template": {"spec": pod["spec"]}}})
        if "get" in args and "pods" in args:
            return json.dumps({"items": [pod]})
        return ""

    forward = Mock()
    forward.poll.return_value = None
    monkeypatch.setattr(release, "command", fake_command)
    monkeypatch.setattr(release.subprocess, "Popen", lambda *a, **k: forward)
    monkeypatch.setattr(release.socket, "create_connection", lambda *a, **k: release.socket.socket())
    github = Mock(side_effect=AssertionError("Shared workload function must not access GitHub identity"))
    monkeypatch.setattr(release, "bound_release", github)
    report = {"production_eligible": False}
    release.deploy_workload(pair, context="test-dev-cluster", namespace="dev", target="eks-dev",
                            report=report, annotations=annotations)
    assert report["deployment_result"] == "success"
    assert report["production_eligible"] is False
    assert report["checks"] == {k: "success" for k in ("apply", "rollout", "readiness", "image", "smoke")}
    assert applied[0]["spec"]["template"]["metadata"]["annotations"] == annotations
    assert "replicas" not in applied[0]["spec"]
    assert "approval_state" not in report and "release_version" not in report
    github.assert_not_called()
    forward.terminate.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("runtime_digest", "sha256:short"), ("artifact_digest", "sha256:" + "c" * 64),
    ("exact_runtime_image", "example.test/model:latest"),
    ("exact_artifact_image", "example.test/model:latest"),
])
def test_shared_deployment_rejects_unpinned_or_mismatched_pair_before_kubectl(
    monkeypatch, identity, field, value
):
    identity[field] = value
    command = Mock()
    monkeypatch.setattr(release, "command", command)
    report = {"deployment_result": "success", "checks": {"apply": "success"}}
    with pytest.raises(ValueError, match="deployment identity"):
        release.deploy_workload(identity, context="test-dev-cluster", namespace="dev", target="eks-dev",
                                report=report, annotations={})
    command.assert_not_called()
    assert report["deployment_result"] == "failure" and report["checks"] == {}


def test_renderer_cannot_take_hpa_replica_ownership(monkeypatch, identity, pod):
    renderer = Mock(return_value=json.dumps({
        "spec": {"replicas": 1, "template": {"metadata": {}, "spec": pod["spec"]}},
    }))
    monkeypatch.setattr(release, "command", renderer)
    with pytest.raises(ValueError, match="HPA replica ownership"):
        release.deploy_workload(identity, context="test-dev-cluster", namespace="dev", target="eks-dev",
                                report={}, annotations={})
    assert renderer.call_count == 1
    assert "--local" in renderer.call_args.args


def test_release_identity_gate_still_runs_before_shared_deployment(monkeypatch, tmp_path):
    monkeypatch.setattr(release, "EVIDENCE", tmp_path)
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "eks-dev")
    monkeypatch.setattr(release, "bound_release", Mock(side_effect=ValueError("unverified release")))
    shared = Mock()
    monkeypatch.setattr(release, "deploy_workload", shared)
    with pytest.raises(ValueError, match="unverified release"):
        release.deploy()
    shared.assert_not_called()
    assert release.read("eks-dev.json")["deployment_result"] == "failure"
