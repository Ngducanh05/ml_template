"""Immutable runtime materialization and isolated KServe V2 lane effects."""
import copy
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import urlopen
from .autoscaling_v2 import predictor_policy
from .state import Conflict
from .state_v2 import ServingPair, runtime_image
from .runtime_catalog import fingerprint_tag

NS = "production"
ANNOTATION = "ml-template.io/runtime-fingerprint"
OWNER = "ml-template.io/control-plane"
LANES = {lane: f"credit-risk-{lane}-v2" for lane in ("stable", "canary")}


def runtime_name(fingerprint):
    fingerprint_tag(fingerprint)
    return "runtime-" + fingerprint[7:47]


def runtime_manifest(fingerprint, image):
    runtime_image(image)
    return dict(apiVersion="serving.kserve.io/v1alpha1", kind="ServingRuntime",
        metadata=dict(name=runtime_name(fingerprint), namespace=NS,
                      annotations={OWNER: "v2", ANNOTATION: fingerprint, "serving.kserve.io/server-type": "mlserver"}),
        spec=dict(disabled=False, protocolVersions=["v2"],
            supportedModelFormats=[dict(name="mlflow", autoSelect=False)],
            containers=[dict(name="kserve-container", image=image, imagePullPolicy="IfNotPresent",
                env=[dict(name=k, value=v) for k, v in {
                    "MLSERVER_MODEL_IMPLEMENTATION": "mlserver_mlflow.MLflowRuntime",
                    "MLSERVER_HTTP_PORT": "8080", "MLSERVER_GRPC_PORT": "9000",
                    "MODELS_DIR": "/mnt/models", "MLSERVER_PARALLEL_WORKERS": "0"}.items()],
                securityContext=dict(runAsNonRoot=True, runAsUser=10001, runAsGroup=10001,
                    allowPrivilegeEscalation=False, privileged=False, capabilities=dict(drop=["ALL"]),
                    seccompProfile=dict(type="RuntimeDefault")),
                resources=dict(requests=dict(cpu="1", memory="2Gi"), limits=dict(cpu="1", memory="2Gi")))]))


def lane_manifest(lane, pair, fingerprint):
    pair = ServingPair(**pair)
    name = LANES[lane]
    return dict(apiVersion="serving.kserve.io/v1beta1", kind="InferenceService",
        metadata=dict(name=name, namespace=NS, annotations={
            OWNER: "v2", "serving.kserve.io/deploymentMode": "Standard", "ml-template.io/model-version": pair.model_version}),
        spec=dict(predictor=dict(serviceAccountName="kserve-artifact-reader", **predictor_policy(lane),
            securityContext=dict(fsGroup=10001, seccompProfile=dict(type="RuntimeDefault")),
            model=dict(modelFormat=dict(name="mlflow"), protocolVersion="v2",
                       runtime=runtime_name(fingerprint), storageUri=pair.storage_uri))))


def deployment_ready(obj, pair):
    """A Ready old replica must never qualify an incomplete new rollout."""
    if not obj:
        return False
    spec, status = obj.get('spec', {}), obj.get('status', {})
    count = spec.get('replicas', 0)
    template = spec.get('template', {})
    annotations = template.get('metadata', {}).get('annotations', {})
    return (count > 0 and status.get('observedGeneration', 0) >= obj['metadata']['generation']
        and status.get('replicas', 0) == status.get('updatedReplicas', 0) == count
        and status.get('readyReplicas', 0) >= count and status.get('availableReplicas', 0) >= count
        and annotations.get('ml-template.io/model-version') == pair['model_version']
        and annotations.get('internal.serving.kserve.io/storage-initializer-sourceuri') == pair['storage_uri']
        and any(c.get('name') == 'kserve-container' and c.get('image') == pair['runtime_digest']
                for c in template.get('spec', {}).get('containers', [])))


def read_json(url):
    with urlopen(url, timeout=4) as response:
        payload = response.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ValueError("Backend response exceeds bound")
        return json.loads(payload) if payload else {}


