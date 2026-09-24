"""Failure-path tests for Week 2 release controls (no registry/cluster required)."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import subprocess

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


@pytest.mark.parametrize("stage", ["apply", "rollout", "readiness", "image", "smoke"])
def test_incomplete_validation_cannot_create_candidate(identity, validation, stage):
    validation["checks"][stage] = "failure"
    with pytest.raises(ValueError):
        release.verify_validation(identity, validation)


def test_validation_cannot_substitute_another_digest(identity, validation):
    validation["image_digest"] = "sha256:" + "c" * 64
    with pytest.raises(ValueError, match="different release"):
        release.verify_validation(identity, validation)


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

@pytest.mark.parametrize("http_code", [401, 403, 429, 500, 503])
def test_registry_errors_never_mean_tag_absent(monkeypatch, build_evidence, http_code):
    from urllib.error import HTTPError
    monkeypatch.setenv("JFROG_USERNAME", "test-user")
    monkeypatch.setenv("JFROG_TOKEN", "test-token")

    def fail(*args, **kwargs):
        raise HTTPError("https://registry.example.test", http_code, "test", {}, None)

    monkeypatch.setattr(release, "urlopen", fail)
    with pytest.raises(ValueError, match="Registry identity check failed"):
        release.registry_check()


@pytest.mark.parametrize("stage", ["apply", "rollout", "wait", "smoke"])
def test_native_failure_stops_deployment_and_records_failure(
    monkeypatch, build_evidence, identity, stage
):
    monkeypatch.setenv("DEPLOYMENT_ENVIRONMENT", "validation")
    monkeypatch.setenv("KUBE_CONTEXT", "docker-desktop")
    monkeypatch.setenv("KUBE_NAMESPACE", "release-test")
    monkeypatch.setattr(release, "bound_release", lambda: identity)
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        if stage in args or (stage == "smoke" and "scripts/smoke_test.py" in args):
            raise subprocess.CalledProcessError(1, args)
        if "set" in args:
            return json.dumps({"spec": {"template": {"metadata": {}}}})
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
    with pytest.raises(subprocess.CalledProcessError):
        release.deploy()
    report = release.read("validation.json")
    assert report["deployment_result"] == "failure"
    assert report["checks"].get("smoke") != "success"
    if stage != "smoke":
        assert not any("scripts/smoke_test.py" in c for c in calls)


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


@pytest.mark.parametrize("state", ["failure", "cancelled", "skipped"])
def test_rejected_or_failed_production_never_records_release_success(
    monkeypatch, build_evidence, github_run, state
):
    needs = {k: {"result": "success"} for k in ("build", "validation", "candidate")}
    needs["production"] = {"result": state}
    monkeypatch.setenv("NEEDS_JSON", json.dumps(needs))
    release.summary()
    assert release.read("summary.json")["release_result"] == "not_successful"


def test_success_requires_final_evidence(monkeypatch, build_evidence, github_run):
    monkeypatch.setenv("NEEDS_JSON", json.dumps({
        k: {"result": "success"} for k in ("build", "validation", "candidate", "production")
    }))
    with pytest.raises(ValueError, match="audit evidence"):
        release.summary()
    assert release.read("summary.json")["release_result"] == "not_successful"


def test_complete_successful_audit(monkeypatch, build_evidence, github_run, identity, validation):
    monkeypatch.setenv("NEEDS_JSON", json.dumps({
        k: {"result": "success"} for k in ("build", "validation", "candidate", "production")
    }))
    release.write("validation.json", validation)
    release.write("candidate.json", {**identity, "approval_state": "pending_human_review"})
    release.write("production.json", {**validation, "deployment_environment": "production",
                                      "approval_state": "environment_gate_passed"})
    release.summary()
    assert release.read("summary.json")["release_result"] == "success"


def test_protection_api_timeout_does_not_create_candidate(
    monkeypatch, build_evidence, github_run, validation
):
    release.write("validation.json", validation)

    def timeout(*args):
        raise TimeoutError("test timeout")

    monkeypatch.setattr(release, "github_json", timeout)
    with pytest.raises(TimeoutError):
        release.candidate()
    assert not (build_evidence / "candidate.json").exists()
