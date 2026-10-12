"""Offline CI manifest contracts and network-disabled control-plane image smoke."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "sha256:" + "a" * 64
RUNTIME_IMAGE = "596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:" + "b" * 64
CONTROL_PLANE_IMAGE = "596432515999.dkr.ecr.ap-southeast-1.amazonaws.com/model-service@sha256:" + "c" * 64
STORAGE = "s3://mlflow-test-artifacts-quangcm/mlflow/workspaces/mlops/27/models/ci-fixture/artifacts"
LANES = {"credit-risk-stable-v2", "credit-risk-canary-v2"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_manifests(release, control):
    require(RUNTIME_IMAGE != CONTROL_PLANE_IMAGE, "Runtime and control-plane identities must remain separate")
    objects = release["items"] + control["items"]
    require(all(x["metadata"].get("namespace") == "production" for x in objects), "Unexpected namespace")
    states = [x for x in release["items"] if x["kind"] == "ConfigMap"]
    require(len(states) == 1 and states[0]["metadata"]["name"] == "model-release-state-v2", "Wrong state identity")
    state = json.loads(states[0]["data"]["state.json"])
    require(state["schema_version"] == 2 and state["stable"] is None and state["active"] is None
            and state["pending"] == [] and state["route"]["weight"] == 0, "Unsafe initial state")
    runtimes = [x for x in release["items"] if x["kind"] == "ServingRuntime"]
    require(len(runtimes) == 1, "Expected one ServingRuntime")
    runtime = runtimes[0]
    require(runtime["spec"]["containers"][0]["image"] == RUNTIME_IMAGE, "Runtime image is not the supplied immutable image")
    require(runtime["metadata"]["annotations"]["ml-template.io/runtime-fingerprint"] == FINGERPRINT, "Full fingerprint missing")
    lanes = [x for x in release["items"] if x["kind"] == "InferenceService"]
    require(len(lanes) == 2 and {x["metadata"]["name"] for x in lanes} == LANES, "Unexpected serving lanes")
    for lane in lanes:
        predictor = lane["spec"]["predictor"]
        require(lane["metadata"]["annotations"]["serving.kserve.io/deploymentMode"] == "Standard", "Wrong KServe mode")
        require(predictor["serviceAccountName"] == "kserve-artifact-reader", "Artifact reader changed")
        require(predictor["model"]["storageUri"] == STORAGE, "ServingPair storage URI changed")
        require(predictor["model"]["runtime"] == runtime["metadata"]["name"]
                and predictor["model"]["protocolVersion"] == "v2", "Wrong runtime/protocol")
    deployments = [x for x in control["items"] if x["kind"] == "Deployment"]
    expected = {"model-promotion-consumer-v2", "model-runtime-processor-v2", "model-champion-reconciler-v2",
                "model-release-controller-v2", "credit-risk-router-v2"}
    require(len(deployments) == 5 and {x["metadata"]["name"] for x in deployments} == expected, "Wrong v2 workloads")
    identities = {
        "model-promotion-consumer-v2": ("model-promotion-consumer", "consumer"),
        "model-runtime-processor-v2": ("model-runtime-processor-v2", "processor"),
        "model-champion-reconciler-v2": ("model-champion-reconciler-v2", "reconciler"),
        "model-release-controller-v2": ("model-release-controller-v2", "controller"),
        "credit-risk-router-v2": ("credit-risk-router-v2", "router"),
    }
    for deployment in deployments:
        sa, process = identities[deployment["metadata"]["name"]]
        pod = deployment["spec"]["template"]["spec"]
        require(pod.get("serviceAccountName") == sa, "Wrong workload service account")
        security = pod.get("securityContext", {})
        require(security.get("runAsNonRoot") is True and security.get("runAsUser") == 10001
                and security.get("runAsGroup") == 10001
                and security.get("seccompProfile") == {"type": "RuntimeDefault"}, "Unsafe pod security")
        require(len(pod["containers"]) == 1 and not pod.get("initContainers")
                and not pod.get("ephemeralContainers"), "Unexpected workload containers")
        container = pod["containers"][0]
        require(container.get("command") == ["python", "-m", "control_plane.run_v2"]
                and container.get("args") == [process, "--enable-live"], "Wrong workload process")
        security = container.get("securityContext", {})
        require(security.get("allowPrivilegeEscalation") is False
                and security.get("readOnlyRootFilesystem") is True
                and security.get("capabilities") == {"drop": ["ALL"]}
                and not security.get("privileged", False)
                and not any(k in security for k in ("runAsUser", "runAsGroup", "runAsNonRoot", "seccompProfile")),
                "Unsafe container security")
        require(deployment["spec"]["replicas"] == 0, "Live replica enabled")
        require(all(c["image"] == CONTROL_PLANE_IMAGE for c in deployment["spec"]["template"]["spec"]["containers"]), "Mutable/incorrect control-plane image")
    roles = [x for x in control["items"] if x["kind"] == "Role"]
    require(len(roles) == 5, "Missing v2 roles")
    for role in roles:
        for rule in role["rules"]:
            require(not ({"delete", "deletecollection", "*"} & set(rule["verbs"])), "Destructive RBAC permission")
            require("*" not in rule["resources"] and "*" not in rule["apiGroups"], "Wildcard RBAC")
            if "configmaps" in rule["resources"]:
                require(rule.get("resourceNames") == ["model-release-state-v2"], "Legacy/unrestricted state access")
            if "inferenceservices" in rule["resources"] and rule["verbs"] != ["create"]:
                require(set(rule.get("resourceNames", [])) == LANES, "Legacy/unrestricted lane access")
    text = json.dumps(control)
    for old in ('"model-release-state"', 'credit-risk-classifier-stable', 'credit-risk-classifier-canary',
                'http://credit-risk-router:', 'control_plane.router"', 'control_plane.controller"'):
        require(old not in text, "Legacy reference in v2 control plane")
    services = [x for x in control["items"] if x["kind"] == "Service"]
    require(len(services) == 1 and services[0]["metadata"]["name"] == "credit-risk-router-v2"
            and services[0]["spec"]["type"] == "ClusterIP", "Wrong router Service")
    accounts = {x["metadata"]["name"] for x in control["items"] if x["kind"] == "ServiceAccount"}
    require(accounts == expected - {"model-promotion-consumer-v2"}, "Unexpected v2 service accounts")
    bindings = [x for x in control["items"] if x["kind"] == "RoleBinding"]
    require(len(bindings) == 5 and {x["metadata"]["name"] for x in bindings} == expected, "Incomplete role bindings")
    for binding in bindings:
        name = binding["metadata"]["name"]
        require(binding.get("roleRef") == {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
                "Wrong RoleBinding role identity")
        require(binding.get("subjects") == [{"kind": "ServiceAccount", "name": identities[name][0], "namespace": "production"}],
                "Wrong RoleBinding subject identity")


def manifests(output):
    output.mkdir(parents=True, exist_ok=True)
    release, control = output / "release.json", output / "control-plane.json"
    for module, args in [
        ("scripts.render_release_v2", ["--output", str(release), "--fingerprint", FINGERPRINT,
          "--runtime-image", RUNTIME_IMAGE, "--storage-uri", STORAGE, "--model-version", "3"]),
        ("scripts.render_control_plane_v2", ["--output", str(control), "--image", CONTROL_PLANE_IMAGE])]:
        subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT, check=True, timeout=30)
    validate_manifests(json.loads(release.read_text()), json.loads(control.read_text()))
    print("PASS: offline manifest contracts (not installed CRD schema validation)")


def smoke(image):
    result = subprocess.run(["docker", "image", "inspect", image], check=True, capture_output=True, text=True, timeout=30)
    obj = json.loads(result.stdout)[0]
    require(obj["Os"] == "linux" and obj["Architecture"] == "amd64", "Unexpected platform")
    require(obj["Config"]["User"] == "10001:10001", "Unexpected non-root user")
    require(obj["Config"]["Entrypoint"] == ["python", "-m", "control_plane.run_v2"]
            and obj["Config"]["Cmd"] == ["--help"], "Default command may enable live effects")
    base = ["docker", "run", "--rm", "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges"]
    subprocess.run([*base, obj["Id"]], check=True, timeout=30)
    code = """import importlib, os
from pathlib import Path
assert os.getuid() == os.getgid() == 10001
assert not list(Path('/app').rglob('*.pyc')), 'Local bytecode leaked into build'
for name in ('run_v2','consumer_v2','processor_v2','controller_v2','router_v2','reconciliation_v2'):
    importlib.import_module('control_plane.' + name)
for root in ('/app','/artifacts','/models','/mnt/models'):
    assert not any(p.name == 'MLmodel' or p.suffix in ('.joblib','.skops','.pkl','.pickle','.onnx') for p in Path(root).rglob('*')), root
print('PASS: non-root, imports, artifact-free application filesystem, no network')
"""
    subprocess.run([*base, "--entrypoint=python", obj["Id"], "-c", code], check=True, timeout=30)
    print("PASS: image smoke", obj["Id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("manifests").add_argument("--output", type=Path, required=True)
    commands.add_parser("image-smoke").add_argument("--image", required=True)
    args = parser.parse_args()
    if args.command == "manifests": manifests(args.output)
    else: smoke(args.image)


if __name__ == "__main__": main()