class KServeEffects:
    """api.get returns None only on404; replace must use resourceVersion/CAS."""
    def __init__(self, api, router, metrics, probe=read_json):
        self.api, self.router, self.metrics, self.probe = api, router, metrics, probe

    def validate_runtime(self, existing, fingerprint, image):
        expected = runtime_manifest(fingerprint, image)
        if (not existing or existing["metadata"].get("annotations", {}).get(ANNOTATION) != fingerprint
                or existing["metadata"].get("annotations", {}).get(OWNER) != "v2"
                or existing["spec"]["containers"][0]["image"] != image):
            raise ValueError("Runtime identity collision")
        container = existing["spec"]["containers"][0]
        wanted = expected["spec"]["containers"][0]
        if (any(container.get(k) != wanted[k] for k in ("env", "securityContext"))
                or container.get("command") or container.get("args")
                or len(existing["spec"]["containers"]) != 1
                or existing["spec"].get("volumes") or existing["spec"].get("hostIPC")):
            raise ValueError("Runtime security/serving configuration drift")

    @staticmethod
    def lane_matches(existing, desired):
        if existing["metadata"].get("annotations", {}).get(OWNER) != "v2":
            raise ValueError("Existing lane is not owned by v2")
        actual = existing["spec"]["predictor"]
        expected = desired["spec"]["predictor"]
        if any(actual.get(k) != expected[k] for k in ("serviceAccountName", "minReplicas", "maxReplicas", "securityContext")):
            return False
        for key in ('scaleMetric', 'scaleTarget', 'autoScaling'):
            if key in expected and actual.get(key) != expected[key]:
                return False
        if any(actual.get(k) for k in ("containers", "initContainers", "volumes", "hostNetwork", "hostPID", "hostIPC", "imagePullSecrets")):
            return False
        model = actual["model"]
        if any(model.get(k) for k in ("image", "command", "args", "env", "envFrom", "volumeMounts")):
            return False
        return (all(model.get(k) == v for k, v in expected["model"].items())
                and all(existing["metadata"].get("annotations", {}).get(k) == v for k, v in desired["metadata"]["annotations"].items()))

    def ensure_runtime(self, fingerprint, image):
        desired = runtime_manifest(fingerprint, image)
        name = desired["metadata"]["name"]
        existing = self.api.get("servingruntimes", name)
        if existing is None:
            try:
                self.api.create("servingruntimes", desired)
            except Conflict:
                pass
            existing = self.api.get("servingruntimes", name)
        self.validate_runtime(existing, fingerprint, image)
        return name

    def ensure(self, lane, pair, fingerprint):
        self.ensure_runtime(fingerprint, pair["runtime_digest"])
        desired = lane_manifest(lane, pair, fingerprint)
        existing = self.api.get("inferenceservices", LANES[lane])
        if existing is None:
            try:
                self.api.create("inferenceservices", desired)
            except Conflict:
                return False
            return False
        if self.lane_matches(existing, desired):
            return True
        desired["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        self.api.replace("inferenceservices", desired)
        return False

    def ready(self, lane, pair, fingerprint):
        obj = self.api.get("inferenceservices", LANES[lane])
        if obj is None:
            return False
        if not self.lane_matches(obj, lane_manifest(lane, pair, fingerprint)):
            return False
        model = obj["spec"]["predictor"]["model"]
        status = obj.get("status", {})
        if (model.get("runtime") != runtime_name(fingerprint) or model.get("storageUri") != pair["storage_uri"]
                or obj["metadata"].get("annotations", {}).get("ml-template.io/model-version") != pair["model_version"]
                or status.get("observedGeneration", 0) < obj["metadata"]["generation"]
                or not any(c["type"] == "Ready" and c["status"] == "True" for c in status.get("conditions", []))):
            return False
        self.validate_runtime(self.api.get("servingruntimes", runtime_name(fingerprint)), fingerprint, pair["runtime_digest"])
        if not deployment_ready(self.api.get_deployment(lane), pair):
            return False
        base = status.get("address", {}).get("url", "")
        parsed = urlsplit(base)
        # Use the controller-reported internal service address, never external URL.
        allowed = {LANES[lane] + "-predictor." + NS + ".svc.cluster.local",
                   LANES[lane] + "-predictor." + NS + ".svc"}
        if parsed.scheme != "http" or parsed.hostname not in allowed or parsed.username or parsed.query or parsed.fragment:
            raise ValueError("Invalid internal KServe address")
        base = base.rstrip("/")
        try:
            self.probe(base + "/v2/health/ready")
            self.probe(base + f"/v2/models/{LANES[lane]}/ready")
            metadata = self.probe(base + f"/v2/models/{LANES[lane]}")
        except HTTPError as exc:
            if exc.code in (404, 503):
                return False
            raise
        if metadata.get("name") != LANES[lane] or not metadata.get("inputs") or not metadata.get("outputs"):
            raise ValueError("Backend metadata contract mismatch")
        return True
