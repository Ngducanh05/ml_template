"""Week 2 release identity, evidence and digest-only Kubernetes deployment.

Standard library only; no model-specific decisions. GitHub owns approval.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

EVIDENCE = Path("evidence")
VERSION = r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
DIGEST = r"sha256:[0-9a-f]{64}"
IMAGE_NAME = r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9]+(?:[._/-][a-z0-9]+)*"
IDENTITY_KEYS = (
    "git_sha", "git_short_sha", "release_version", "image_name", "image_tag",
    "image_digest", "exact_deploy_image", "workflow_run", "ticket_reference",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    require(bool(value), f"{name} is required")
    return value


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def write(name: str, value: dict) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / name).write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def read(name: str) -> dict:
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def output(**values: str) -> None:
    with open(env("GITHUB_OUTPUT"), "a", encoding="utf-8") as stream:
        for key, value in values.items():
            require("\n" not in value and "\r" not in value, "Invalid output")
            stream.write(f"{key}={value}\n")


def command(*args: str, **kwargs) -> str:
    # check=True is essential on Windows: a native failure must stop deployment.
    result = subprocess.run(
        args, check=True, text=True, encoding="utf-8",
        stdout=subprocess.PIPE, timeout=180, **kwargs,
    )
    return result.stdout.strip()


def first_attempt() -> None:
    require(env("GITHUB_RUN_ATTEMPT") == "1",
            "Release reruns are disabled; use a new source commit and release version")


def validate_identity(data: dict) -> None:
    for key in IDENTITY_KEYS:
        require(isinstance(data.get(key), str) and bool(data[key].strip()),
                f"Missing release identity: {key}")
    require(re.fullmatch(r"[0-9a-f]{40}", data["git_sha"]) is not None, "Invalid SHA")
    require(data["git_short_sha"] == data["git_sha"][:12], "Short SHA mismatch")
    require(re.fullmatch(VERSION, data["release_version"]) is not None, "Invalid version")
    require(re.fullmatch(IMAGE_NAME, data["image_name"]) is not None, "Invalid image name")
    require(data["image_tag"] == f'm-{data["git_short_sha"]}', "Build tag mismatch")
    require(re.fullmatch(DIGEST, data["image_digest"]) is not None, "Missing/invalid digest")
    require(data["exact_deploy_image"] == f'{data["image_name"]}@{data["image_digest"]}',
            "Deployment identity mismatch")
    require(len(data["ticket_reference"]) <= 256 and
            all(ord(c) >= 32 and ord(c) != 127 for c in data["ticket_reference"]),
            "Ticket must be a short non-sensitive single-line reference")


def prepare() -> None:
    first_attempt()
    require(env("GITHUB_EVENT_NAME") == "workflow_dispatch", "Disallowed release trigger")
    version = env("GITHUB_REF_NAME")
    require(env("GITHUB_REF") == f"refs/tags/{version}" and
            re.fullmatch(VERSION, version) is not None, "Select a vX.Y.Z release tag")
    sha = command("git", "rev-parse", "HEAD")
    require(sha == env("GITHUB_SHA"), "Checkout does not match release SHA")
    command("git", "merge-base", "--is-ancestor", sha, "origin/main")
    registry = env("JFROG_REGISTRY")
    name = f'{registry}/{env("JFROG_IMAGE_PATH")}'
    severity = env("TRIVY_BLOCKING_SEVERITIES")
    require(set(severity.split(",")) <= {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"},
            "Configure a non-empty comma-separated Trivy severity list")
    unfixed = env("TRIVY_IGNORE_UNFIXED")
    require(unfixed in {"true", "false"}, "TRIVY_IGNORE_UNFIXED must be true or false")
    data = {
        "schema_version": 1, "git_sha": sha, "git_short_sha": sha[:12],
        "release_version": version, "image_name": name, "image_tag": f"m-{sha[:12]}",
        "image_digest": "", "exact_deploy_image": "",
        "workflow_run": f'{env("GITHUB_SERVER_URL")}/{env("GITHUB_REPOSITORY")}/actions/runs/{env("GITHUB_RUN_ID")}',
        "ticket_reference": env("TICKET_REFERENCE"),
        "security_policy": {"blocking_severities": severity, "ignore_unfixed": unfixed},
        "timestamp": timestamp(),
    }
    # Validate all fields before publishing anything, using a temporary digest.
    validate_identity({**data, "image_digest": "sha256:" + "0" * 64,
                       "exact_deploy_image": name + "@sha256:" + "0" * 64})
    write("release.json", data)
    output(image_name=name, image_tag=data["image_tag"], release_version=version)


def registry_check() -> None:
    data = read("release.json")
    registry, path = data["image_name"].split("/", 1)
    auth = base64.b64encode(
        f'{env("JFROG_USERNAME")}:{env("JFROG_TOKEN")}'.encode()
    ).decode()
    # JFrog Docker V2 endpoint, using a scoped username/access-token pair.
    # Only an explicit 404 means absent. Auth/network errors never mean absent.
    for tag in (data["release_version"], data["image_tag"]):
        request = Request(f"https://{registry}/v2/{path}/manifests/{tag}", method="HEAD",
                          headers={"Authorization": f"Basic {auth}",
                                   "Accept": "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
        try:
            with urlopen(request, timeout=30):
                raise ValueError("Image tag already exists; release identities cannot be reused")
        except HTTPError as exc:
            require(exc.code == 404, f"Registry identity check failed (HTTP {exc.code})")


def pin() -> None:
    data = read("release.json")
    data["image_digest"] = env("IMAGE_DIGEST")
    data["exact_deploy_image"] = f'{data["image_name"]}@{data["image_digest"]}'
    validate_identity(data)
    write("release.json", data)
    output(exact_deploy_image=data["exact_deploy_image"])


def verify_build(data: dict) -> None:
    validate_identity(data)
    require(data.get("tests_result") == "success" and data.get("push_result") == "success"
            and data.get("security_result") == "success", "Release prerequisites did not pass")
    scan = read("trivy.json")
    require(scan.get("ArtifactName") == data["exact_deploy_image"] and
            isinstance(scan.get("Results"), list) and bool(scan["Results"]),
            "Missing scan evidence for the exact deployment image")
    suites = ET.parse(EVIDENCE / "tests.xml").getroot()
    require(sum(int(s.get("tests", "0")) for s in suites.iter("testsuite")) > 0,
            "Missing test evidence")
    require(all(int(s.get("failures", "0")) == 0 and int(s.get("errors", "0")) == 0
                for s in suites.iter("testsuite")), "Test evidence contains failures")


def finalize_build() -> None:
    data = read("release.json")
    data.update(tests_result=os.environ.get("TEST_RESULT", "not_run"),
                push_result=os.environ.get("PUSH_RESULT", "not_run"),
                security_result=os.environ.get("SCAN_RESULT", "not_run"),
                timestamp=timestamp())
    write("release.json", data)
    verify_build(data)


def github_json(suffix: str) -> dict:
    request = Request(
        f'{env("GITHUB_API_URL")}/repos/{env("GITHUB_REPOSITORY")}/{suffix}',
        headers={"Authorization": f'Bearer {env("GH_TOKEN")}',
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def verify_protection(protection: dict, policies: dict) -> None:
    reviewers = [r for r in protection.get("protection_rules", [])
                 if r.get("type") == "required_reviewers"]
    require(any(r.get("reviewers") and r.get("prevent_self_review") is True
                for r in reviewers), "Production requires reviewers and prevent self-review")
    require(protection.get("can_admins_bypass") is False,
            "Production administrator bypass must be disabled")
    require(protection.get("deployment_branch_policy") ==
            {"protected_branches": False, "custom_branch_policies": True},
            "Production must use selected deployment tags")
    rules = policies.get("branch_policies", [])
    require(policies.get("total_count") == 1 and len(rules) == 1 and
            rules[0].get("type") == "tag" and rules[0].get("name") == "v*",
            "Production must allow only the v* tag rule (strict SemVer checked separately)")


def bound_release() -> dict:
    first_attempt()
    data = read("release.json")
    verify_build(data)
    require(data["git_sha"] == env("GITHUB_SHA"), "Evidence/source SHA mismatch")
    require(data["exact_deploy_image"] == env("EXACT_DEPLOY_IMAGE"), "Digest changed between jobs")
    require(data["workflow_run"] ==
            f'{env("GITHUB_SERVER_URL")}/{env("GITHUB_REPOSITORY")}/actions/runs/{env("GITHUB_RUN_ID")}',
            "Evidence belongs to a different release run")
    return data


def verify_validation(data: dict, validation: dict) -> None:
    require(all(validation.get(k) == data[k] for k in IDENTITY_KEYS),
            "Validation used a different release identity")
    require(validation.get("deployment_environment") == "validation" and
            validation.get("deployment_result") == "success" and
            all(validation.get("checks", {}).get(k) == "success"
                for k in ("apply", "rollout", "readiness", "image", "smoke")),
            "Validation evidence is incomplete or failed")


def candidate() -> None:
    data = bound_release()
    verify_validation(data, read("validation.json"))
    verify_protection(github_json("environments/production"),
                      github_json("environments/production/deployment-branch-policies"))
    data.update(approval_state="pending_human_review", validation_result="success",
                ticket_validation="reference_present; approval must be verified by human",
                deployment_environment="production", deployment_result="not_started",
                timestamp=timestamp())
    write("candidate.json", data)
    # JSON avoids rendering a user-supplied ticket as Markdown/HTML.
    print(json.dumps(data, indent=2, sort_keys=True))


def deploy() -> None:
    target = env("DEPLOYMENT_ENVIRONMENT")
    require(target in {"validation", "production"}, "Unknown deployment environment")
    report = {"deployment_environment": target, "deployment_result": "failure", "checks": {}}
    kube_file = None
    try:
        data = bound_release()
        report.update(data)
        if target == "production":
            approved = read("candidate.json")
            require(all(approved.get(k) == data[k] for k in IDENTITY_KEYS),
                    "Production candidate identity mismatch")
            require(approved.get("approval_state") == "pending_human_review",
                    "Missing production candidate")
            verify_validation(data, read("validation.json"))
            # Only the protected production job calls this path. This string does
            # not purport to retrieve the reviewer identity or ticket approval.
            report["approval_state"] = "environment_gate_passed"
            raw = base64.b64decode(env("KUBECONFIG_B64"), validate=True)
            descriptor, kube_file = tempfile.mkstemp(prefix="model-service-kube-")
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
            os.environ["KUBECONFIG"] = kube_file
        else:
            report["approval_state"] = "not_applicable"
        context, namespace = env("KUBE_CONTEXT"), env("KUBE_NAMESPACE")
        require((target == "validation" and context == "docker-desktop") or
                (target == "production" and context != "docker-desktop"),
                "Docker Desktop is validation, never production")
        require(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace) is not None,
                "Invalid namespace")
        report.update(kubernetes_context=context, kubernetes_namespace=namespace)
        prefix = ("kubectl", "--context", context, "--namespace", namespace,
                  "--request-timeout=30s")

        def kube(*args: str, **kwargs) -> str:
            return command(*prefix, *args, **kwargs)

        # Render first, then apply: the placeholder image is never sent to the API.
        rendered = json.loads(command(
            "kubectl", "set", "image", "--local", "-f", "k8s/deployment.yaml",
            f'model-service={data["exact_deploy_image"]}', "-o", "json",
        ))
        rendered["spec"]["template"]["metadata"].setdefault("annotations", {}).update({
            "release.model-service/version": data["release_version"],
            "release.model-service/git-sha": data["git_sha"],
            "release.model-service/workflow-run": data["workflow_run"],
        })
        kube("apply", "-f", "k8s/configmap.yaml", "-f", "k8s/service.yaml")
        kube("apply", "-f", "-", input=json.dumps(rendered))
        report["checks"]["apply"] = "success"
        kube("rollout", "status", "deployment/model-service", "--timeout=120s")
        report["checks"]["rollout"] = "success"
        kube("wait", "--for=condition=Available", "deployment/model-service", "--timeout=120s")
        kube("wait", "--for=condition=Ready", "pod", "-l",
             "app.kubernetes.io/name=model-service", "--timeout=120s")
        report["checks"]["readiness"] = "success"
        deployment = json.loads(kube("get", "deployment", "model-service", "-o", "json"))
        require(deployment["spec"]["template"]["spec"]["containers"][0]["image"] ==
                data["exact_deploy_image"], "Deployment image changed")
        pods = json.loads(kube("get", "pods", "-l", "app.kubernetes.io/name=model-service",
                               "-o", "json"))["items"]
        pods = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        require(bool(pods), "No running release pods")
        for pod in pods:
            container = next(c for c in pod["spec"]["containers"] if c["name"] == "model-service")
            state = next(c for c in pod["status"]["containerStatuses"] if c["name"] == "model-service")
            require(container["image"] == data["exact_deploy_image"] and state["ready"]
                    and state["imageID"].endswith(data["image_digest"]),
                    "Running pod is not ready on the approved digest")
        report["checks"]["image"] = "success"
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryFile() as log:
            forward = subprocess.Popen(
                [*prefix, "port-forward", "service/model-service", f"{port}:8000",
                 "--address=127.0.0.1"], stdout=log, stderr=log, creationflags=flags,
            )
            try:
                for _ in range(60):
                    require(forward.poll() is None, "Port-forward exited unexpectedly")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=1):
                            break
                    except OSError:
                        time.sleep(0.5)
                else:
                    raise ValueError("Port-forward did not become ready")
                print(command(sys.executable, "scripts/smoke_test.py", "--base-url",
                              f"http://127.0.0.1:{port}"))
                report["checks"]["smoke"] = "success"
            finally:
                forward.terminate()
                forward.wait(timeout=10)
        report["deployment_result"] = "success"
    finally:
        try:
            if kube_file:
                Path(kube_file).unlink(missing_ok=True)
        finally:
            report["timestamp"] = timestamp()
            write(f"{target}.json", report)


def summary() -> None:
    needs = json.loads(env("NEEDS_JSON"))
    results = {name: job["result"] for name, job in needs.items()}
    data = {
        "git_sha": env("GITHUB_SHA"), "release_version": env("GITHUB_REF_NAME"),
        "ticket_reference": os.environ.get("TICKET_REFERENCE", "").strip(),
        "workflow_run": f'{env("GITHUB_SERVER_URL")}/{env("GITHUB_REPOSITORY")}/actions/runs/{env("GITHUB_RUN_ID")}',
        "job_results": results, "deployment_environment": "production",
        "deployment_result": results.get("production", "not_started"),
        "release_result": "not_successful",
        "approval_state": "see_github_environment_history",
        "timestamp": timestamp(),
    }
    # Missing artifacts are deliberately not interpreted as success.
    for filename in ("release.json", "production.json"):
        if (EVIDENCE / filename).exists():
            evidence = read(filename)
            for key in (*IDENTITY_KEYS, "security_result", "approval_state"):
                if key in evidence:
                    data[key] = evidence[key]
    if all(results.get(job) == "success" for job in ("build", "validation", "candidate", "production")):
        try:
            built, deployed = read("release.json"), read("production.json")
            verify_build(built)
            verify_validation(built, read("validation.json"))
            candidate_evidence = read("candidate.json")
            require(all(deployed.get(k) == built[k] == candidate_evidence.get(k)
                        for k in IDENTITY_KEYS), "Final audit identity mismatch")
            require(deployed.get("deployment_result") == "success" and
                    deployed.get("deployment_environment") == "production" and
                    deployed.get("approval_state") == "environment_gate_passed" and
                    all(deployed.get("checks", {}).get(k) == "success"
                        for k in ("apply", "rollout", "readiness", "image", "smoke")),
                    "Missing successful production evidence")
            data["release_result"] = "success"
        except (OSError, ValueError, KeyError, ET.ParseError):
            data["audit_result"] = "required_evidence_missing_or_invalid"
    write("summary.json", data)
    print(json.dumps(data, indent=2, sort_keys=True))
    require(data.get("audit_result") is None, "Final audit evidence is incomplete")


if __name__ == "__main__":
    actions = {"prepare": prepare, "registry-check": registry_check, "pin": pin,
               "finalize-build": finalize_build, "candidate": candidate,
               "deploy": deploy, "summary": summary}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=actions)
    try:
        actions[parser.parse_args().action]()
    except (ValueError, KeyError, OSError, URLError, subprocess.SubprocessError, ET.ParseError) as exc:
        # Do not print HTTP response bodies, kubeconfig, environment or credentials.
        print(f"Release blocked: {type(exc).__name__}: "
              f"{str(exc) if isinstance(exc, ValueError) else 'operation failed; inspect the failed step'}",
              file=sys.stderr)
        raise SystemExit(1)
