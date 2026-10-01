"""Week 3 runtime/artifact release identity and digest-pair deployment.

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
from urllib.error import URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

EVIDENCE = Path("evidence")
VERSION = r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
DIGEST = r"sha256:[0-9a-f]{64}"
IMAGE_NAME = r"[a-z0-9.-]+(?::[0-9]+)?/[a-z0-9]+(?:[._/-][a-z0-9]+)*"
STRING_IDENTITY_KEYS = (
    "git_sha", "git_short_sha", "release_version",
    "runtime_image_name", "runtime_digest", "exact_runtime_image",
    "artifact_image_name", "artifact_tag", "artifact_digest", "exact_artifact_image",
    "workflow_run", "ticket_reference",
)
IDENTITY_KEYS = ("schema_version", *STRING_IDENTITY_KEYS, "security_policy")


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


def validate_identity(data: dict, *, allow_unpinned: bool = False) -> None:
    require(data.get("schema_version") == 2, "Unsupported release schema")
    unpinned = (allow_unpinned and data.get("artifact_digest") == ""
                and data.get("exact_artifact_image") == "")
    for key in STRING_IDENTITY_KEYS:
        if unpinned and key in {"artifact_digest", "exact_artifact_image"}:
            continue
        require(isinstance(data.get(key), str) and bool(data[key].strip()),
                f"Missing release identity: {key}")
    require(re.fullmatch(r"[0-9a-f]{40}", data["git_sha"]) is not None, "Invalid SHA")
    require(data["git_short_sha"] == data["git_sha"][:12], "Short SHA mismatch")
    require(re.fullmatch(VERSION, data["release_version"]) is not None, "Invalid version")
    for kind in ("runtime", "artifact"):
        require(re.fullmatch(IMAGE_NAME, data[f"{kind}_image_name"]) is not None,
                f"Invalid {kind} image name")
        if kind == "artifact" and unpinned:
            continue
        require(re.fullmatch(DIGEST, data[f"{kind}_digest"]) is not None,
                f"Missing/invalid {kind} digest")
        require(data[f"exact_{kind}_image"] ==
                f'{data[f"{kind}_image_name"]}@{data[f"{kind}_digest"]}',
                f"{kind} deployment identity mismatch")
    require(data["artifact_tag"] == f'm-{data["git_short_sha"]}', "Build tag mismatch")
    require(len(data["ticket_reference"]) <= 256 and
            all(ord(c) >= 32 and ord(c) != 127 for c in data["ticket_reference"]),
            "Ticket must be a short non-sensitive single-line reference")
    policy = data.get("security_policy")
    require(isinstance(policy, dict), "Missing security policy")
    severity = policy.get("blocking_severities")
    require(isinstance(severity, str) and bool(severity) and
            set(severity.split(",")) <= {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"},
            "Configure a non-empty comma-separated Trivy severity list")
    require(policy.get("ignore_unfixed") in {"true", "false"},
            "TRIVY_IGNORE_UNFIXED must be true or false")


def image_name(kind: str) -> str:
    account, region = env("AWS_ACCOUNT_ID"), env("AWS_REGION")
    require(re.fullmatch(r"[0-9]{12}", account) is not None, "Invalid AWS account ID")
    require(re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region) is not None,
            "Invalid AWS region")
    return f'{account}.dkr.ecr.{region}.amazonaws.com/{env(f"ECR_{kind.upper()}_REPOSITORY")}'


def prepare() -> None:
    first_attempt()
    require(env("GITHUB_EVENT_NAME") == "workflow_dispatch", "Disallowed release trigger")
    version = env("GITHUB_REF_NAME")
    require(env("GITHUB_REF") == f"refs/tags/{version}" and
            re.fullmatch(VERSION, version) is not None, "Select a vX.Y.Z release tag")
    sha = command("git", "rev-parse", "HEAD")
    require(sha == env("GITHUB_SHA"), "Checkout does not match release SHA")
    command("git", "merge-base", "--is-ancestor", sha, "origin/main")
    runtime_name, artifact_name = image_name("runtime"), image_name("artifact")
    runtime_digest = env("RUNTIME_IMAGE_DIGEST")
    data = {
        "schema_version": 2, "git_sha": sha, "git_short_sha": sha[:12],
        "release_version": version,
        "runtime_image_name": runtime_name, "runtime_digest": runtime_digest,
        "exact_runtime_image": f"{runtime_name}@{runtime_digest}",
        "artifact_image_name": artifact_name, "artifact_tag": f"m-{sha[:12]}",
        "artifact_digest": "", "exact_artifact_image": "",
        "workflow_run": f'{env("GITHUB_SERVER_URL")}/{env("GITHUB_REPOSITORY")}/actions/runs/{env("GITHUB_RUN_ID")}',
        "ticket_reference": env("TICKET_REFERENCE"),
        "security_policy": {"blocking_severities": env("TRIVY_BLOCKING_SEVERITIES"),
                            "ignore_unfixed": env("TRIVY_IGNORE_UNFIXED")},
        "timestamp": timestamp(),
    }
    validate_identity(data, allow_unpinned=True)
    write("release.json", data)
    output(artifact_image_name=artifact_name, artifact_tag=data["artifact_tag"],
           exact_runtime_image=data["exact_runtime_image"], release_version=version)


def registry_check() -> None:
    data = read("release.json")
    validate_identity(data, allow_unpinned=True)
    verify_run_identity(data)
    for kind in ("runtime", "artifact"):
        require(data[f"{kind}_image_name"] == image_name(kind), "ECR repository identity changed")
    require(data["runtime_digest"] == env("RUNTIME_IMAGE_DIGEST"), "Runtime digest changed")

    def describe(kind: str, image_id: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["aws", "ecr", "describe-images", "--region", env("AWS_REGION"),
                 "--registry-id", env("AWS_ACCOUNT_ID"),
                 "--repository-name", env(f"ECR_{kind.upper()}_REPOSITORY"),
                 "--image-ids", image_id,
                 "--output", "json"],
                check=False, capture_output=True, text=True, encoding="utf-8", timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            raise ValueError("ECR identity check failed") from None

    # Only the AWS ImageNotFoundException means an unused artifact tag.
    for tag in (data["release_version"], data["artifact_tag"]):
        result = describe("artifact", f"imageTag={tag}")
        require(result.returncode != 0,
                "Image tag already exists; release identities cannot be reused")
        require(re.search(r"^An error occurred \(ImageNotFoundException\) when calling the "
                          r"DescribeImages operation:", result.stderr.strip()) is not None,
                "ECR identity check failed")
    result = describe("runtime", f'imageDigest={data["runtime_digest"]}')
    require(result.returncode == 0, "ECR runtime digest lookup failed")
    details = json.loads(result.stdout).get("imageDetails", [])
    require(len(details) == 1 and details[0].get("imageDigest") == data["runtime_digest"]
            and details[0].get("repositoryName") == env("ECR_RUNTIME_REPOSITORY")
            and details[0].get("registryId") == env("AWS_ACCOUNT_ID"),
            "ECR runtime digest/repository identity mismatch")


def pin() -> None:
    data = read("release.json")
    validate_identity(data, allow_unpinned=True)
    verify_run_identity(data)
    require(data["artifact_digest"] == "" and data["exact_artifact_image"] == "",
            "Artifact digest is already pinned")
    data["artifact_digest"] = env("ARTIFACT_IMAGE_DIGEST")
    data["exact_artifact_image"] = f'{data["artifact_image_name"]}@{data["artifact_digest"]}'
    validate_identity(data)
    write("release.json", data)
    output(exact_runtime_image=data["exact_runtime_image"],
           exact_artifact_image=data["exact_artifact_image"])


def verify_build(data: dict) -> None:
    validate_identity(data)
    require(all(data.get(key) == "success" for key in (
        "tests_result", "artifact_push_result", "runtime_scan_result", "artifact_scan_result",
    )), "Release prerequisites did not pass")
    for kind in ("runtime", "artifact"):
        scan = read(f"trivy-{kind}.json")
        require(scan.get("ArtifactName") == data[f"exact_{kind}_image"] and
                isinstance(scan.get("Results"), list) and bool(scan["Results"]),
                f"Missing {kind} scan evidence for the exact image")
    suites = ET.parse(EVIDENCE / "tests.xml").getroot()
    require(sum(int(s.get("tests", "0")) for s in suites.iter("testsuite")) > 0,
            "Missing test evidence")
    require(all(int(s.get("failures", "0")) == 0 and int(s.get("errors", "0")) == 0
                for s in suites.iter("testsuite")), "Test evidence contains failures")


def finalize_build() -> None:
    data = read("release.json")
    data.update(tests_result=os.environ.get("TEST_RESULT", "not_run"),
                artifact_push_result=os.environ.get("ARTIFACT_PUSH_RESULT", "not_run"),
                runtime_scan_result=os.environ.get("RUNTIME_SCAN_RESULT", "not_run"),
                artifact_scan_result=os.environ.get("ARTIFACT_SCAN_RESULT", "not_run"),
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
    reviewers = [
        rule
        for rule in protection.get("protection_rules", [])
        if rule.get("type") == "required_reviewers"
    ]

    require(
        any(
            rule.get("reviewers")
            and rule.get("prevent_self_review") is True
            for rule in reviewers
        ),
        "Production requires reviewers and prevent self-review",
    )

    require(
        protection.get("deployment_branch_policy")
        == {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
        "Production must use selected deployment policies",
    )

    rules = policies.get("branch_policies", [])

    require(
        policies.get("total_count") == 1
        and len(rules) == 1
        and rules[0].get("name") == "v*",
        "Production must allow only the v* release policy",
    )


def verify_run_identity(data: dict) -> None:
    first_attempt()
    require(data["git_sha"] == env("GITHUB_SHA"), "Evidence/source SHA mismatch")
    require(data["release_version"] == env("GITHUB_REF_NAME") and
            env("GITHUB_REF") == f'refs/tags/{data["release_version"]}',
            "Evidence/release version mismatch")
    require(data["ticket_reference"] == env("TICKET_REFERENCE"), "Evidence/ticket mismatch")
    require(data["workflow_run"] ==
            f'{env("GITHUB_SERVER_URL")}/{env("GITHUB_REPOSITORY")}/actions/runs/{env("GITHUB_RUN_ID")}',
            "Evidence belongs to a different release run")


def bound_release() -> dict:
    data = read("release.json")
    verify_build(data)
    verify_run_identity(data)
    for kind in ("runtime", "artifact"):
        require(data[f"exact_{kind}_image"] == env(f"EXACT_{kind.upper()}_IMAGE"),
                f"{kind} digest changed between jobs")
    return data


def verify_validation(data: dict, validation: dict, expected_environment: str) -> None:
    require(all(validation.get(k) == data[k] for k in IDENTITY_KEYS),
            "Validation used a different release identity")
    require(validation.get("deployment_environment") == expected_environment and
            validation.get("deployment_result") == "success" and
            all(validation.get("checks", {}).get(k) == "success"
                for k in ("apply", "rollout", "readiness", "image", "smoke")),
            "Validation evidence is incomplete or failed")


def candidate() -> None:
    data = bound_release()
    verify_validation(data, read("eks-dev.json"), "eks-dev")
    verify_protection(github_json("environments/production"),
                      github_json("environments/production/deployment-branch-policies"))
    data.update(approval_state="pending_human_review", validation_result="success",
                ticket_validation="reference_present; approval must be verified by human",
                deployment_environment="production", deployment_result="not_started",
                timestamp=timestamp())
    write("candidate.json", data)
    # JSON avoids rendering a user-supplied ticket as Markdown/HTML.
    print(json.dumps(data, indent=2, sort_keys=True))


def named(items: list, name: str) -> dict:
    matches = [item for item in items if item.get("name") == name]
    require(len(matches) == 1, f"Missing or ambiguous container: {name}")
    return matches[0]


def verify_images(data: dict, spec: dict) -> None:
    for kind, group, name in (("runtime", "containers", "model-service"),
                              ("artifact", "initContainers", "model-artifact")):
        require(named(spec.get(group, []), name).get("image") == data[f"exact_{kind}_image"],
                f"{kind} image changed")
    require(all(c.get("image") not in {"RUNTIME_IMAGE_REQUIRED", "ARTIFACT_IMAGE_REQUIRED"}
                for group in ("containers", "initContainers") for c in spec.get(group, [])),
            "Unrendered image placeholder")


def verify_pods(data: dict, pods: list) -> None:
    pods = [p for p in pods if not p.get("metadata", {}).get("deletionTimestamp")]
    require(bool(pods), "No running release pods")
    for pod in pods:
        verify_images(data, pod["spec"])
        status = pod.get("status", {})
        require(status.get("phase") == "Running" and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        ), "Release pod is not Ready")
        for kind, group, name in (("runtime", "containerStatuses", "model-service"),
                                  ("artifact", "initContainerStatuses", "model-artifact")):
            state = named(status.get(group, []), name)
            # Retain the existing exact manifest digest check for both images.
            # An index/config digest is not silently accepted as an equivalent image.
            image_id = state.get("imageID", "")
            for prefix in ("docker-pullable://", "docker://", "containerd://"):
                if image_id.startswith(prefix):
                    image_id = image_id.removeprefix(prefix)
                    break
            require(image_id == data[f"{kind}_digest"] or
                    image_id == data[f"exact_{kind}_image"],
                    f"Running {kind} imageID does not match approved digest")
            if kind == "runtime":
                require(state.get("ready") is True, "Runtime container is not Ready")
            else:
                require(state.get("state", {}).get("terminated", {}).get("exitCode") == 0,
                        "Artifact init container did not complete successfully")


def deploy() -> None:
    target = env("DEPLOYMENT_ENVIRONMENT")
    require(target in {"validation", "eks-dev", "production"}, "Unknown deployment environment")
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
            verify_validation(data, read("eks-dev.json"), "eks-dev")
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
                (target in {"eks-dev", "production"} and context != "docker-desktop"),
                "Docker Desktop is only for local validation")
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
            f'model-service={data["exact_runtime_image"]}',
            f'model-artifact={data["exact_artifact_image"]}', "-o", "json",
        ))
        verify_images(data, rendered["spec"]["template"]["spec"])
        rendered["spec"]["template"]["metadata"].setdefault("annotations", {}).update({
            "release.model-service/version": data["release_version"],
            "release.model-service/git-sha": data["git_sha"],
            "release.model-service/workflow-run": data["workflow_run"],
        })
        if target == "eks-dev":
            rendered["spec"]["template"]["spec"].pop("imagePullSecrets", None)
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
        verify_images(data, deployment["spec"]["template"]["spec"])
        pods = json.loads(kube("get", "pods", "-l", "app.kubernetes.io/name=model-service",
                               "-o", "json"))["items"]
        verify_pods(data, pods)
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
            for key in (*IDENTITY_KEYS, "runtime_scan_result", "artifact_scan_result", "approval_state"):
                if key in evidence:
                    data[key] = evidence[key]
    if all(results.get(job) == "success" for job in ("build", "eks_dev", "candidate", "production")):
        try:
            built, deployed = bound_release(), read("production.json")
            verify_validation(built, read("eks-dev.json"), "eks-dev")
            candidate_evidence = read("candidate.json")
            require(all(deployed.get(k) == built[k] == candidate_evidence.get(k)
                        for k in IDENTITY_KEYS), "Final audit identity mismatch")
            require(candidate_evidence.get("approval_state") == "pending_human_review",
                    "Missing eligible candidate")
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
